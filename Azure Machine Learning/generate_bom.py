"""
generate_bom.py

Streams Azure Machine Learning log files from logs/, extracts BOM-relevant
entities (AML workspaces, ML models, compute, online endpoints, data assets,
client applications, resource providers, Log Analytics workspaces), deduplicates
them with a Bloom filter backed by an exact set, and writes a CycloneDX 1.6
BOM JSON report to report/.

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
def stream_workspaces(log_file: Path):
    """
    Yields each AML workspace object from the nested workspaces array.
    The log format is { workspaces: [...] }, so the ijson prefix is
    "workspaces.item" — identical nesting pattern to Azure Storage logs.
    """
    with log_file.open("rb") as fh:
        yield from ijson.items(fh, "workspaces.item")


# Entity extractors
def extract_workspace(ws: dict) -> dict | None:
    """
    Builds an AML workspace component from the workspace metadata block.
    workspace_resource_id (lowercase ARM path) is the unique key.
    """
    resource_id = ws.get("workspace_resource_id", "")
    if not resource_id:
        return None
    return {
        "kind":            "aml_workspace",
        "key":             resource_id.lower(),
        "name":            ws.get("workspace_name") or resource_id.split("/")[-1],
        "resource_id":     resource_id.lower(),
        "subscription_id": ws.get("subscription_id", ""),
        "resource_group":  ws.get("resource_group", ""),
        "location":        ws.get("location", ""),
        "workload":        "Microsoft.MachineLearningServices",
    }


def extract_models(ws: dict) -> list[dict]:
    """
    Extracts registered ML models from assets.models. Each model is a first-class
    BOM artifact and receives a CycloneDX version field from its registry version.
    Returns an empty list when the SDK was not available or no models are registered.
    """
    assets = ws.get("assets", {})
    models = assets.get("models", [])
    if not isinstance(models, list):
        return []
    ws_id = ws.get("workspace_resource_id", "").lower()
    results = []
    for m in models:
        if not isinstance(m, dict):
            continue
        name = m.get("name", "")
        version = str(m.get("version", "")) if m.get("version") is not None else ""
        if not name:
            continue
        results.append({
            "kind":        "ml_model",
            "key":         f"{ws_id}/models/{name}/{version}",
            "name":        name,
            "version":     version,
            "model_type":  str(m.get("type", "")),
            "description": str(m.get("description", "")),
            "resource_id": ws_id,
            "workload":    "Microsoft.MachineLearningServices",
        })
    return results


def extract_compute(ws: dict) -> list[dict]:
    """
    Extracts compute clusters and instances from assets.compute.
    Compute resources map to CycloneDX type machine — physical or virtual
    infrastructure that runs training jobs.
    """
    assets = ws.get("assets", {})
    compute_list = assets.get("compute", [])
    if not isinstance(compute_list, list):
        return []
    ws_id = ws.get("workspace_resource_id", "").lower()
    results = []
    for c in compute_list:
        if not isinstance(c, dict):
            continue
        name = c.get("name", "")
        if not name:
            continue
        results.append({
            "kind":               "compute",
            "key":                f"{ws_id}/compute/{name}",
            "name":               name,
            "compute_type":       str(c.get("type", "")),
            "provisioning_state": str(c.get("provisioning_state", "")),
            "location":           str(c.get("location", "")),
            "resource_id":        ws_id,
        })
    return results


def extract_data_assets(ws: dict) -> list[dict]:
    """
    Extracts data assets from assets.data_assets. Data assets are versioned
    datasets registered in the workspace and represent data inputs to ML pipelines.
    """
    assets = ws.get("assets", {})
    data_list = assets.get("data_assets", [])
    if not isinstance(data_list, list):
        return []
    ws_id = ws.get("workspace_resource_id", "").lower()
    results = []
    for d in data_list:
        if not isinstance(d, dict):
            continue
        name = d.get("name", "")
        version = str(d.get("version", "")) if d.get("version") is not None else ""
        if not name:
            continue
        results.append({
            "kind":       "data_asset",
            "key":        f"{ws_id}/data/{name}/{version}",
            "name":       name,
            "version":    version,
            "data_type":  str(d.get("type", "")),
            "path":       str(d.get("path", "")),
            "resource_id": ws_id,
        })
    return results


def extract_online_endpoints(ws: dict) -> list[dict]:
    """
    Extracts online inference endpoints from assets.online_endpoints.
    Endpoints are deployed ML services and map to the CycloneDX services section.
    """
    assets = ws.get("assets", {})
    ep_list = assets.get("online_endpoints", [])
    if not isinstance(ep_list, list):
        return []
    ws_id = ws.get("workspace_resource_id", "").lower()
    results = []
    for ep in ep_list:
        if not isinstance(ep, dict):
            continue
        name = ep.get("name", "")
        if not name:
            continue
        results.append({
            "kind":                "online_endpoint",
            "key":                 f"{ws_id}/endpoints/{name}",
            "name":                name,
            "scoring_uri":         str(ep.get("scoring_uri", "")),
            "provisioning_state":  str(ep.get("provisioning_state", "")),
            "auth_mode":           str(ep.get("auth_mode", "")),
            "resource_id":         ws_id,
        })
    return results


def extract_client_apps(ws: dict) -> list[dict]:
    """
    Scans activity_logs for Azure AD application IDs from JWT claims.
    claims.appid is the OAuth2 client that performed each management-plane action.
    """
    results = []
    for event in ws.get("activity_logs", []):
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


def extract_resource_providers(ws: dict) -> list[dict]:
    """
    Extracts unique resource providers (e.g., Microsoft.MachineLearningServices)
    from activity_logs. These are the Azure platform services being invoked.
    """
    results = []
    for event in ws.get("activity_logs", []):
        if not isinstance(event, dict):
            continue
        provider = event.get("resource_provider_name", {})
        if isinstance(provider, dict):
            name = provider.get("value", "")
            if name:
                results.append({
                    "kind": "resource_provider",
                    "key":  name.lower(),
                    "name": name,
                })
    return results


def extract_log_analytics_workspaces(ws: dict) -> list[dict]:
    """
    Extracts Log Analytics workspace IDs from aml_log_tables keys.
    Each key is a workspace ID receiving AML diagnostic telemetry.
    """
    results = []
    log_tables = ws.get("aml_log_tables", {})
    if not isinstance(log_tables, dict):
        return results
    for ws_id, tables in log_tables.items():
        if isinstance(tables, dict) and ws_id not in ("status", "error"):
            results.append({
                "kind":  "log_analytics_workspace",
                "key":   ws_id,
                "name":  f"Log Analytics Workspace ({ws_id[:8]}...)",
                "ws_id": ws_id,
            })
    return results


def capture_tenant_id(ws: dict) -> str:
    """Reads tenant_id from the first valid activity log event in the workspace."""
    for event in ws.get("activity_logs", []):
        if isinstance(event, dict):
            tid = event.get("tenant_id", "")
            if tid:
                return tid
    return ""


# CycloneDX 1.6 serializers
def to_cyclonedx_component(raw: dict) -> dict:
    """
    Maps a raw component dict to a CycloneDX 1.6 component object.
    ML models use type 'library' and carry a version field.
    Compute resources use type 'machine'. All others use type 'application'.
    """
    bom_ref = f"{raw['kind']}-{raw['key']}"
    props: list[dict] = []

    type_map = {
        "aml_workspace": "application",
        "ml_model":      "library",
        "compute":       "machine",
        "data_asset":    "file",
        "client_app":    "application",
    }

    field_map: dict[str, dict[str, str]] = {
        "aml_workspace": {
            "resource_id":     "azure:WorkspaceResourceId",
            "subscription_id": "azure:SubscriptionId",
            "resource_group":  "azure:ResourceGroup",
            "location":        "azure:Location",
        },
        "ml_model": {
            "model_type":  "aml:ModelType",
            "description": "aml:Description",
            "resource_id": "azure:WorkspaceResourceId",
        },
        "compute": {
            "compute_type":       "aml:ComputeType",
            "provisioning_state": "aml:ProvisioningState",
            "location":           "azure:Location",
            "resource_id":        "azure:WorkspaceResourceId",
        },
        "data_asset": {
            "data_type":  "aml:DataType",
            "path":       "aml:Path",
            "resource_id": "azure:WorkspaceResourceId",
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
        "type":    type_map.get(raw["kind"], "application"),
        "bom-ref": bom_ref,
        "name":    raw["name"],
    }
    if raw.get("version"):
        comp["version"] = raw["version"]
    if props:
        comp["properties"] = props
    return comp


def to_cyclonedx_service(raw: dict) -> dict:
    """
    Maps a raw resource_provider, online_endpoint, or log_analytics_workspace
    dict to a CycloneDX 1.6 service object. authenticated is True for all
    Azure services.
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
    elif raw["kind"] == "online_endpoint":
        if raw.get("scoring_uri"):
            props.append({"name": "aml:ScoringUri",         "value": raw["scoring_uri"]})
        if raw.get("auth_mode"):
            props.append({"name": "aml:AuthMode",           "value": raw["auth_mode"]})
        if raw.get("provisioning_state"):
            props.append({"name": "aml:ProvisioningState",  "value": raw["provisioning_state"]})

    if props:
        svc["properties"] = props
    return svc


