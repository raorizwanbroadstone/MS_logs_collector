"""
generate_bom.py

Streams every JSON file in logs/, extracts BOM-relevant entities (foundation
models, IAM principals, Bedrock agents), deduplicates them with a Bloom filter
backed by an exact set, and writes a CycloneDX 1.6 BOM JSON report to report/.

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
# Log streaming
# ---------------------------------------------------------------------------

def stream_events(log_file: Path):
    """Yields each top-level JSON object from a large array file using ijson."""
    with log_file.open("rb") as fh:
        yield from ijson.items(fh, "item")


# ---------------------------------------------------------------------------
# Entity extractors — each returns a typed dict or None
# ---------------------------------------------------------------------------

def extract_foundation_model(event: dict) -> dict | None:
    """
    Extracts the foundation model ID from InvokeModel, Converse, or any
    Bedrock API call that includes modelId in requestParameters.
    The model provider is derived from the first segment of the model ID
    (e.g. 'anthropic' from 'anthropic.claude-3-sonnet-20240229-v1:0').
    """
    params = event.get("requestParameters") or {}
    if not isinstance(params, dict):
        return None
    model_id = params.get("modelId", "") or params.get("modelArn", "")
    if not model_id:
        return None

    provider = model_id.split(".")[0] if "." in model_id else "unknown"
    return {
        "kind":         "foundation_model",
        "key":          model_id,
        "name":         model_id,
        "model_id":     model_id,
        "provider":     provider,
        "event_source": event.get("EventSource", ""),
    }


def extract_iam_principal(event: dict) -> dict | None:
    """
    Extracts the calling IAM identity from the userIdentity block.
    For AssumedRole, the role ARN (from sessionIssuer) is used as the dedup key
    so that all sessions from the same role collapse to one BOM component.
    Also captures which model was used in this event for dependency linking.
    """
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
        role_arn = issuer.get("arn", session_arn)   # stable role ARN, not session ARN
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

    params         = event.get("requestParameters") or {}
    observed_model = params.get("modelId", "") or params.get("modelArn", "")

    return {
        "kind":            "iam_principal",
        "key":             key,
        "name":            name,
        "arn":             session_arn,
        "identity_type":   identity_type,
        "account_id":      account_id,
        "observed_model":  observed_model,
        "event_source":    event.get("EventSource", ""),
    }


def extract_bedrock_agent(event: dict) -> dict | None:
    """
    Extracts Bedrock Agent details from bedrock-agent or bedrock-agent-runtime
    events. agentId in requestParameters is the unique dedup key.
    Returns None for events from other sources or without an agentId.
    """
    if "bedrock-agent" not in (event.get("EventSource") or ""):
        return None
    params = event.get("requestParameters") or {}
    if not isinstance(params, dict):
        return None
    agent_id = params.get("agentId", "")
    if not agent_id:
        return None

    return {
        "kind":           "bedrock_agent",
        "key":            agent_id,
        "name":           params.get("agentName") or agent_id,
        "agent_id":       agent_id,
        "agent_alias_id": params.get("agentAliasId", ""),
        "operation":      event.get("EventName", ""),
        "event_source":   event.get("EventSource", ""),
    }


# ---------------------------------------------------------------------------
# CycloneDX 1.6 serialisers
# ---------------------------------------------------------------------------

def _make_bom_ref(kind: str, key: str) -> str:
    """Sanitises key characters that may confuse BOM parsers."""
    safe = key.replace(":", "-").replace("/", "-")
    return f"{kind}-{safe}"


def to_cyclonedx_component(raw: dict) -> dict:
    """
    Converts an IAM principal or Bedrock agent dict to a CycloneDX 1.6
    component (type: application). All source fields are stored as
    aws:-namespaced properties.
    """
    field_map: dict[str, dict[str, str]] = {
        "iam_principal": {
            "arn":           "aws:IAMPrincipalARN",
            "identity_type": "aws:IdentityType",
            "account_id":    "aws:AccountId",
            "event_source":  "aws:EventSource",
        },
        "bedrock_agent": {
            "agent_id":       "aws:BedrockAgentId",
            "agent_alias_id": "aws:BedrockAgentAliasId",
            "operation":      "aws:Operation",
            "event_source":   "aws:EventSource",
        },
    }
    props = [
        {"name": cdx_name, "value": raw[field]}
        for field, cdx_name in field_map.get(raw["kind"], {}).items()
        if raw.get(field)
    ]
    component: dict = {
        "type":    "application",
        "bom-ref": _make_bom_ref(raw["kind"], raw["key"]),
        "name":    raw["name"],
    }
    if props:
        component["properties"] = props
    return component


def to_cyclonedx_service(raw: dict) -> dict:
    """
    Converts a foundation model dict to a CycloneDX 1.6 service entry.
    authenticated is True because all Bedrock API calls require AWS SigV4.
    """
    svc: dict = {
        "bom-ref":       _make_bom_ref("model", raw["model_id"]),
        "name":          raw["name"],
        "authenticated": True,
    }
    props = [
        {"name": "aws:ModelProvider", "value": raw["provider"]},
        {"name": "aws:ModelId",       "value": raw["model_id"]},
        {"name": "aws:EventSource",   "value": raw["event_source"]},
    ]
    svc["properties"] = [p for p in props if p["value"]]
    return svc


def build_dependency_graph(
    raw_components: list[dict],
    raw_services:   list[dict],
) -> list[dict]:
    """
    Builds the CycloneDX dependencies section.
    Root AWS account depends on every foundation model observed.
    Each IAM principal depends on the specific model it was seen calling
    (or the root account when no model was captured in that event).
    Bedrock agents depend on the root account (model binding is internal to agent config).
    """
    model_ref_map = {r["model_id"]: _make_bom_ref("model", r["model_id"]) for r in raw_services}

    deps: list[dict] = [
        {
            "ref":       "root-aws-account",
            "dependsOn": list(model_ref_map.values()),
        }
    ]

    for raw in raw_components:
        if raw["kind"] == "iam_principal":
            observed = raw.get("observed_model", "")
            depends_on = [model_ref_map[observed]] if observed in model_ref_map else ["root-aws-account"]
        else:
            depends_on = ["root-aws-account"]

        deps.append({
            "ref":       _make_bom_ref(raw["kind"], raw["key"]),
            "dependsOn": depends_on,
        })

    return deps


def build_cyclonedx_bom(
    raw_components: list[dict],
    raw_services:   list[dict],
    account_id:     str,
    source_files:   str,
) -> dict:
    """
    Assembles the complete CycloneDX 1.6 BOM document from extracted data.
    Root component represents the AWS account; services are foundation models;
    components are IAM principals and Bedrock agents.
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
                        "name":    "aws-bedrock-bom-generator",
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
        "services":     [to_cyclonedx_service(r)   for r in raw_services],
        "dependencies": build_dependency_graph(raw_components, raw_services),
    }


