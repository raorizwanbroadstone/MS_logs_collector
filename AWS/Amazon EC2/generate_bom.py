import json
import math
import uuid
from datetime import datetime, timezone
from pathlib import Path

import ijson
import mmh3
from bitarray import bitarray

SCRIPT_DIR = Path(__file__).parent
LOGS_DIR   = SCRIPT_DIR / "logs"
REPORT_DIR = SCRIPT_DIR / "report"

BLOOM_CAPACITY = 500_000
BLOOM_FPR      = 0.0001

RESOURCE_PARAM_KEYS = {
    "instanceId":  "Instance",
    "groupId":     "SecurityGroup",
    "keyName":     "KeyPair",
    "imageId":     "Image",
    "volumeId":    "Volume",
    "snapshotId":  "Snapshot",
}


class BloomFilter:

    def __init__(self, capacity: int, fpr: float):
        m = math.ceil(-(capacity * math.log(fpr)) / (math.log(2) ** 2))
        k = max(1, round((m / capacity) * math.log(2)))
        self._m    = m
        self._k    = k
        self._bits = bitarray(m)
        self._bits.setall(0)

    def _positions(self, key: str) -> list[int]:
        h1 = mmh3.hash(key, seed=0, signed=False)
        h2 = mmh3.hash(key, seed=1, signed=False)
        return [(h1 + i * h2) % self._m for i in range(self._k)]

    def add(self, key: str) -> None:
        for p in self._positions(key):
            self._bits[p] = 1

    def might_contain(self, key: str) -> bool:
        return all(self._bits[p] for p in self._positions(key))


class DeduplicatingSet:

    def __init__(self, capacity: int = BLOOM_CAPACITY, fpr: float = BLOOM_FPR):
        self._bloom = BloomFilter(capacity, fpr)
        self._seen: set[str] = set()

    def add_if_new(self, key: str) -> bool:
        if self._bloom.might_contain(key) and key in self._seen:
            return False
        self._bloom.add(key)
        self._seen.add(key)
        return True

    def __len__(self) -> int:
        return len(self._seen)


def stream_events(log_file: Path):
    with log_file.open("rb") as fh:
        yield from ijson.items(fh, "item")


def extract_resource_inventory(event: dict) -> dict | None:
    if event.get("EventSource") != "ec2-local-enumeration":
        return None
    params        = event.get("requestParameters") or {}
    resource_type = params.get("resourceType", "")
    resource_name = params.get("resourceName", "")
    if not resource_name:
        return None
    inv = event.get("inventory") or {}
    return {
        "resource_key":         f"{resource_type}:{resource_name}",
        "resource_type":        resource_type,
        "resource_name":        resource_name,
        "arn":                  inv.get("arn", ""),
        "instance_type":        inv.get("instance_type", ""),
        "state":                inv.get("state", ""),
        "ami_id":               inv.get("ami_id", ""),
        "key_name":             inv.get("key_name", ""),
        "vpc_id":               inv.get("vpc_id", ""),
        "subnet_id":            inv.get("subnet_id", ""),
        "private_ip":           inv.get("private_ip", ""),
        "public_ip":            inv.get("public_ip", ""),
        "platform":             inv.get("platform", ""),
        "architecture":         inv.get("architecture", ""),
        "monitoring":           inv.get("monitoring", ""),
        "launch_time":          inv.get("launch_time", ""),
        "iam_instance_profile": inv.get("iam_instance_profile", ""),
        "security_groups":      inv.get("security_groups", []),
        "volumes":              inv.get("volumes", []),
        "group_name":           inv.get("group_name", ""),
        "description":          inv.get("description", ""),
        "ingress_rule_count":   inv.get("ingress_rule_count", 0),
        "egress_rule_count":    inv.get("egress_rule_count", 0),
        "key_pair_id":          inv.get("key_pair_id", ""),
        "key_type":             inv.get("key_type", ""),
        "volume_type":          inv.get("volume_type", ""),
        "size_gb":              inv.get("size_gb", 0),
        "availability_zone":    inv.get("availability_zone", ""),
        "encrypted":            inv.get("encrypted", False),
        "iops":                 inv.get("iops", 0),
        "throughput":           inv.get("throughput", 0),
        "create_time":          inv.get("create_time", ""),
        "attached_to":          inv.get("attached_to", []),
        "access_denied":        inv.get("access_denied", False),
        "not_found":            inv.get("not_found", False),
    }


