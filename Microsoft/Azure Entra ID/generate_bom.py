import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import ijson
from dotenv import load_dotenv

load_dotenv()

TENANT_ID = os.getenv("AZURE_ENTRAID_TENANT_ID", "unknown-tenant")

REPORT_DIR = Path(__file__).parent / "report"
LOGS_DIR   = Path(__file__).parent / "logs"

COMPONENT_TYPES = {"User", "Group", "ServicePrincipal"}
SERVICE_TYPES   = {"AppRegistration", "DirectoryRole"}


class BloomFilter:
    def __init__(self, capacity: int = 500_000, fpr: float = 0.0001):
        import math
        import mmh3
        import bitarray
        self.mmh3 = mmh3
        n, p      = capacity, fpr
        m         = -int(n * math.log(p) / (math.log(2) ** 2))
        self.k    = int((m / n) * math.log(2))
        self.bits = bitarray.bitarray(m)
        self.bits.setall(0)
        self.m    = m

    def _hashes(self, item: str):
        h1 = self.mmh3.hash(item, 0, signed=False)
        h2 = self.mmh3.hash(item, 1, signed=False)
        return [(h1 + i * h2) % self.m for i in range(self.k)]

    def add(self, item: str) -> None:
        for idx in self._hashes(item):
            self.bits[idx] = 1

    def __contains__(self, item: str) -> bool:
        return all(self.bits[idx] for idx in self._hashes(item))


class DeduplicatingSet:
    def __init__(self):
        self.bloom  = BloomFilter()
        self.exact  = set()

    def add_if_new(self, key: str) -> bool:
        if key in self.bloom and key in self.exact:
            return False
        self.bloom.add(key)
        self.exact.add(key)
        return True


def stream_resources(log_file: Path):
    with log_file.open("rb") as fh:
        yield from ijson.items(fh, "resources.item")


def extract_component(resource: dict) -> dict | None:
    rtype = resource.get("resource_type", "")
    if rtype not in COMPONENT_TYPES:
        return None
    rid  = resource.get("id", "")
    name = resource.get("display_name", rid)
    return {"resource_type": rtype, "id": rid, "name": name, "raw": resource}


def extract_service(resource: dict) -> dict | None:
    rtype = resource.get("resource_type", "")
    if rtype not in SERVICE_TYPES:
        return None
    rid  = resource.get("id", "")
    name = resource.get("display_name", rid)
    return {"resource_type": rtype, "id": rid, "name": name, "raw": resource}


def _make_bom_ref(kind: str, key: str) -> str:
    sanitized = re.sub(r"[^a-zA-Z0-9\-_.]", "-", key)
    return f"{kind}/{sanitized}"


def to_cyclonedx_component(raw_component: dict) -> dict:
    rtype = raw_component["resource_type"]
    rid   = raw_component["id"]
    name  = raw_component["name"]
    data  = raw_component["raw"]

    properties: list[dict] = [
        {"name": "azure:ResourceType", "value": rtype},
        {"name": "azure:ObjectId",     "value": rid},
    ]

    if rtype == "User":
        if data.get("user_principal_name"):
            properties.append({"name": "azure:UserPrincipalName", "value": data["user_principal_name"]})
        if data.get("mail"):
            properties.append({"name": "azure:Mail", "value": data["mail"]})
        properties.append({"name": "azure:AccountEnabled",  "value": str(data.get("account_enabled", "")).lower()})
        properties.append({"name": "azure:UserType",        "value": data.get("user_type", "")})
        if data.get("job_title"):
            properties.append({"name": "azure:JobTitle",   "value": data["job_title"]})
        if data.get("department"):
            properties.append({"name": "azure:Department",  "value": data["department"]})
        if data.get("created_datetime"):
            properties.append({"name": "azure:CreatedAt",   "value": data["created_datetime"]})

    elif rtype == "Group":
        properties.append({"name": "azure:SecurityEnabled", "value": str(data.get("security_enabled", "")).lower()})
        properties.append({"name": "azure:MailEnabled",     "value": str(data.get("mail_enabled", "")).lower()})
        properties.append({"name": "azure:IsDynamic",       "value": str(data.get("is_dynamic", "")).lower()})
        properties.append({"name": "azure:MemberCount",     "value": str(data.get("member_count", 0))})
        if data.get("created_datetime"):
            properties.append({"name": "azure:CreatedAt",   "value": data["created_datetime"]})
        if data.get("description"):
            properties.append({"name": "azure:Description", "value": data["description"]})

    elif rtype == "ServicePrincipal":
        if data.get("app_id"):
            properties.append({"name": "azure:AppId",                "value": data["app_id"]})
        properties.append({"name": "azure:ServicePrincipalType",      "value": data.get("service_principal_type", "")})
        properties.append({"name": "azure:AccountEnabled",            "value": str(data.get("account_enabled", "")).lower()})
        if data.get("created_datetime"):
            properties.append({"name": "azure:CreatedAt",             "value": data["created_datetime"]})
        if data.get("description"):
            properties.append({"name": "azure:Description",           "value": data["description"]})

    return {
        "type":    "library",
        "bom-ref": _make_bom_ref("component", rid or name),
        "name":    name,
        "version": "1",
        "properties": properties,
    }