# ---------------------------------------------------------------------------
# Per-file processor
# ---------------------------------------------------------------------------

def process_log_file(
    log_file:        Path,
    model_dedup:     DeduplicatingSet,
    principal_dedup: DeduplicatingSet,
    agent_dedup:     DeduplicatingSet,
) -> tuple[list[dict], list[dict], str]:
    """
    Streams one log file and returns newly seen components and services.
    The three dedup sets are shared across files so cross-file duplicates are
    caught. Returns (raw_components, raw_services, first_seen_account_id).
    """
    raw_components: list[dict] = []
    raw_services:   list[dict] = []
    account_id = ""

    for event in stream_events(log_file):
        if not account_id:
            account_id = (event.get("userIdentity") or {}).get("accountId", "")

        model = extract_foundation_model(event)
        if model and model_dedup.add_if_new(model["key"]):
            raw_services.append(model)

        principal = extract_iam_principal(event)
        if principal and principal_dedup.add_if_new(principal["key"]):
            raw_components.append(principal)

        agent = extract_bedrock_agent(event)
        if agent and agent_dedup.add_if_new(agent["key"]):
            raw_components.append(agent)

    return raw_components, raw_services, account_id


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(target_file: Path | None = None) -> None:
    """
    Processes log files and writes a CycloneDX 1.6 BOM to report/.
    When target_file is given, only that file is processed — used when called
    directly from fetch_bedrock_logs.py to scope the BOM to the freshly fetched
    log. When called standalone (no argument), all JSON files in logs/ are processed.
    """
    REPORT_DIR.mkdir(parents=True, exist_ok=True)

    log_files = [target_file] if target_file else sorted(LOGS_DIR.glob("*.json"))
    if not log_files:
        print(f"No JSON files found in {LOGS_DIR}")
        return

    model_dedup     = DeduplicatingSet()
    principal_dedup = DeduplicatingSet()
    agent_dedup     = DeduplicatingSet()

    all_components: list[dict] = []
    all_services:   list[dict] = []
    account_id = ""

    for log_file in log_files:
        print(f"Processing {log_file.name} ...")
        comps, svcs, aid = process_log_file(
            log_file, model_dedup, principal_dedup, agent_dedup
        )
        all_components.extend(comps)
        all_services.extend(svcs)
        if not account_id and aid:
            account_id = aid
        print(f"  {len(comps)} new components, {len(svcs)} new models")

    source_files = ", ".join(f.name for f in log_files)
    bom          = build_cyclonedx_bom(all_components, all_services, account_id, source_files)

    timestamp   = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    output_path = REPORT_DIR / f"bom_{timestamp}.json"
    output_path.write_text(json.dumps(bom, indent=2), encoding="utf-8")

    print(f"\nReport : {output_path}")
    print(f"Total  : {len(all_components)} components, {len(all_services)} models")


if __name__ == "__main__":
    main()