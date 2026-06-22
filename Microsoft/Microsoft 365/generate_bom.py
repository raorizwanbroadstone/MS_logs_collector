"""
generate_bom.py

Streams every JSON file in logs/, extracts BOM-relevant entities (service
principals, client applications, M365 workloads), deduplicates them with a
Bloom filter backed by an exact set, and writes a CycloneDX 1.6 BOM JSON
report to report/.

Dependencies: mmh3, bitarray, ijson  (pip install mmh3 bitarray ijson)
"""

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



# Bloom filter + deduplication layer
class BloomFilter:
    """
    Probabilistic membership structure using MurmurHash3 double-hashing over a
    bitarray. Sized at init for a given capacity and false positive rate.
    Guarantees no false negatives; callers must resolve false positives externally.
    """

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
    """
    Combines BloomFilter (fast definite-miss path) with an exact backing set to
    guarantee zero duplicate insertions regardless of false positive rate.
    The Bloom filter avoids a hash-set lookup for every key the set has never seen.
    """

    def __init__(self, capacity: int = BLOOM_CAPACITY, fpr: float = BLOOM_FPR):
        self._bloom = BloomFilter(capacity, fpr)
        self._seen: set[str] = set()

    def add_if_new(self, key: str) -> bool:
        """Returns True and records key if it has never been seen; False otherwise."""
        if self._bloom.might_contain(key) and key in self._seen:
            return False
        self._bloom.add(key)
        self._seen.add(key)
        return True

    def __len__(self) -> int:
        return len(self._seen)



# Log streaming
def stream_events(log_file: Path):
    """Yields each top-level JSON object from a large array file using ijson."""
    with log_file.open("rb") as fh:
        yield from ijson.items(fh, "item")



# Field parsers for embedded JSON strings
def parse_embedded_json_list(raw: str) -> list[str]:
    """
    ModifiedProperties stores values as JSON-encoded strings like '["Azure Managed HSM RP"]'.
    Parses them and returns a flat list of strings, falling back to a single-element list.
    """
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return [str(x) for x in parsed if x is not None]
        return [str(parsed)]
    except (json.JSONDecodeError, TypeError):
        return [raw.strip()] if raw and raw.strip() else []


def read_extended_properties(event: dict) -> tuple[str, dict]:
    """
    Reads ExtendedProperties to return (extendedAuditEventCategory, additionalDetails dict).
    Both values are empty if the fields are absent or malformed.
    """
    category = ""
    details: dict = {}
    for prop in event.get("ExtendedProperties", []):
        name = prop.get("Name", "")
        if name == "extendedAuditEventCategory":
            category = prop.get("Value", "")
        elif name == "additionalDetails":
            try:
                details = json.loads(prop.get("Value", "{}"))
            except (json.JSONDecodeError, TypeError):
                pass
    return category, details



# Entity extractors — each returns a typed dict or None
def extract_service_principal(event: dict) -> dict | None:
    """
    Identifies AAD service principal provisioning events by checking for category
    'ServicePrincipal' in ExtendedProperties. Pulls AppId from additionalDetails
    and DisplayName from ModifiedProperties. Returns None for non-SP events.
    """
    category, details = read_extended_properties(event)
    if category != "ServicePrincipal":
        return None

    app_id = details.get("AppId") or event.get("ObjectId", "")
    if not app_id:
        return None

    modified = {p["Name"]: p.get("NewValue", "") for p in event.get("ModifiedProperties", [])}
    names = parse_embedded_json_list(modified.get("DisplayName", ""))
    display_name = names[0] if names else app_id

    return {
        "kind":             "service_principal",
        "key":              app_id,
        "name":             display_name,
        "app_id":           app_id,
        "owner_org_id":     details.get("AppOwnerOrganizationId", ""),
        "provisioning":     details.get("ServicePrincipalProvisioningType", ""),
        "operation":        event.get("Operation", ""),
        "result_status":    event.get("ResultStatus", ""),
        "workload":         event.get("Workload", ""),
    }


def extract_client_app(event: dict) -> dict | None:
    """
    Extracts a consuming application from AppAccessContext if present.
    ClientAppId is used as the unique key; ClientAppName as the display name.
    Returns None for events without AppAccessContext.
    """
    ctx = event.get("AppAccessContext")
    if not isinstance(ctx, dict):
        return None
    client_id = ctx.get("ClientAppId", "")
    if not client_id:
        return None

    return {
        "kind":          "client_app",
        "key":           client_id,
        "name":          ctx.get("ClientAppName") or client_id,
        "client_app_id": client_id,
        "api_id":        ctx.get("APIId", ""),
        "workload":      event.get("Workload", ""),
    }


def extract_workload(event: dict) -> dict | None:
    """
    Captures each unique M365 workload (AzureActiveDirectory, MicrosoftTeams, etc.)
    as a CycloneDX service entry. RecordType is retained for the first encounter.
    Returns None when the Workload field is absent.
    """
    workload = event.get("Workload", "")
    if not workload:
        return None

    return {
        "kind":        "workload",
        "key":         workload,
        "name":        workload,
        "record_type": str(event.get("RecordType", "")),
    }



