import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import ijson
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent.parent / ".env")

REGION = os.getenv("AWS_DEFAULT_REGION", "us-east-1")

REPORT_DIR = Path(__file__).parent / "report"
LOGS_DIR   = Path(__file__).parent / "logs"

RESOURCE_PARAM_KEYS = {
    "dBInstanceIdentifier": "DBInstance",
    "dBClusterIdentifier":  "DBCluster",
    "dBSnapshotIdentifier": "DBSnapshot",
    "dBSubnetGroupName":    "DBSubnetGroup",
    "dBParameterGroupName": "DBParameterGroup",
}


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


def stream_events(log_file: Path):
    with log_file.open("rb") as fh:
        yield from ijson.items(fh, "item")


def extract_resource_inventory(event: dict):
    if event.get("EventSource") != "rds-local-enumeration":
        return None
    params    = event.get("requestParameters", {}) or {}
    inventory = event.get("inventory", {}) or {}
    for param_key, resource_type in RESOURCE_PARAM_KEYS.items():
        name = params.get(param_key)
        if name:
            return resource_type, name, inventory
    return None


def extract_rds_resource(event: dict):
    params = event.get("requestParameters", {}) or {}
    for param_key, resource_type in RESOURCE_PARAM_KEYS.items():
        name = params.get(param_key)
        if name:
            return resource_type, name

    for res in event.get("Resources", []):
        rtype = res.get("ResourceType", "")
        rname = res.get("ResourceName", "")
        if "DBInstance" in rtype and rname:
            return "DBInstance", rname
        if "DBCluster" in rtype and rname:
            return "DBCluster", rname

    return None


def extract_iam_principal(event: dict):
    identity = event.get("userIdentity", {}) or {}
    kind     = identity.get("type", "")
    if kind == "IAMUser":
        return "IAMUser", identity.get("userName", identity.get("arn", ""))
    if kind == "AssumedRole":
        session = identity.get("sessionContext", {}) or {}
        entity  = session.get("sessionIssuer", {}) or {}
        return "AssumedRole", entity.get("userName", identity.get("arn", ""))
    if kind == "Root":
        return "Root", identity.get("accountId", "root")
    return None


def _make_bom_ref(kind: str, key: str) -> str:
    sanitized = re.sub(r"[^a-zA-Z0-9\-_.]", "-", key)
    return f"{kind}/{sanitized}"