def extract_ec2_resource(event: dict) -> dict | None:
    if event.get("EventSource") == "ec2-local-enumeration":
        return None
    params = event.get("requestParameters") or {}
    if not isinstance(params, dict):
        return None

    resource_type = ""
    resource_name = ""

    for param_key, rtype in RESOURCE_PARAM_KEYS.items():
        name = params.get(param_key)
        if name and isinstance(name, str):
            resource_type = rtype
            resource_name = name
            break

    if not resource_name:
        items_set = params.get("instancesSet") or {}
        items     = items_set.get("items") or []
        if items and isinstance(items[0], dict):
            iid = items[0].get("instanceId", "")
            if iid:
                resource_type = "Instance"
                resource_name = iid

    if not resource_name:
        for res in (event.get("Resources") or []):
            if isinstance(res, dict) and "EC2" in res.get("type", ""):
                arn           = res.get("ARN", "")
                resource_name = arn.split("/")[-1]
                rtype         = res.get("type", "")
                resource_type = rtype.split("::")[-1] if "::" in rtype else rtype
                break

    if not resource_name:
        return None

    key = f"{resource_type}:{resource_name}"
    return {
        "kind":          "ec2_resource",
        "key":           key,
        "name":          resource_name,
        "resource_type": resource_type,
        "resource_name": resource_name,
        "region":        event.get("awsRegion", ""),
        "event_name":    event.get("EventName", ""),
        "event_source":  event.get("EventSource", ""),
    }


def extract_iam_principal(event: dict) -> dict | None:
    identity = event.get("userIdentity") or {}
    if not isinstance(identity, dict):
        return None

    identity_type = identity.get("type", "")
    account_id    = identity.get("accountId", "")
    session_arn   = identity.get("arn", "")

    if identity_type == "IAMUser":
        key  = session_arn
        name = identity.get("userName", "") or session_arn
    elif identity_type == "AssumedRole":
        issuer   = (identity.get("sessionContext") or {}).get("sessionIssuer") or {}
        role_arn = issuer.get("arn", session_arn)
        key      = role_arn
        name     = issuer.get("userName", "") or role_arn.split("/")[-1]
    elif identity_type == "Root":
        key  = f"arn:aws:iam::{account_id}:root"
        name = f"Root ({account_id})"
    else:
        key  = session_arn or identity.get("principalId", "")
        name = identity.get("userName", "") or key

    if not key:
        return None

    return {
        "kind":          "iam_principal",
        "key":           key,
        "name":          name,
        "arn":           session_arn,
        "identity_type": identity_type,
        "account_id":    account_id,
        "event_source":  event.get("EventSource", ""),
    }


def _make_bom_ref(kind: str, key: str) -> str:
    safe = key.replace(":", "-").replace("/", "-").replace(".", "-")
    return f"{kind}-{safe}"