def build_dependency_graph(
    raw_components: list[dict],
    raw_services: list[dict],
) -> list[dict]:
    """
    Builds the CycloneDX dependencies section.
    Root Azure tenant depends on all services.
    AML workspaces and client apps depend on their resource provider.
    Models, compute, and data assets depend on their parent workspace.
    Online endpoints depend on their parent workspace.
    """
    service_refs  = {r["key"]: f"{r['kind']}-{r['key']}" for r in raw_services}
    workspace_refs = {r["key"]: f"{r['kind']}-{r['key']}" for r in raw_components if r["kind"] == "aml_workspace"}

    deps: list[dict] = [
        {
            "ref":       "root-azure-tenant",
            "dependsOn": list(service_refs.values()),
        }
    ]

    workspace_provider_key = "microsoft.machinelearningservices"

    for raw in raw_components:
        kind = raw["kind"]

        if kind == "aml_workspace":
            ref = service_refs.get(workspace_provider_key)
            if ref:
                deps.append({"ref": f"{kind}-{raw['key']}", "dependsOn": [ref]})

        elif kind == "client_app":
            ref = service_refs.get(raw.get("workload", "").lower())
            if ref:
                deps.append({"ref": f"{kind}-{raw['key']}", "dependsOn": [ref]})

        elif kind in ("ml_model", "compute", "data_asset"):
            parent_ws = raw.get("resource_id", "")
            ref = workspace_refs.get(parent_ws)
            if ref:
                deps.append({"ref": f"{kind}-{raw['key']}", "dependsOn": [ref]})

    for raw in raw_services:
        if raw["kind"] == "online_endpoint":
            parent_ws = raw.get("resource_id", "")
            ref = workspace_refs.get(parent_ws)
            if ref:
                deps.append({"ref": f"{raw['kind']}-{raw['key']}", "dependsOn": [ref]})

    return deps


