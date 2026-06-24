import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import ijson
from dotenv import load_dotenv

load_dotenv()

TENANT_ID = os.getenv("AZURE_SQL_TENANT_ID")

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


def stream_servers(log_file: Path):
    with log_file.open("rb") as fh:
        yield from ijson.items(fh, "servers.item")


def _make_bom_ref(kind: str, key: str) -> str:
    sanitized = re.sub(r"[^a-zA-Z0-9\-_.]", "-", key)
    return f"{kind}/{sanitized}"


def extract_server_service(server: dict) -> dict:
    name    = server.get("server_name", "")
    rid     = server.get("resource_id", "")
    details = server.get("details", {}) or {}

    properties: list[dict] = [
        {"name": "azure:ResourceType",   "value": "Microsoft.Sql/servers"},
        {"name": "azure:ResourceId",     "value": rid},
        {"name": "azure:Location",       "value": server.get("location", "")},
        {"name": "azure:ResourceGroup",  "value": server.get("resource_group", "")},
        {"name": "azure:SubscriptionId", "value": server.get("subscription_id", "")},
    ]

    if details.get("fqdn"):
        properties.append({"name": "azure:FQDN",               "value": details["fqdn"]})
    if details.get("version"):
        properties.append({"name": "azure:Version",            "value": details["version"]})
    if details.get("state"):
        properties.append({"name": "azure:State",              "value": details["state"]})
    if details.get("administrator_login"):
        properties.append({"name": "azure:AdminLogin",         "value": details["administrator_login"]})
    if details.get("public_network_access"):
        properties.append({"name": "azure:PublicNetworkAccess","value": details["public_network_access"]})
    if details.get("minimal_tls_version"):
        properties.append({"name": "azure:MinimalTLSVersion",  "value": details["minimal_tls_version"]})

    databases = server.get("databases", [])
    properties.append({"name": "azure:DatabaseCount", "value": str(len(databases))})

    return {
        "type":       "service",
        "bom-ref":    _make_bom_ref("service", f"server-{name}"),
        "name":       name,
        "properties": properties,
    }


def extract_database_service(server_name: str, db: dict) -> dict:
    db_name = db.get("database_name", "")

    properties: list[dict] = [
        {"name": "azure:ResourceType",  "value": "Microsoft.Sql/servers/databases"},
        {"name": "azure:ParentServer",  "value": server_name},
    ]

    if db.get("sku_tier"):
        properties.append({"name": "azure:SkuTier",         "value": db["sku_tier"]})
    if db.get("sku_name"):
        properties.append({"name": "azure:SkuName",         "value": db["sku_name"]})
    if db.get("sku_capacity") is not None:
        properties.append({"name": "azure:SkuCapacity",     "value": str(db["sku_capacity"])})
    if db.get("status"):
        properties.append({"name": "azure:Status",          "value": db["status"]})
    if db.get("collation"):
        properties.append({"name": "azure:Collation",       "value": db["collation"]})
    if db.get("max_size_gb") is not None:
        properties.append({"name": "azure:MaxSizeGB",       "value": str(db["max_size_gb"])})
    properties.append({"name": "azure:ZoneRedundant",       "value": str(db.get("zone_redundant", False)).lower()})
    if db.get("read_scale"):
        properties.append({"name": "azure:ReadScale",       "value": db["read_scale"]})
    if db.get("high_availability_replica_count") is not None:
        properties.append({"name": "azure:HAReplicaCount",  "value": str(db["high_availability_replica_count"])})
    if db.get("backup_storage_redundancy"):
        properties.append({"name": "azure:BackupStorageRedundancy", "value": db["backup_storage_redundancy"]})
    properties.append({"name": "azure:InElasticPool",       "value": str(db.get("in_elastic_pool", False)).lower()})
    if db.get("creation_date"):
        properties.append({"name": "azure:CreatedAt",       "value": db["creation_date"]})

    return {
        "type":       "service",
        "bom-ref":    _make_bom_ref("service", f"db-{server_name}-{db_name}"),
        "name":       db_name,
        "properties": properties,
    }