def to_cyclonedx_service(raw: dict, inventory: dict | None = None) -> dict:
    svc: dict = {
        "bom-ref":       _make_bom_ref("ec2_resource", raw["key"]),
        "name":          raw["name"],
        "authenticated": True,
    }
    props = [
        {"name": "aws:EC2ResourceType", "value": raw.get("resource_type", "")},
        {"name": "aws:EC2ResourceName", "value": raw.get("resource_name", "")},
        {"name": "aws:Region",          "value": raw.get("region", "")},
        {"name": "aws:EventSource",     "value": raw.get("event_source", "")},
    ]
    if inventory:
        if inventory.get("access_denied"):
            props.append({"name": "aws:InventoryStatus", "value": "AccessDenied"})
        elif inventory.get("not_found"):
            props.append({"name": "aws:InventoryStatus", "value": "NotFound"})
        else:
            rtype = inventory.get("resource_type", "")
            if inventory.get("arn"):
                props.append({"name": "aws:EC2ResourceArn", "value": inventory["arn"]})

            if rtype == "Instance":
                if inventory.get("instance_type"):
                    props.append({"name": "aws:InstanceType",        "value": inventory["instance_type"]})
                if inventory.get("state"):
                    props.append({"name": "aws:State",               "value": inventory["state"]})
                if inventory.get("ami_id"):
                    props.append({"name": "aws:AMI",                 "value": inventory["ami_id"]})
                if inventory.get("key_name"):
                    props.append({"name": "aws:KeyName",             "value": inventory["key_name"]})
                if inventory.get("vpc_id"):
                    props.append({"name": "aws:VpcId",               "value": inventory["vpc_id"]})
                if inventory.get("subnet_id"):
                    props.append({"name": "aws:SubnetId",            "value": inventory["subnet_id"]})
                if inventory.get("private_ip"):
                    props.append({"name": "aws:PrivateIp",           "value": inventory["private_ip"]})
                if inventory.get("public_ip"):
                    props.append({"name": "aws:PublicIp",            "value": inventory["public_ip"]})
                if inventory.get("platform"):
                    props.append({"name": "aws:Platform",            "value": inventory["platform"]})
                if inventory.get("architecture"):
                    props.append({"name": "aws:Architecture",        "value": inventory["architecture"]})
                if inventory.get("monitoring"):
                    props.append({"name": "aws:Monitoring",          "value": inventory["monitoring"]})
                if inventory.get("launch_time"):
                    props.append({"name": "aws:LaunchTime",          "value": inventory["launch_time"]})
                if inventory.get("iam_instance_profile"):
                    props.append({"name": "aws:IAMInstanceProfile",  "value": inventory["iam_instance_profile"]})
                for i, sg_id in enumerate(inventory.get("security_groups", [])):
                    if sg_id:
                        props.append({"name": f"aws:SecurityGroup:{i}", "value": sg_id})
                for i, vol_id in enumerate(inventory.get("volumes", [])):
                    if vol_id:
                        props.append({"name": f"aws:Volume:{i}", "value": vol_id})

            elif rtype == "SecurityGroup":
                if inventory.get("group_name"):
                    props.append({"name": "aws:GroupName",         "value": inventory["group_name"]})
                if inventory.get("description"):
                    props.append({"name": "aws:Description",       "value": inventory["description"]})
                if inventory.get("vpc_id"):
                    props.append({"name": "aws:VpcId",             "value": inventory["vpc_id"]})
                props.append({"name": "aws:IngressRuleCount",      "value": str(inventory.get("ingress_rule_count", 0))})
                props.append({"name": "aws:EgressRuleCount",       "value": str(inventory.get("egress_rule_count", 0))})

            elif rtype == "KeyPair":
                if inventory.get("key_pair_id"):
                    props.append({"name": "aws:KeyPairId",  "value": inventory["key_pair_id"]})
                if inventory.get("key_type"):
                    props.append({"name": "aws:KeyType",    "value": inventory["key_type"]})
                if inventory.get("create_time"):
                    props.append({"name": "aws:CreateTime", "value": inventory["create_time"]})

            elif rtype == "Volume":
                if inventory.get("volume_type"):
                    props.append({"name": "aws:VolumeType",       "value": inventory["volume_type"]})
                if inventory.get("size_gb"):
                    props.append({"name": "aws:SizeGB",           "value": str(inventory["size_gb"])})
                if inventory.get("state"):
                    props.append({"name": "aws:State",            "value": inventory["state"]})
                if inventory.get("availability_zone"):
                    props.append({"name": "aws:AvailabilityZone", "value": inventory["availability_zone"]})
                props.append({"name": "aws:Encrypted",            "value": str(inventory.get("encrypted", False)).lower()})
                if inventory.get("iops"):
                    props.append({"name": "aws:IOPS",             "value": str(inventory["iops"])})
                if inventory.get("throughput"):
                    props.append({"name": "aws:ThroughputMBps",   "value": str(inventory["throughput"])})
                if inventory.get("create_time"):
                    props.append({"name": "aws:CreateTime",       "value": inventory["create_time"]})
                for i, instance_id in enumerate(inventory.get("attached_to", [])):
                    if instance_id:
                        props.append({"name": f"aws:AttachedTo:{i}", "value": instance_id})

    svc["properties"] = [p for p in props if p.get("value")]
    return svc


def to_cyclonedx_component(raw: dict) -> dict:
    props = [
        {"name": "aws:IAMPrincipalARN", "value": raw.get("arn", "")},
        {"name": "aws:IdentityType",    "value": raw.get("identity_type", "")},
        {"name": "aws:AccountId",       "value": raw.get("account_id", "")},
        {"name": "aws:EventSource",     "value": raw.get("event_source", "")},
    ]
    return {
        "type":       "application",
        "bom-ref":    _make_bom_ref(raw["kind"], raw["key"]),
        "name":       raw["name"],
        "properties": [p for p in props if p.get("value")],
    }


