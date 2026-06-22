"""
generate_bom.py

Reads Azure OpenAI diagnostic log files from azure_diagnostic_logs/, extracts
BOM-relevant entities (Cognitive Services resources, resource providers, log
tables, Log Analytics workspace), deduplicates them with a Bloom filter backed
by an exact set, and writes a CycloneDX 1.6 BOM JSON report to report/.

Files are small (~11KB each) so each is loaded fully with json.load().
The Bloom filter + exact set guarantee zero duplicate components across all files.

Dependencies: mmh3, bitarray  (pip install mmh3 bitarray)
"""

import json
import math
import uuid
from datetime import datetime, timezone
from pathlib import Path

import mmh3
from bitarray import bitarray

SCRIPT_DIR = Path(__file__).parent
LOGS_DIR   = SCRIPT_DIR / "logs"
REPORT_DIR = SCRIPT_DIR / "report"

BLOOM_CAPACITY = 500_000
BLOOM_FPR      = 0.0001


# ---------------------------------------------------------------------------
# Bloom filter + deduplication layer
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# File loading
# ---------------------------------------------------------------------------

def load_log_file(log_file: Path) -> dict:
    """
    Loads the entire JSON file. Azure OpenAI diagnostic files are small (~11KB),
    and their structure requires reading from multiple top-level keys (workspaceId,
    results.AzureDiagnostics_Sample, results.Table_Counts) in one pass.
    """
    with log_file.open("r", encoding="utf-8") as fh:
        return json.load(fh)


# ---------------------------------------------------------------------------
# Entity extractors
# ---------------------------------------------------------------------------

def extract_cognitive_services_resources(data: dict) -> list[dict]:
    """
    Extracts Cognitive Services / Azure OpenAI resource components from
    AzureDiagnostics_Sample records. _ResourceId (lowercase ARM path) is the
    unique key; it is stable across runs for the same resource.
    """
    results = []
    for record in data.get("results", {}).get("AzureDiagnostics_Sample", []):
        if not isinstance(record, dict):
            continue
        resource_id = record.get("_ResourceId") or record.get("ResourceId", "")
        if not resource_id:
            continue
        resource_id = resource_id.lower()
        results.append({
            "kind":              "cognitive_services_resource",
            "key":               resource_id,
            "name":              record.get("Resource", resource_id.split("/")[-1]),
            "resource_id":       resource_id,
            "resource_provider": record.get("ResourceProvider", ""),
            "resource_type":     record.get("ResourceType", ""),
            "resource_group":    record.get("ResourceGroup", ""),
            "subscription_id":   record.get("SubscriptionId", ""),
            "operation_name":    record.get("OperationName", ""),
            "category":          record.get("Category", ""),
            "workload":          record.get("ResourceProvider", ""),
        })
    return results


def extract_resource_providers(data: dict) -> list[dict]:
    """
    Extracts unique resource providers (e.g., MICROSOFT.COGNITIVESERVICES) from
    AzureDiagnostics_Sample records. Normalised to uppercase for consistency.
    """
    results = []
    for record in data.get("results", {}).get("AzureDiagnostics_Sample", []):
        if not isinstance(record, dict):
            continue
        provider = record.get("ResourceProvider", "").upper()
        if provider:
            results.append({
                "kind": "resource_provider",
                "key":  provider,
                "name": provider,
            })
    return results


def extract_log_tables(data: dict) -> list[dict]:
    """
    Extracts Log Analytics table names from Table_Counts query results.
    Each table is a service that stores diagnostic telemetry in the workspace.
    """
    results = []
    for record in data.get("results", {}).get("Table_Counts", []):
        if not isinstance(record, dict):
            continue
        table = record.get("$table", "")
        count = record.get("Count", 0)
        if table:
            results.append({
                "kind":        "log_table",
                "key":         table,
                "name":        table,
                "record_count": str(count),
            })
    return results


def extract_workspace(data: dict) -> dict | None:
    """
    Extracts the Log Analytics workspace as a service from the top-level
    workspaceId field. Returns None if the field is absent.
    """
    ws_id = data.get("workspaceId", "")
    if not ws_id:
        return None
    return {
        "kind":  "log_analytics_workspace",
        "key":   ws_id,
        "name":  f"Log Analytics Workspace ({ws_id[:8]}...)",
        "ws_id": ws_id,
    }


# ---------------------------------------------------------------------------
# CycloneDX 1.6 serializers
# ---------------------------------------------------------------------------

def to_cyclonedx_component(raw: dict) -> dict:
    """
    Maps a raw cognitive_services_resource dict to a CycloneDX 1.6 component
    (type: application). Azure-specific fields are stored as azure: properties.
    """
    bom_ref = f"{raw['kind']}-{raw['key']}"
    props: list[dict] = []

    for field, cdx_name in [
        ("resource_id",       "azure:ResourceId"),
        ("resource_provider", "azure:ResourceProvider"),
        ("resource_type",     "azure:ResourceType"),
        ("resource_group",    "azure:ResourceGroup"),
        ("subscription_id",   "azure:SubscriptionId"),
        ("operation_name",    "azure:OperationName"),
        ("category",          "azure:Category"),
    ]:
        value = raw.get(field, "")
        if value:
            props.append({"name": cdx_name, "value": value})

    comp: dict = {
        "type":    "application",
        "bom-ref": bom_ref,
        "name":    raw["name"],
    }
    if props:
        comp["properties"] = props
    return comp


