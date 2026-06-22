"""
generate_bom.py

Streams Azure Storage log files from logs/, extracts BOM-relevant entities
(storage accounts, client applications, resource providers, Log Analytics
workspaces), deduplicates them with a Bloom filter backed by an exact set,
and writes a CycloneDX 1.6 BOM JSON report to report/.

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
def stream_storage_accounts(log_file: Path):
    """
    Yields each storage account object from the nested storage_accounts array.
    The log format is { storage_accounts: [...] }, not a top-level array, so the
    ijson prefix must be "storage_accounts.item".
    """
    with log_file.open("rb") as fh:
        yield from ijson.items(fh, "storage_accounts.item")


# Entity extractors
def extract_storage_account(account: dict) -> dict | None:
    """
    Builds a storage account component from the account-level metadata block.
    storage_account_id (the full ARM resource path) is the unique key.
    """
    account_id = account.get("storage_account_id", "")
    if not account_id:
        return None

    return {
        "kind":               "storage_account",
        "key":                account_id,
        "name":               account.get("storage_account_name") or account_id,
        "account_id":         account_id,
        "account_name":       account.get("storage_account_name", ""),
        "subscription_id":    account.get("subscription_id", ""),
        "resource_group":     account.get("resource_group", ""),
        "location":           account.get("location", ""),
        "diagnostic_enabled": str(account.get("diagnostic_logging_enabled", False)),
        "workload":           "Microsoft.Storage",
    }


def extract_client_apps(account: dict) -> list[dict]:
    """
    Scans activity_logs for Azure AD application IDs from JWT claims.
    claims.appid is the OAuth2 client that performed each management-plane action.
    """
    results = []
    for event in account.get("activity_logs", []):
        if not isinstance(event, dict):
            continue
        app_id = event.get("claims", {}).get("appid", "")
        if app_id:
            provider = event.get("resource_provider_name", {})
            results.append({
                "kind":            "client_app",
                "key":             app_id,
                "name":            app_id,
                "app_id":          app_id,
                "subscription_id": event.get("subscription_id", ""),
                "workload":        provider.get("value", "") if isinstance(provider, dict) else "",
            })
    return results


def extract_resource_providers(account: dict) -> list[dict]:
    """
    Extracts unique Azure resource providers (e.g., Microsoft.Storage) from
    activity_logs. These are the platform services being called by client apps.
    """
    results = []
    for event in account.get("activity_logs", []):
        if not isinstance(event, dict):
            continue
        provider = event.get("resource_provider_name", {})
        if isinstance(provider, dict):
            name = provider.get("value", "")
            if name:
                results.append({
                    "kind": "resource_provider",
                    "key":  name,
                    "name": name,
                })
    return results


def extract_log_analytics_workspaces(account: dict) -> list[dict]:
    """
    Extracts Log Analytics workspace IDs from storage_diagnostic_logs keys.
    Each key is a workspace ID that receives diagnostic telemetry from the account.
    """
    results = []
    diag_logs = account.get("storage_diagnostic_logs", {})
    if not isinstance(diag_logs, dict):
        return results
    for ws_id, tables in diag_logs.items():
        if isinstance(tables, dict) and ws_id not in ("status", "error"):
            results.append({
                "kind":  "log_analytics_workspace",
                "key":   ws_id,
                "name":  f"Log Analytics Workspace ({ws_id[:8]}...)",
                "ws_id": ws_id,
            })
    return results


def capture_tenant_id(account: dict) -> str:
    """
    Reads tenant_id from the first valid activity log event within the account.
    Returns an empty string if no events are present.
    """
    for event in account.get("activity_logs", []):
        if isinstance(event, dict):
            tid = event.get("tenant_id", "")
            if tid:
                return tid
    return ""


# CycloneDX 1.6 serializers
def to_cyclonedx_component(raw: dict) -> dict:
    """
    Maps a raw storage_account or client_app dict to a CycloneDX 1.6 component
    (type: application). Azure-specific fields are stored as azure: properties.
    """
    bom_ref = f"{raw['kind']}-{raw['key']}"
    props: list[dict] = []

    field_map: dict[str, dict[str, str]] = {
        "storage_account": {
            "account_id":         "azure:StorageAccountId",
            "account_name":       "azure:StorageAccountName",
            "subscription_id":    "azure:SubscriptionId",
            "resource_group":     "azure:ResourceGroup",
            "location":           "azure:Location",
            "diagnostic_enabled": "azure:DiagnosticLoggingEnabled",
        },
        "client_app": {
            "app_id":          "azure:AppId",
            "subscription_id": "azure:SubscriptionId",
            "workload":        "azure:ResourceProvider",
        },
    }

    for field, cdx_name in field_map.get(raw["kind"], {}).items():
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
    Maps a raw resource_provider or log_analytics_workspace dict to a CycloneDX
    1.6 service object. authenticated is True as all Azure services require OAuth2.
    """
    bom_ref = f"{raw['kind']}-{raw['key']}"
    svc: dict = {
        "bom-ref":       bom_ref,
        "name":          raw["name"],
        "authenticated": True,
    }
    if raw["kind"] == "log_analytics_workspace" and raw.get("ws_id"):
        svc["properties"] = [{"name": "azure:WorkspaceId", "value": raw["ws_id"]}]
    return svc