def to_cyclonedx_service(raw: dict, inventory: dict | None = None) -> dict:
    resource_type = raw["resource_type"]
    resource_name = raw["resource_name"]
    inv           = inventory or {}

    properties: list[dict] = [
        {"name": "aws:ResourceType",  "value": resource_type},
        {"name": "aws:ResourceName",  "value": resource_name},
        {"name": "aws:Region",        "value": REGION},
    ]

    if resource_type == "DBInstance":
        if inv.get("DBInstanceArn"):
            properties.append({"name": "aws:DBInstanceArn",     "value": inv["DBInstanceArn"]})
        if inv.get("Engine"):
            properties.append({"name": "aws:Engine",            "value": inv["Engine"]})
        if inv.get("EngineVersion"):
            properties.append({"name": "aws:EngineVersion",     "value": inv["EngineVersion"]})
        if inv.get("DBInstanceClass"):
            properties.append({"name": "aws:DBInstanceClass",   "value": inv["DBInstanceClass"]})
        if inv.get("DBInstanceStatus"):
            properties.append({"name": "aws:Status",            "value": inv["DBInstanceStatus"]})
        properties.append({"name": "aws:MultiAZ",               "value": str(inv.get("MultiAZ", False)).lower()})
        properties.append({"name": "aws:StorageEncrypted",      "value": str(inv.get("StorageEncrypted", False)).lower()})
        if inv.get("KMSKeyId"):
            properties.append({"name": "aws:KMSKeyId",          "value": inv["KMSKeyId"]})
        if inv.get("StorageType"):
            properties.append({"name": "aws:StorageType",       "value": inv["StorageType"]})
        if inv.get("AllocatedStorageGB"):
            properties.append({"name": "aws:AllocatedStorageGB","value": str(inv["AllocatedStorageGB"])})
        if inv.get("MaxAllocatedStorageGB"):
            properties.append({"name": "aws:MaxAllocatedStorageGB", "value": str(inv["MaxAllocatedStorageGB"])})
        properties.append({"name": "aws:DeletionProtection",    "value": str(inv.get("DeletionProtection", False)).lower()})
        properties.append({"name": "aws:PubliclyAccessible",    "value": str(inv.get("PubliclyAccessible", False)).lower()})
        properties.append({"name": "aws:IAMDBAuthEnabled",      "value": str(inv.get("IAMDatabaseAuthenticationEnabled", False)).lower()})
        if inv.get("Endpoint"):
            properties.append({"name": "aws:Endpoint",          "value": inv["Endpoint"]})
        if inv.get("AvailabilityZone"):
            properties.append({"name": "aws:AvailabilityZone",  "value": inv["AvailabilityZone"]})
        if inv.get("DBSubnetGroup"):
            properties.append({"name": "aws:DBSubnetGroup",     "value": inv["DBSubnetGroup"]})
        if inv.get("VpcId"):
            properties.append({"name": "aws:VpcId",             "value": inv["VpcId"]})
        if inv.get("DBName"):
            properties.append({"name": "aws:DBName",            "value": inv["DBName"]})
        if inv.get("MasterUsername"):
            properties.append({"name": "aws:MasterUsername",    "value": inv["MasterUsername"]})
        if inv.get("CACertificateIdentifier"):
            properties.append({"name": "aws:CACertificate",     "value": inv["CACertificateIdentifier"]})
        if inv.get("BackupRetentionPeriodDays") is not None:
            properties.append({"name": "aws:BackupRetentionDays","value": str(inv["BackupRetentionPeriodDays"])})
        properties.append({"name": "aws:AutoMinorVersionUpgrade","value": str(inv.get("AutoMinorVersionUpgrade", False)).lower()})
        if inv.get("InstanceCreateTime"):
            properties.append({"name": "aws:CreatedAt",         "value": inv["InstanceCreateTime"]})
        properties.append({"name": "aws:InventoryStatus",       "value": inv.get("InventoryStatus", "")})

    elif resource_type == "DBCluster":
        if inv.get("DBClusterArn"):
            properties.append({"name": "aws:DBClusterArn",      "value": inv["DBClusterArn"]})
        if inv.get("Engine"):
            properties.append({"name": "aws:Engine",            "value": inv["Engine"]})
        if inv.get("EngineVersion"):
            properties.append({"name": "aws:EngineVersion",     "value": inv["EngineVersion"]})
        if inv.get("EngineMode"):
            properties.append({"name": "aws:EngineMode",        "value": inv["EngineMode"]})
        if inv.get("Status"):
            properties.append({"name": "aws:Status",            "value": inv["Status"]})
        properties.append({"name": "aws:MultiAZ",               "value": str(inv.get("MultiAZ", False)).lower()})
        properties.append({"name": "aws:StorageEncrypted",      "value": str(inv.get("StorageEncrypted", False)).lower()})
        if inv.get("KMSKeyId"):
            properties.append({"name": "aws:KMSKeyId",          "value": inv["KMSKeyId"]})
        properties.append({"name": "aws:DeletionProtection",    "value": str(inv.get("DeletionProtection", False)).lower()})
        if inv.get("Endpoint"):
            properties.append({"name": "aws:Endpoint",          "value": inv["Endpoint"]})
        if inv.get("ReaderEndpoint"):
            properties.append({"name": "aws:ReaderEndpoint",    "value": inv["ReaderEndpoint"]})
        if inv.get("ClusterMemberCount") is not None:
            properties.append({"name": "aws:ClusterMemberCount","value": str(inv["ClusterMemberCount"])})
        if inv.get("AvailabilityZones"):
            properties.append({"name": "aws:AvailabilityZones", "value": ", ".join(inv["AvailabilityZones"])})
        if inv.get("DBSubnetGroup"):
            properties.append({"name": "aws:DBSubnetGroup",     "value": inv["DBSubnetGroup"]})
        if inv.get("MasterUsername"):
            properties.append({"name": "aws:MasterUsername",    "value": inv["MasterUsername"]})
        if inv.get("BackupRetentionPeriodDays") is not None:
            properties.append({"name": "aws:BackupRetentionDays","value": str(inv["BackupRetentionPeriodDays"])})
        properties.append({"name": "aws:IAMDBAuthEnabled",      "value": str(inv.get("IAMDatabaseAuthenticationEnabled", False)).lower()})
        if inv.get("ClusterCreateTime"):
            properties.append({"name": "aws:CreatedAt",         "value": inv["ClusterCreateTime"]})
        properties.append({"name": "aws:InventoryStatus",       "value": inv.get("InventoryStatus", "")})

    else:
        properties.append({"name": "aws:InventoryStatus", "value": "NoDescribe"})

    return {
        "type":       "service",
        "bom-ref":    _make_bom_ref("service", f"{resource_type}-{resource_name}"),
        "name":       resource_name,
        "properties": properties,
    }


