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
LOGS_DIR = SCRIPT_DIR / "logs"
REPORT_DIR = SCRIPT_DIR / "report"

BLOOM_CAPACITY = 500_000
BLOOM_FPR = 0.0001


# Bloom filter and deduplication layer

class BloomFilter:
    """
    Probabilistic membership structure using MurmurHash3 double-hashing over a
    bitarray. Sized at init for a given capacity and false positive rate.
    Guarantees no false negatives; callers must resolve false positives externally.
    """

    def __init__(self, capacity: int, fpr: float):
        bit_array_size = math.ceil(-(capacity * math.log(fpr)) / (math.log(2) ** 2))
        hash_count = max(1, round((bit_array_size / capacity) * math.log(2)))
        self.bit_array_size = bit_array_size
        self.hash_count = hash_count
        self.bits = bitarray(bit_array_size)
        self.bits.setall(0)

    def compute_positions(self, key: str) -> list[int]:
        primary_hash = mmh3.hash(key, seed=0, signed=False)
        secondary_hash = mmh3.hash(key, seed=1, signed=False)
        return [(primary_hash + i * secondary_hash) % self.bit_array_size for i in range(self.hash_count)]

    def add(self, key: str) -> None:
        for position in self.compute_positions(key):
            self.bits[position] = 1

    def might_contain(self, key: str) -> bool:
        return all(self.bits[position] for position in self.compute_positions(key))


class DeduplicatingSet:
    """
    Combines BloomFilter (fast definite-miss path) with an exact backing set to
    guarantee zero duplicate insertions regardless of false positive rate.
    The Bloom filter avoids a hash-set lookup for every key the set has never seen.
    """

    def __init__(self, capacity: int = BLOOM_CAPACITY, fpr: float = BLOOM_FPR):
        self.bloom_filter = BloomFilter(capacity, fpr)
        self.seen_keys: set[str] = set()

    def add_if_new(self, key: str) -> bool:
        """Returns True and records the key if it has never been seen; False otherwise."""
        if self.bloom_filter.might_contain(key) and key in self.seen_keys:
            return False
        self.bloom_filter.add(key)
        self.seen_keys.add(key)
        return True

    def __len__(self) -> int:
        return len(self.seen_keys)


# Log streaming

def stream_storage_accounts(log_file: Path):
    """
    Yields each storage account object from the nested storage_accounts array.
    The log format is { storage_accounts: [...] }, not a top-level array, so the
    ijson prefix must be "storage_accounts.item".
    """
    with log_file.open("rb") as file_handle:
        yield from ijson.items(file_handle, "storage_accounts.item")


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

def to_cyclonedx_component(entity: dict) -> dict:
    """
    Maps a raw storage_account or client_app dict to a CycloneDX 1.6 component
    (type: application). Azure-specific fields are stored as azure: properties.
    """
    bom_ref = f"{entity['kind']}-{entity['key']}"
    properties: list[dict] = []

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

    for field, cdx_name in field_map.get(entity["kind"], {}).items():
        value = entity.get(field, "")
        if value:
            properties.append({"name": cdx_name, "value": value})

    component: dict = {
        "type":    "application",
        "bom-ref": bom_ref,
        "name":    entity["name"],
    }
    if properties:
        component["properties"] = properties
    return component


def to_cyclonedx_service(entity: dict) -> dict:
    """
    Maps a raw resource_provider or log_analytics_workspace dict to a CycloneDX
    1.6 service object. authenticated is True as all Azure services require OAuth2.
    """
    bom_ref = f"{entity['kind']}-{entity['key']}"
    service_entry: dict = {
        "bom-ref":       bom_ref,
        "name":          entity["name"],
        "authenticated": True,
    }
    if entity["kind"] == "log_analytics_workspace" and entity.get("ws_id"):
        service_entry["properties"] = [{"name": "azure:WorkspaceId", "value": entity["ws_id"]}]
    return service_entry


def build_dependency_graph(
    raw_components: list[dict],
    raw_services: list[dict],
) -> list[dict]:
    """
    Builds the CycloneDX dependencies section.
    Root Azure tenant depends on every resource provider and workspace.
    Storage accounts and client apps depend on the resource provider they interact with.
    """
    service_refs = {entity["key"]: f"{entity['kind']}-{entity['key']}" for entity in raw_services}

    dependencies: list[dict] = [
        {
            "ref":       "root-azure-tenant",
            "dependsOn": list(service_refs.values()),
        }
    ]

    for entity in raw_components:
        workload_ref = service_refs.get(entity.get("workload", ""))
        if workload_ref:
            dependencies.append({
                "ref":       f"{entity['kind']}-{entity['key']}",
                "dependsOn": [workload_ref],
            })

    return dependencies


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
        "components":   [to_cyclonedx_component(entity) for entity in raw_components],
        "services":     [to_cyclonedx_service(entity) for entity in raw_services],
        "dependencies": build_dependency_graph(raw_components, raw_services),
    }


# Per-file processor

def process_log_file(
    log_file: Path,
    storage_account_dedup: DeduplicatingSet,
    client_app_dedup: DeduplicatingSet,
    resource_provider_dedup: DeduplicatingSet,
    workspace_dedup: DeduplicatingSet,
) -> tuple[list[dict], list[dict], str]:
    """
    Streams one log file, iterates over each storage account and its nested
    activity_logs, and collects new components and services.
    All dedup sets are shared across calls to catch cross-file duplicates.
    Returns (raw_components, raw_services, tenant_id).
    """
    raw_components: list[dict] = []
    raw_services: list[dict] = []
    tenant_id = ""

    for account in stream_storage_accounts(log_file):
        if not tenant_id:
            tenant_id = capture_tenant_id(account)

        storage_account_entity = extract_storage_account(account)
        if storage_account_entity and storage_account_dedup.add_if_new(storage_account_entity["key"]):
            raw_components.append(storage_account_entity)

        for client_app_entity in extract_client_apps(account):
            if client_app_dedup.add_if_new(client_app_entity["key"]):
                raw_components.append(client_app_entity)

        for resource_provider_entity in extract_resource_providers(account):
            if resource_provider_dedup.add_if_new(resource_provider_entity["key"]):
                raw_services.append(resource_provider_entity)

        for workspace_entity in extract_log_analytics_workspaces(account):
            if workspace_dedup.add_if_new(workspace_entity["key"]):
                raw_services.append(workspace_entity)

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

    storage_account_dedup = DeduplicatingSet()
    client_app_dedup = DeduplicatingSet()
    resource_provider_dedup = DeduplicatingSet()
    workspace_dedup = DeduplicatingSet()

    all_components: list[dict] = []
    all_services: list[dict] = []
    tenant_id = ""

    for log_file in log_files:
        print(f"Processing {log_file.name} ...")
        components, services, first_tenant_id = process_log_file(
            log_file, storage_account_dedup, client_app_dedup, resource_provider_dedup, workspace_dedup
        )
        all_components.extend(components)
        all_services.extend(services)
        if not tenant_id:
            tenant_id = first_tenant_id
        print(f"  {len(components)} new components, {len(services)} new services")

    source_files = ", ".join(file.name for file in log_files)
    bom = build_cyclonedx_bom(all_components, all_services, tenant_id, source_files)

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    output_path = REPORT_DIR / f"bom_{timestamp}.json"
    output_path.write_text(json.dumps(bom, indent=2), encoding="utf-8")

    print(f"\nBOM report saved to: {output_path}")
    print(f"Total: {len(all_components)} components, {len(all_services)} services")


if __name__ == "__main__":
    main()