def build_dependency_graph(
    raw_components: list[dict],
    raw_services: list[dict],
) -> list[dict]:
    """
    Builds the CycloneDX dependencies section.
    Root Azure tenant depends on every resource provider and workspace.
    Storage accounts and client apps depend on the resource provider they interact with.
    """
    service_refs = {r["key"]: f"{r['kind']}-{r['key']}" for r in raw_services}

    deps: list[dict] = [
        {
            "ref":       "root-azure-tenant",
            "dependsOn": list(service_refs.values()),
        }
    ]

    for raw in raw_components:
        workload = raw.get("workload", "")
        ref = service_refs.get(workload)
        if ref:
            deps.append({
                "ref":       f"{raw['kind']}-{raw['key']}",
                "dependsOn": [ref],
            })

    return deps


def build_cyclonedx_bom(
    raw_components: list[dict],
    raw_services: list[dict],
    tenant_id: str,
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
                        "name":    "azure-storage-bom-generator",
                        "version": "1.0.0",
                    }
                ]
            },
            "component": {
                "type":    "platform",
                "bom-ref": "root-azure-tenant",
                "name":    "Azure Tenant",
                "properties": [
                    {"name": "azure:TenantId",    "value": tenant_id},
                    {"name": "azure:SourceFiles", "value": source_files},
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
    account_dedup:   DeduplicatingSet,
    client_dedup:    DeduplicatingSet,
    provider_dedup:  DeduplicatingSet,
    workspace_dedup: DeduplicatingSet,
) -> tuple[list[dict], list[dict], str]:
    """
    Streams one log file, iterates over each storage account and its nested
    activity_logs, and collects new components and services.
    All dedup sets are shared across calls to catch cross-file duplicates.
    Returns (raw_components, raw_services, tenant_id).
    """
    raw_components: list[dict] = []
    raw_services:   list[dict] = []
    tenant_id = ""

    for account in stream_storage_accounts(log_file):
        if not tenant_id:
            tenant_id = capture_tenant_id(account)

        sa = extract_storage_account(account)
        if sa and account_dedup.add_if_new(sa["key"]):
            raw_components.append(sa)

        for ca in extract_client_apps(account):
            if client_dedup.add_if_new(ca["key"]):
                raw_components.append(ca)

        for rp in extract_resource_providers(account):
            if provider_dedup.add_if_new(rp["key"]):
                raw_services.append(rp)

        for ws in extract_log_analytics_workspaces(account):
            if workspace_dedup.add_if_new(ws["key"]):
                raw_services.append(ws)

    return raw_components, raw_services, tenant_id


# Entry point
def main(target_file: Path | None = None) -> None:
    """
    Entry point. Processes target_file if given (called from fetch_azure_storage_logs.py),
    otherwise processes all JSON files in logs/. Builds a CycloneDX 1.6 BOM and
    writes it to report/.
    """
    REPORT_DIR.mkdir(parents=True, exist_ok=True)

    log_files = [target_file] if target_file else sorted(LOGS_DIR.glob("*.json"))
    if not log_files:
        print(f"No JSON files found in {LOGS_DIR}")
        return

    account_dedup   = DeduplicatingSet()
    client_dedup    = DeduplicatingSet()
    provider_dedup  = DeduplicatingSet()
    workspace_dedup = DeduplicatingSet()

    all_components: list[dict] = []
    all_services:   list[dict] = []
    tenant_id = ""

    for log_file in log_files:
        print(f"Processing {log_file.name} ...")
        comps, svcs, fid = process_log_file(
            log_file, account_dedup, client_dedup, provider_dedup, workspace_dedup
        )
        all_components.extend(comps)
        all_services.extend(svcs)
        if not tenant_id:
            tenant_id = fid
        print(f"  {len(comps)} new components, {len(svcs)} new services")

    source_files = ", ".join(f.name for f in log_files)
    bom = build_cyclonedx_bom(all_components, all_services, tenant_id, source_files)

    timestamp   = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    output_path = REPORT_DIR / f"bom_{timestamp}.json"
    output_path.write_text(json.dumps(bom, indent=2), encoding="utf-8")

    print(f"\nReport : {output_path}")
    print(f"Total  : {len(all_components)} components, {len(all_services)} services")


if __name__ == "__main__":
    main()
