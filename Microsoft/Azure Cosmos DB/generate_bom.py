import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import ijson
from dotenv import load_dotenv

load_dotenv()

TENANT_ID = os.getenv("AZURE_COSMOSDB_TENANT_ID", "unknown-tenant")

REPORT_DIR = Path(__file__).parent / "report"
LOGS_DIR   = Path(__file__).parent / "logs"


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
        self.bloom = BloomFilter()
        self.exact = set()

    def add_if_new(self, key: str) -> bool:
        if key in self.bloom and key in self.exact:
            return False
        self.bloom.add(key)
        self.exact.add(key)
        return True


def stream_accounts(log_file: Path):
    with log_file.open("rb") as fh:
        yield from ijson.items(fh, "accounts.item")


def _make_bom_ref(kind: str, key: str) -> str:
    sanitized = re.sub(r"[^a-zA-Z0-9\-_.]", "-", key)
    return f"{kind}/{sanitized}"


def extract_cosmosdb_service(account: dict) -> dict:
    name    = account.get("account_name", "")
    rid     = account.get("resource_id", "")
    details = account.get("details", {}) or {}

    properties: list[dict] = [
        {"name": "azure:ResourceType",   "value": "Microsoft.DocumentDB/databaseAccounts"},
        {"name": "azure:ResourceId",     "value": rid},
        {"name": "azure:Location",       "value": account.get("location", "")},
        {"name": "azure:ResourceGroup",  "value": account.get("resource_group", "")},
        {"name": "azure:SubscriptionId", "value": account.get("subscription_id", "")},
    ]

    if details.get("api_type"):
        properties.append({"name": "azure:ApiType",          "value": details["api_type"]})
    if details.get("kind"):
        properties.append({"name": "azure:Kind",             "value": details["kind"]})
    if details.get("provisioning_state"):
        properties.append({"name": "azure:ProvisioningState","value": details["provisioning_state"]})
    if details.get("public_network_access"):
        properties.append({"name": "azure:PublicNetworkAccess", "value": details["public_network_access"]})
    if details.get("enable_free_tier") is not None:
        properties.append({"name": "azure:FreeTierEnabled",  "value": str(details["enable_free_tier"]).lower()})
    if details.get("enable_automatic_failover") is not None:
        properties.append({"name": "azure:AutomaticFailoverEnabled", "value": str(details["enable_automatic_failover"]).lower()})
    if details.get("backup_policy_type"):
        properties.append({"name": "azure:BackupPolicyType", "value": details["backup_policy_type"]})
    if details.get("document_endpoint"):
        properties.append({"name": "azure:DocumentEndpoint", "value": details["document_endpoint"]})

    cp = details.get("consistency_policy", {}) or {}
    if cp.get("level"):
        properties.append({"name": "azure:ConsistencyLevel",   "value": cp["level"]})
    if cp.get("max_staleness_prefix") is not None:
        properties.append({"name": "azure:MaxStalenessPrefix", "value": str(cp["max_staleness_prefix"])})
    if cp.get("max_interval_in_seconds") is not None:
        properties.append({"name": "azure:MaxIntervalSeconds",  "value": str(cp["max_interval_in_seconds"])})

    locations = details.get("locations", [])
    if locations:
        properties.append({"name": "azure:Locations",       "value": ", ".join(locations)})
        properties.append({"name": "azure:LocationCount",   "value": str(len(locations))})

    databases = details.get("databases", [])
    if databases:
        properties.append({"name": "azure:DatabaseCount",   "value": str(len(databases))})
        properties.append({"name": "azure:Databases",       "value": ", ".join(databases)})

    return {
        "type":       "service",
        "bom-ref":    _make_bom_ref("service", name or rid),
        "name":       name,
        "properties": properties,
    }


def extract_resource_provider_service(rp_name: str) -> dict:
    return {
        "type":       "service",
        "bom-ref":    _make_bom_ref("service", rp_name),
        "name":       rp_name,
        "properties": [
            {"name": "azure:ResourceType", "value": "ResourceProvider"},
        ],
    }