def to_cyclonedx_component(raw: dict) -> dict:
    kind, key = raw["kind"], raw["key"]
    return {
        "type":    "library",
        "bom-ref": _make_bom_ref("component", f"{kind}-{key}"),
        "name":    key,
        "version": "1",
        "properties": [
            {"name": "aws:PrincipalType", "value": kind},
            {"name": "aws:PrincipalName", "value": key},
        ],
    }


def build_dependency_graph(raw_components, raw_services, principal_resources):
    root_ref  = "root-aws-account"
    svc_refs  = [to_cyclonedx_service(s)["bom-ref"]  for s in raw_services]
    comp_refs = [to_cyclonedx_component(c)["bom-ref"] for c in raw_components]

    comp_ref_map = {
        c["key"]: to_cyclonedx_component(c)["bom-ref"] for c in raw_components
    }

    dependencies = [{"ref": root_ref, "dependsOn": svc_refs}]

    for svc in raw_services:
        svc_ref     = to_cyclonedx_service(svc)["bom-ref"]
        svc_name    = svc["resource_name"]
        caller_refs = [
            comp_ref_map[p]
            for p in principal_resources.get(svc_name, [])
            if p in comp_ref_map
        ]
        dependencies.append({"ref": svc_ref, "dependsOn": caller_refs})

    for ref in comp_refs:
        dependencies.append({"ref": ref, "dependsOn": []})

    return dependencies


def build_cyclonedx_bom(raw_components, raw_services, principal_resources, log_file):
    services      = [to_cyclonedx_service(s, s.get("inventory"))  for s in raw_services]
    components    = [to_cyclonedx_component(c)                    for c in raw_components]
    dependencies  = build_dependency_graph(raw_components, raw_services, principal_resources)
    root_ref      = "root-aws-account"

    return {
        "bomFormat":   "CycloneDX",
        "specVersion": "1.6",
        "version":     1,
        "metadata": {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "tools": [{"name": "aws-rds-bom-generator", "version": "1.0.0"}],
            "component": {
                "type":    "platform",
                "bom-ref": root_ref,
                "name":    "aws-account",
                "version": "1",
                "properties": [
                    {"name": "aws:Region",      "value": REGION},
                    {"name": "source:LogFile",  "value": log_file.name},
                ],
            },
        },
        "components":   components,
        "services":     services,
        "dependencies": dependencies,
    }


def process_log_file(log_file: Path) -> dict:
    seen_services    = DeduplicatingSet()
    seen_components  = DeduplicatingSet()
    raw_services:    list[dict] = []
    raw_components:  list[dict] = []
    inventory_map:   dict[str, dict] = {}
    principal_resources: dict[str, list[str]] = {}

    for event in stream_events(log_file):
        inv = extract_resource_inventory(event)
        if inv:
            rtype, rname, inventory = inv
            inventory_map[rname] = inventory

        resource = extract_rds_resource(event)
        if resource:
            rtype, rname = resource
            key = f"{rtype}-{rname}"
            if seen_services.add_if_new(key):
                raw_services.append({"resource_type": rtype, "resource_name": rname})

        principal = extract_iam_principal(event)
        if principal:
            pkind, pkey = principal
            if pkey and seen_components.add_if_new(pkey):
                raw_components.append({"kind": pkind, "key": pkey})
            if resource and pkey:
                rname = resource[1]
                principal_resources.setdefault(rname, [])
                if pkey not in principal_resources[rname]:
                    principal_resources[rname].append(pkey)

    for svc in raw_services:
        svc["inventory"] = inventory_map.get(svc["resource_name"], {})

    return build_cyclonedx_bom(raw_components, raw_services, principal_resources, log_file)


def main(target_file: Path | None = None) -> None:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)

    if target_file:
        log_files = [Path(target_file)]
    else:
        log_files = sorted(LOGS_DIR.glob("rds_logs_*.json"))
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
        print(f"  Services    : {len(bom['services'])}")
        print(f"  Components  : {len(bom['components'])}")
        print(f"  BOM written : {out_file}")


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    main(target_file=target)