def build_cyclonedx_bom(
    raw_components: list[dict],
    raw_services: list[dict],
    tenant_id: str,
    source_files: str,
) -> dict:
    """
    Assembles the full CycloneDX 1.6 BOM document from extracted entities.
    Includes metadata, components (workspaces, models, compute, data, client apps),
    services (endpoints, providers, workspaces), and a full dependency graph.
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
                        "name":    "azure-aml-bom-generator",
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
    workspace_dedup:  DeduplicatingSet,
    model_dedup:      DeduplicatingSet,
    compute_dedup:    DeduplicatingSet,
    data_dedup:       DeduplicatingSet,
    client_dedup:     DeduplicatingSet,
    provider_dedup:   DeduplicatingSet,
    la_workspace_dedup: DeduplicatingSet,
    endpoint_dedup:   DeduplicatingSet,
) -> tuple[list[dict], list[dict], str]:
    """
    Streams one log file, iterates over each AML workspace and its nested
    activity_logs and assets, and collects new components and services.
    All dedup sets are shared across calls to catch cross-file duplicates.
    Returns (raw_components, raw_services, tenant_id).
    """
    raw_components: list[dict] = []
    raw_services:   list[dict] = []
    tenant_id = ""

    for ws in stream_workspaces(log_file):
        if not tenant_id:
            tenant_id = capture_tenant_id(ws)

        w = extract_workspace(ws)
        if w and workspace_dedup.add_if_new(w["key"]):
            raw_components.append(w)

        for m in extract_models(ws):
            if model_dedup.add_if_new(m["key"]):
                raw_components.append(m)

        for c in extract_compute(ws):
            if compute_dedup.add_if_new(c["key"]):
                raw_components.append(c)

        for d in extract_data_assets(ws):
            if data_dedup.add_if_new(d["key"]):
                raw_components.append(d)

        for ca in extract_client_apps(ws):
            if client_dedup.add_if_new(ca["key"]):
                raw_components.append(ca)

        for rp in extract_resource_providers(ws):
            if provider_dedup.add_if_new(rp["key"]):
                raw_services.append(rp)

        for la in extract_log_analytics_workspaces(ws):
            if la_workspace_dedup.add_if_new(la["key"]):
                raw_services.append(la)

        for ep in extract_online_endpoints(ws):
            if endpoint_dedup.add_if_new(ep["key"]):
                raw_services.append(ep)

    return raw_components, raw_services, tenant_id


# Entry point
def main(target_file: Path | None = None) -> None:
    """
    Entry point. Processes target_file if given (called from fetch_aml_logs.py),
    otherwise processes all JSON files in logs/. Builds a CycloneDX 1.6 BOM
    and writes it to report/.
    """
    REPORT_DIR.mkdir(parents=True, exist_ok=True)

    log_files = [target_file] if target_file else sorted(LOGS_DIR.glob("*.json"))
    if not log_files:
        print(f"No JSON files found in {LOGS_DIR}")
        return

    workspace_dedup    = DeduplicatingSet()
    model_dedup        = DeduplicatingSet()
    compute_dedup      = DeduplicatingSet()
    data_dedup         = DeduplicatingSet()
    client_dedup       = DeduplicatingSet()
    provider_dedup     = DeduplicatingSet()
    la_workspace_dedup = DeduplicatingSet()
    endpoint_dedup     = DeduplicatingSet()

    all_components: list[dict] = []
    all_services:   list[dict] = []
    tenant_id = ""

    for log_file in log_files:
        print(f"Processing {log_file.name} ...")
        comps, svcs, fid = process_log_file(
            log_file,
            workspace_dedup, model_dedup, compute_dedup, data_dedup,
            client_dedup, provider_dedup, la_workspace_dedup, endpoint_dedup,
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