def extract_client_app_component(app_id: str) -> dict:
    return {
        "type":    "library",
        "bom-ref": _make_bom_ref("component", app_id),
        "name":    app_id,
        "version": "1",
        "properties": [
            {"name": "azure:ResourceType", "value": "ClientApplication"},
            {"name": "azure:AppId",        "value": app_id},
        ],
    }


def build_cyclonedx_bom(
    account_services:    list[dict],
    rp_services:         list[dict],
    client_components:   list[dict],
    account_to_callers:  dict[str, list[str]],
    log_file:            Path,
) -> dict:
    root_ref      = "root-azure-tenant"
    all_services  = account_services + rp_services
    all_components = client_components

    account_bom_refs = [s["bom-ref"] for s in account_services]
    rp_bom_refs      = [s["bom-ref"] for s in rp_services]
    comp_bom_refs    = [c["bom-ref"] for c in all_components]

    dependencies = [
        {"ref": root_ref, "dependsOn": account_bom_refs + rp_bom_refs},
    ]

    ref_to_name = {s["bom-ref"]: s["name"] for s in account_services}

    caller_ref_map: dict[str, str] = {}
    for comp in all_components:
        app_id = next(
            (p["value"] for p in comp["properties"] if p["name"] == "azure:AppId"),
            comp["name"],
        )
        caller_ref_map[app_id] = comp["bom-ref"]

    for acct_svc in account_services:
        callers   = account_to_callers.get(acct_svc["name"], [])
        caller_refs = [caller_ref_map[app_id] for app_id in callers if app_id in caller_ref_map]
        dependencies.append({"ref": acct_svc["bom-ref"], "dependsOn": caller_refs})

    for ref in rp_bom_refs + comp_bom_refs:
        dependencies.append({"ref": ref, "dependsOn": []})

    return {
        "bomFormat":   "CycloneDX",
        "specVersion": "1.6",
        "version":     1,
        "metadata": {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "tools": [{"name": "azure-cosmosdb-bom-generator", "version": "1.0.0"}],
            "component": {
                "type":    "platform",
                "bom-ref": root_ref,
                "name":    f"azure-tenant-{TENANT_ID}",
                "version": "1",
                "properties": [
                    {"name": "azure:TenantId",   "value": TENANT_ID},
                    {"name": "source:LogFile",   "value": log_file.name},
                ],
            },
        },
        "components":   all_components,
        "services":     all_services,
        "dependencies": dependencies,
    }


def process_log_file(log_file: Path) -> dict:
    seen_accounts = DeduplicatingSet()
    seen_rps      = DeduplicatingSet()
    seen_apps     = DeduplicatingSet()

    account_services:    list[dict] = []
    rp_services:         list[dict] = []
    client_components:   list[dict] = []
    account_to_callers:  dict[str, list[str]] = {}

    for account in stream_accounts(log_file):
        name = account.get("account_name", "")
        if not seen_accounts.add_if_new(name):
            continue

        svc = extract_cosmosdb_service(account)
        account_services.append(svc)

        caller_app_ids    = account.get("caller_app_ids", []) or []
        resource_providers = account.get("resource_providers", []) or []
        account_to_callers[name] = caller_app_ids

        for rp_name in resource_providers:
            if rp_name and seen_rps.add_if_new(rp_name):
                rp_services.append(extract_resource_provider_service(rp_name))

        for app_id in caller_app_ids:
            if app_id and seen_apps.add_if_new(app_id):
                client_components.append(extract_client_app_component(app_id))

    return build_cyclonedx_bom(account_services, rp_services, client_components, account_to_callers, log_file)


def main(target_file: Path | None = None) -> None:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)

    if target_file:
        log_files = [Path(target_file)]
    else:
        log_files = sorted(LOGS_DIR.glob("cosmosdb_logs_*.json"))
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
        print(f"  Account services : {len([s for s in bom['services'] if 'DocumentDB' in str(s.get('properties', []))])}")
        print(f"  Total services   : {len(bom['services'])}")
        print(f"  Components       : {len(bom['components'])}")
        print(f"  BOM written      : {out_file}")


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    main(target_file=target)