def build_dependency_graph(
    raw_components:      list[dict],
    raw_services:        list[dict],
    principal_resources: dict[str, set[str]],
) -> list[dict]:
    resource_ref_map = {r["key"]: _make_bom_ref("ec2_resource", r["key"]) for r in raw_services}

    deps: list[dict] = [
        {
            "ref":       "root-aws-account",
            "dependsOn": list(resource_ref_map.values()),
        }
    ]

    for raw in raw_components:
        accessed   = principal_resources.get(raw["key"], set())
        depends_on = [resource_ref_map[r] for r in accessed if r in resource_ref_map]
        if not depends_on:
            depends_on = ["root-aws-account"]
        deps.append({
            "ref":       _make_bom_ref(raw["kind"], raw["key"]),
            "dependsOn": depends_on,
        })

    return deps


def build_cyclonedx_bom(
    raw_components:      list[dict],
    raw_services:        list[dict],
    account_id:          str,
    source_files:        str,
    principal_resources: dict[str, set[str]],
    resource_inventory:  dict[str, dict],
) -> dict:
    return {
        "bomFormat":    "CycloneDX",
        "specVersion":  "1.6",
        "serialNumber": f"urn:uuid:{uuid.uuid4()}",
        "version":      1,
        "metadata": {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "tools": {
                "components": [
                    {
                        "type":    "application",
                        "name":    "aws-ec2-bom-generator",
                        "version": "1.0.0",
                    }
                ]
            },
            "component": {
                "type":    "application",
                "bom-ref": "root-aws-account",
                "name":    "AWS Account",
                "properties": [
                    {"name": "aws:AccountId",   "value": account_id},
                    {"name": "aws:SourceFiles", "value": source_files},
                ],
            },
        },
        "components":   [to_cyclonedx_component(r) for r in raw_components],
        "services":     [to_cyclonedx_service(r, resource_inventory.get(r["key"])) for r in raw_services],
        "dependencies": build_dependency_graph(raw_components, raw_services, principal_resources),
    }


def process_log_file(
    log_file:            Path,
    resource_dedup:      DeduplicatingSet,
    principal_dedup:     DeduplicatingSet,
    principal_resources: dict[str, set[str]],
    resource_inventory:  dict[str, dict],
) -> tuple[list[dict], list[dict], str]:
    raw_components: list[dict] = []
    raw_services:   list[dict] = []
    account_id = ""

    for event in stream_events(log_file):
        inv = extract_resource_inventory(event)
        if inv is not None:
            resource_inventory[inv["resource_key"]] = inv
            continue

        if not account_id:
            account_id = (event.get("userIdentity") or {}).get("accountId", "")

        resource = extract_ec2_resource(event)
        if resource and resource_dedup.add_if_new(resource["key"]):
            raw_services.append(resource)

        principal = extract_iam_principal(event)
        if principal:
            if resource:
                principal_resources.setdefault(principal["key"], set()).add(resource["key"])
            if principal_dedup.add_if_new(principal["key"]):
                raw_components.append(principal)

    return raw_components, raw_services, account_id


def main(target_file: Path | None = None) -> None:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)

    log_files = [target_file] if target_file else sorted(LOGS_DIR.glob("*.json"))
    if not log_files:
        print(f"No JSON files found in {LOGS_DIR}")
        return

    resource_dedup      = DeduplicatingSet()
    principal_dedup     = DeduplicatingSet()
    principal_resources: dict[str, set[str]] = {}
    resource_inventory:  dict[str, dict]     = {}

    all_components: list[dict] = []
    all_services:   list[dict] = []
    account_id = ""

    for log_file in log_files:
        print(f"Processing {log_file.name} ...")
        comps, svcs, aid = process_log_file(
            log_file, resource_dedup, principal_dedup, principal_resources, resource_inventory
        )
        all_components.extend(comps)
        all_services.extend(svcs)
        if not account_id and aid:
            account_id = aid
        print(f"  {len(comps)} new principals, {len(svcs)} new resources")

    source_files = ", ".join(f.name for f in log_files)
    bom = build_cyclonedx_bom(
        all_components, all_services, account_id, source_files,
        principal_resources, resource_inventory
    )

    timestamp   = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    output_path = REPORT_DIR / f"bom_{timestamp}.json"
    output_path.write_text(json.dumps(bom, indent=2), encoding="utf-8")

    print(f"\nBOM report saved to: {output_path}")
    print(f"Total: {len(all_components)} principals, {len(all_services)} resources")


if __name__ == "__main__":
    main()