def to_cyclonedx_service(raw_service: dict) -> dict:
    rtype = raw_service["resource_type"]
    rid   = raw_service["id"]
    name  = raw_service["name"]
    data  = raw_service["raw"]

    properties: list[dict] = [
        {"name": "azure:ResourceType", "value": rtype},
        {"name": "azure:ObjectId",     "value": rid},
    ]

    if rtype == "AppRegistration":
        if data.get("app_id"):
            properties.append({"name": "azure:AppId",          "value": data["app_id"]})
        if data.get("created_datetime"):
            properties.append({"name": "azure:CreatedAt",      "value": data["created_datetime"]})
        if data.get("sign_in_audience"):
            properties.append({"name": "azure:SignInAudience",  "value": data["sign_in_audience"]})
        if data.get("description"):
            properties.append({"name": "azure:Description",    "value": data["description"]})

    elif rtype == "DirectoryRole":
        if data.get("role_template_id"):
            properties.append({"name": "azure:RoleTemplateId", "value": data["role_template_id"]})
        if data.get("description"):
            properties.append({"name": "azure:Description",    "value": data["description"]})

    return {
        "type":       "service",
        "bom-ref":    _make_bom_ref("service", rid or name),
        "name":       name,
        "properties": properties,
    }


def build_cyclonedx_bom(components: list[dict], services: list[dict], log_file: Path) -> dict:
    all_refs = [c["bom-ref"] for c in components] + [s["bom-ref"] for s in services]
    root_ref = "root-azure-tenant"
    dependencies = [{"ref": root_ref, "dependsOn": all_refs}]
    for comp in components:
        dependencies.append({"ref": comp["bom-ref"], "dependsOn": []})
    for svc in services:
        dependencies.append({"ref": svc["bom-ref"], "dependsOn": []})

    return {
        "bomFormat":  "CycloneDX",
        "specVersion": "1.6",
        "version":    1,
        "metadata": {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "tools": [{"name": "azure-entraid-bom-generator", "version": "1.0.0"}],
            "component": {
                "type":    "platform",
                "bom-ref": root_ref,
                "name":    f"azure-tenant-{TENANT_ID}",
                "version": "1",
                "properties": [
                    {"name": "azure:TenantId", "value": TENANT_ID},
                    {"name": "source:LogFile", "value": log_file.name},
                ],
            },
        },
        "components": components,
        "services":   services,
        "dependencies": dependencies,
    }


def process_log_file(log_file: Path) -> dict:
    seen_components = DeduplicatingSet()
    seen_services   = DeduplicatingSet()
    raw_components: list[dict] = []
    raw_services:   list[dict] = []

    for resource in stream_resources(log_file):
        comp = extract_component(resource)
        if comp:
            key = comp["id"] or comp["name"]
            if seen_components.add_if_new(key):
                raw_components.append(comp)
            continue

        svc = extract_service(resource)
        if svc:
            key = svc["id"] or svc["name"]
            if seen_services.add_if_new(key):
                raw_services.append(svc)

    bom_components = [to_cyclonedx_component(c) for c in raw_components]
    bom_services   = [to_cyclonedx_service(s)   for s in raw_services]
    return build_cyclonedx_bom(bom_components, bom_services, log_file)


def main(target_file: Path | None = None) -> None:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)

    if target_file:
        log_files = [Path(target_file)]
    else:
        log_files = sorted(LOGS_DIR.glob("entraid_logs_*.json"))
        if not log_files:
            print(f"No log files found in {LOGS_DIR}")
            return

    for log_file in log_files:
        print(f"Processing {log_file.name}...")
        bom       = process_log_file(log_file)
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H-%M-%S")
        out_file  = REPORT_DIR / f"bom_{log_file.stem}_{timestamp}.json"
        with out_file.open("w", encoding="utf-8") as fh:
            json.dump(bom, fh, indent=2, ensure_ascii=False)
        print(f"  Components : {len(bom['components'])}")
        print(f"  Services   : {len(bom['services'])}")
        print(f"  BOM written: {out_file}")


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    main(target_file=target)