def extract_resource_provider_service(rp_name: str) -> dict:
    return {
        "type":       "service",
        "bom-ref":    _make_bom_ref("service", rp_name),
        "name":       rp_name,
        "properties": [{"name": "azure:ResourceType", "value": "ResourceProvider"}],
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
    server_services:    list[dict],
    db_services:        list[dict],
    rp_services:        list[dict],
    client_components:  list[dict],
    server_to_dbs:      dict[str, list[str]],
    server_to_callers:  dict[str, list[str]],
    log_file:           Path,
) -> dict:
    root_ref     = "root-azure-tenant"
    server_refs  = [s["bom-ref"] for s in server_services]
    rp_refs      = [s["bom-ref"] for s in rp_services]
    comp_refs    = [c["bom-ref"] for c in client_components]

    caller_ref_map: dict[str, str] = {}
    for comp in client_components:
        app_id = next((p["value"] for p in comp["properties"] if p["name"] == "azure:AppId"), comp["name"])
        caller_ref_map[app_id] = comp["bom-ref"]

    db_ref_map: dict[str, list[str]] = {}
    for db_svc in db_services:
        parent = next((p["value"] for p in db_svc["properties"] if p["name"] == "azure:ParentServer"), "")
        db_ref_map.setdefault(parent, []).append(db_svc["bom-ref"])

    dependencies = [{"ref": root_ref, "dependsOn": server_refs + rp_refs}]

    for svc in server_services:
        sname       = svc["name"]
        db_refs     = db_ref_map.get(sname, [])
        caller_refs = [caller_ref_map[a] for a in server_to_callers.get(sname, []) if a in caller_ref_map]
        dependencies.append({"ref": svc["bom-ref"], "dependsOn": db_refs + caller_refs})

    for ref in [s["bom-ref"] for s in db_services] + rp_refs + comp_refs:
        dependencies.append({"ref": ref, "dependsOn": []})

    return {
        "bomFormat":   "CycloneDX",
        "specVersion": "1.6",
        "version":     1,
        "metadata": {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "tools": [{"name": "azure-sql-bom-generator", "version": "1.0.0"}],
            "component": {
                "type":    "platform",
                "bom-ref": root_ref,
                "name":    f"azure-tenant-{TENANT_ID}",
                "version": "1",
                "properties": [
                    {"name": "azure:TenantId",  "value": TENANT_ID},
                    {"name": "source:LogFile",  "value": log_file.name},
                ],
            },
        },
        "components":   client_components,
        "services":     server_services + db_services + rp_services,
        "dependencies": dependencies,
    }


def process_log_file(log_file: Path) -> dict:
    seen_servers  = DeduplicatingSet()
    seen_dbs      = DeduplicatingSet()
    seen_rps      = DeduplicatingSet()
    seen_apps     = DeduplicatingSet()

    server_services:   list[dict] = []
    db_services:       list[dict] = []
    rp_services:       list[dict] = []
    client_components: list[dict] = []
    server_to_dbs:     dict[str, list[str]] = {}
    server_to_callers: dict[str, list[str]] = {}

    for server in stream_servers(log_file):
        sname = server.get("server_name", "")
        if not seen_servers.add_if_new(sname):
            continue

        server_services.append(extract_server_service(server))

        for db in server.get("databases", []) or []:
            db_name = db.get("database_name", "")
            db_key  = f"{sname}/{db_name}"
            if db_name and seen_dbs.add_if_new(db_key):
                db_services.append(extract_database_service(sname, db))
                server_to_dbs.setdefault(sname, []).append(db_name)

        for rp_name in server.get("resource_providers", []) or []:
            if rp_name and seen_rps.add_if_new(rp_name):
                rp_services.append(extract_resource_provider_service(rp_name))

        for app_id in server.get("caller_app_ids", []) or []:
            if app_id and seen_apps.add_if_new(app_id):
                client_components.append(extract_client_app_component(app_id))
            if app_id:
                server_to_callers.setdefault(sname, []).append(app_id)

    return build_cyclonedx_bom(
        server_services, db_services, rp_services, client_components,
        server_to_dbs, server_to_callers, log_file,
    )


def main(target_file: Path | None = None) -> None:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)

    if target_file:
        log_files = [Path(target_file)]
    else:
        log_files = sorted(LOGS_DIR.glob("azuresql_logs_*.json"))
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
        total_svcs = len(bom["services"])
        server_count = sum(1 for s in bom["services"] if any(p["value"] == "Microsoft.Sql/servers" for p in s.get("properties", [])))
        db_count     = sum(1 for s in bom["services"] if any(p["value"] == "Microsoft.Sql/servers/databases" for p in s.get("properties", [])))
        print(f"  Servers     : {server_count}")
        print(f"  Databases   : {db_count}")
        print(f"  Components  : {len(bom['components'])}")
        print(f"  BOM written : {out_file}")


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    main(target_file=target)