def to_cyclonedx_service(raw: dict) -> dict:
    """
    Maps a raw resource_provider, log_table, or log_analytics_workspace dict
    to a CycloneDX 1.6 service object. authenticated is True for all Azure services.
    """
    bom_ref = f"{raw['kind']}-{raw['key']}"
    svc: dict = {
        "bom-ref":       bom_ref,
        "name":          raw["name"],
        "authenticated": True,
    }

    props = []
    if raw["kind"] == "log_analytics_workspace" and raw.get("ws_id"):
        props.append({"name": "azure:WorkspaceId", "value": raw["ws_id"]})
    elif raw["kind"] == "log_table" and raw.get("record_count"):
        props.append({"name": "azure:RecordCount", "value": raw["record_count"]})

    if props:
        svc["properties"] = props
    return svc


def build_dependency_graph(
    raw_components: list[dict],
    raw_services: list[dict],
) -> list[dict]:
    """
    Builds the CycloneDX dependencies section.
    Root Azure tenant depends on all services (providers, tables, workspace).
    Cognitive Services resources depend on their resource provider.
    Log tables depend on the Log Analytics workspace.
    """
    service_refs = {r["key"]: f"{r['kind']}-{r['key']}" for r in raw_services}
    workspace_refs = [f"{r['kind']}-{r['key']}" for r in raw_services if r["kind"] == "log_analytics_workspace"]

    deps: list[dict] = [
        {
            "ref":       "root-azure-tenant",
            "dependsOn": list(service_refs.values()),
        }
    ]

    for raw in raw_components:
        workload = raw.get("workload", "").upper()
        ref = service_refs.get(workload)
        if ref:
            deps.append({
                "ref":       f"{raw['kind']}-{raw['key']}",
                "dependsOn": [ref],
            })

    for raw in raw_services:
        if raw["kind"] == "log_table" and workspace_refs:
            deps.append({
                "ref":       f"{raw['kind']}-{raw['key']}",
                "dependsOn": workspace_refs,
            })

    return deps


def build_cyclonedx_bom(
    raw_components: list[dict],
    raw_services: list[dict],
    workspace_id: str,
    source_files: str,
) -> dict:
    """
    Assembles the full CycloneDX 1.6 BOM document from extracted entities.
    Includes metadata (tool provenance, root tenant), components, services,
    and a full dependency graph.
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
                        "name":    "azure-openai-bom-generator",
                        "version": "1.0.0",
                    }
                ]
            },
            "component": {
                "type":    "platform",
                "bom-ref": "root-azure-tenant",
                "name":    "Azure Tenant",
                "properties": [
                    {"name": "azure:WorkspaceId", "value": workspace_id},
                    {"name": "azure:SourceFiles", "value": source_files},
                ],
            },
        },
        "components":   [to_cyclonedx_component(r) for r in raw_components],
        "services":     [to_cyclonedx_service(r) for r in raw_services],
        "dependencies": build_dependency_graph(raw_components, raw_services),
    }


# ---------------------------------------------------------------------------
# Per-file processor
# ---------------------------------------------------------------------------

def process_log_file(
    log_file: Path,
    resource_dedup:  DeduplicatingSet,
    provider_dedup:  DeduplicatingSet,
    table_dedup:     DeduplicatingSet,
    workspace_dedup: DeduplicatingSet,
) -> tuple[list[dict], list[dict], str]:
    """
    Loads one log file and extracts all new components and services.
    All dedup sets are shared across calls to catch cross-file duplicates.
    Returns (raw_components, raw_services, workspace_id).
    """
    data = load_log_file(log_file)
    raw_components: list[dict] = []
    raw_services:   list[dict] = []

    for res in extract_cognitive_services_resources(data):
        if resource_dedup.add_if_new(res["key"]):
            raw_components.append(res)

    for rp in extract_resource_providers(data):
        if provider_dedup.add_if_new(rp["key"]):
            raw_services.append(rp)

    for tbl in extract_log_tables(data):
        if table_dedup.add_if_new(tbl["key"]):
            raw_services.append(tbl)

    ws = extract_workspace(data)
    workspace_id = ws["ws_id"] if ws else ""
    if ws and workspace_dedup.add_if_new(ws["key"]):
        raw_services.append(ws)

    return raw_components, raw_services, workspace_id


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(target_file: Path | None = None) -> None:
    """
    Entry point. Processes target_file if given (called from fetch_azure_diagnostic_logs.py),
    otherwise processes all JSON files in azure_diagnostic_logs/. Builds a CycloneDX 1.6
    BOM and writes it to report/.
    """
    REPORT_DIR.mkdir(parents=True, exist_ok=True)

    log_files = [target_file] if target_file else sorted(LOGS_DIR.glob("*.json"))
    if not log_files:
        print(f"No JSON files found in {LOGS_DIR}")
        return

    resource_dedup  = DeduplicatingSet()
    provider_dedup  = DeduplicatingSet()
    table_dedup     = DeduplicatingSet()
    workspace_dedup = DeduplicatingSet()

    all_components: list[dict] = []
    all_services:   list[dict] = []
    workspace_id = ""

    for log_file in log_files:
        print(f"Processing {log_file.name} ...")
        comps, svcs, fid = process_log_file(
            log_file, resource_dedup, provider_dedup, table_dedup, workspace_dedup
        )
        all_components.extend(comps)
        all_services.extend(svcs)
        if not workspace_id:
            workspace_id = fid
        print(f"  {len(comps)} new components, {len(svcs)} new services")

    source_files = ", ".join(f.name for f in log_files)
    bom = build_cyclonedx_bom(all_components, all_services, workspace_id, source_files)

    timestamp   = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    output_path = REPORT_DIR / f"bom_{timestamp}.json"
    output_path.write_text(json.dumps(bom, indent=2), encoding="utf-8")

    print(f"\nReport : {output_path}")
    print(f"Total  : {len(all_components)} components, {len(all_services)} services")


if __name__ == "__main__":
    main()