# CycloneDX 1.6 serializers
def to_cyclonedx_component(raw: dict) -> dict:
    """
    Converts a raw service_principal or client_app dict to a CycloneDX 1.6
    component object (type: application). All source fields are stored as
    namespaced properties under m365:.
    """
    bom_ref = f"{raw['kind']}-{raw['key']}"
    props: list[dict] = []

    field_map: dict[str, dict[str, str]] = {
        "service_principal": {
            "app_id":        "m365:AppId",
            "owner_org_id":  "m365:AppOwnerOrganizationId",
            "provisioning":  "m365:ServicePrincipalProvisioningType",
            "operation":     "m365:Operation",
            "result_status": "m365:ResultStatus",
            "workload":      "m365:Workload",
        },
        "client_app": {
            "client_app_id": "m365:ClientAppId",
            "api_id":        "m365:APIId",
            "workload":      "m365:Workload",
        },
    }

    for field, cdx_name in field_map.get(raw["kind"], {}).items():
        value = raw.get(field, "")
        if value:
            props.append({"name": cdx_name, "value": value})

    component: dict = {
        "type":    "application",
        "bom-ref": bom_ref,
        "name":    raw["name"],
    }
    if props:
        component["properties"] = props
    return component


def to_cyclonedx_service(raw: dict) -> dict:
    """
    Converts a raw workload dict to a CycloneDX 1.6 service object.
    authenticated is set True because all M365 workloads require OAuth2.
    """
    svc: dict = {
        "bom-ref":       f"workload-{raw['name']}",
        "name":          raw["name"],
        "authenticated": True,
    }
    if raw.get("record_type"):
        svc["properties"] = [{"name": "m365:RecordType", "value": raw["record_type"]}]
    return svc


def build_dependency_graph(
    raw_components: list[dict],
    raw_services: list[dict],
) -> list[dict]:
    """
    Builds the CycloneDX dependencies section.
    The root tenant node depends on every workload.
    Each component depends on the workload it was observed in.
    """
    workload_refs = {r["name"]: f"workload-{r['name']}" for r in raw_services}

    deps: list[dict] = [
        {
            "ref":       "root-m365-tenant",
            "dependsOn": list(workload_refs.values()),
        }
    ]

    for raw in raw_components:
        wl_ref = workload_refs.get(raw.get("workload", ""))
        if wl_ref:
            deps.append({
                "ref":       f"{raw['kind']}-{raw['key']}",
                "dependsOn": [wl_ref],
            })

    return deps


def build_cyclonedx_bom(
    raw_components: list[dict],
    raw_services: list[dict],
    org_id: str,
    source_files: str,
) -> dict:
    """
    Assembles the complete CycloneDX 1.6 BOM document from extracted data.
    Includes metadata (tool provenance, root component, org ID), components,
    services, and a full dependency graph.
    """
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
                        "name":    "ms-logs-bom-generator",
                        "version": "1.0.0",
                    }
                ]
            },
            "component": {
                "type":    "application",
                "bom-ref": "root-m365-tenant",
                "name":    "Microsoft 365 Tenant",
                "properties": [
                    {"name": "m365:OrganizationId", "value": org_id},
                    {"name": "m365:SourceFiles",    "value": source_files},
                ],
            },
        },
        "components":   [to_cyclonedx_component(r) for r in raw_components],
        "services":     [to_cyclonedx_service(r) for r in raw_services],
        "dependencies": build_dependency_graph(raw_components, raw_services),
    }



# Per-file processor
def process_log_file(
    log_file: Path,
    sp_dedup:       DeduplicatingSet,
    client_dedup:   DeduplicatingSet,
    workload_dedup: DeduplicatingSet,
) -> tuple[list[dict], list[dict], str]:
    """
    Streams one log file and appends newly seen components and workloads.
    The three dedup sets are shared across files so cross-file duplicates are
    caught. Returns (raw_components, raw_services, first_seen_org_id).
    """
    raw_components: list[dict] = []
    raw_services:   list[dict] = []
    org_id = ""

    for event in stream_events(log_file):
        if not org_id:
            org_id = event.get("OrganizationId", "")

        sp = extract_service_principal(event)
        if sp and sp_dedup.add_if_new(sp["key"]):
            raw_components.append(sp)

        ca = extract_client_app(event)
        if ca and client_dedup.add_if_new(ca["key"]):
            raw_components.append(ca)

        wl = extract_workload(event)
        if wl and workload_dedup.add_if_new(wl["key"]):
            raw_services.append(wl)

    return raw_components, raw_services, org_id



# Entry point
def main(target_file: Path | None = None) -> None:
    """
    Processes log files and writes a CycloneDX 1.6 BOM to report/.
    When target_file is given, only that file is processed — used when called
    directly from fetch_m365_logs.py to scope the BOM to the freshly fetched log.
    When called standalone (no argument), all JSON files in logs/ are processed.
    """
    REPORT_DIR.mkdir(parents=True, exist_ok=True)

    log_files = [target_file] if target_file else sorted(LOGS_DIR.glob("*.json"))
    if not log_files:
        print(f"No JSON files found in {LOGS_DIR}")
        return

    sp_dedup       = DeduplicatingSet()
    client_dedup   = DeduplicatingSet()
    workload_dedup = DeduplicatingSet()

    all_components: list[dict] = []
    all_services:   list[dict] = []
    org_id = ""

    for log_file in log_files:
        print(f"Processing {log_file.name} ...")
        comps, svcs, fid = process_log_file(log_file, sp_dedup, client_dedup, workload_dedup)
        all_components.extend(comps)
        all_services.extend(svcs)
        if not org_id:
            org_id = fid
        print(f"  {len(comps)} new components, {len(svcs)} new workloads")

    source_files = ", ".join(f.name for f in log_files)
    bom = build_cyclonedx_bom(all_components, all_services, org_id, source_files)

    timestamp   = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    output_path = REPORT_DIR / f"bom_{timestamp}.json"
    output_path.write_text(json.dumps(bom, indent=2), encoding="utf-8")

    print(f"\nReport : {output_path}")
    print(f"Total  : {len(all_components)} components, {len(all_services)} workloads")


if __name__ == "__main__":
    main()
