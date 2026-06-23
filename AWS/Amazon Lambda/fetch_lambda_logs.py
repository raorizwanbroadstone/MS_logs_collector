import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import boto3
from dotenv import load_dotenv
import generate_bom

load_dotenv(Path(__file__).parent.parent.parent / ".env")

AWS_REGION     = os.getenv("AWS_DEFAULT_REGION", "eu-north-1")
AWS_KEY_ID     = os.getenv("AWS_LAMBDA_ACCESS_KEY_ID", "")
AWS_SECRET_KEY = os.getenv("AWS_LAMBDA_SECRET_ACCESS_KEY", "")

LOOKBACK_HOURS = 24
OUTPUT_DIR     = Path(__file__).parent / "logs"

LAMBDA_EVENT_SOURCES = [
    "lambda.amazonaws.com",
]

RESOURCE_PARAM_KEYS = {
    "functionName": "Function",
    "layerName":    "Layer",
}


def _lambda_client():
    return boto3.client(
        "lambda",
        region_name=AWS_REGION,
        aws_access_key_id=AWS_KEY_ID,
        aws_secret_access_key=AWS_SECRET_KEY,
    )


def check_lambda_availability() -> bool:
    try:
        _lambda_client().list_functions(MaxItems=1)
        print("  Lambda is reachable")
        return True
    except Exception as exc:
        msg = str(exc)
        if any(kw in msg.lower() for kw in ("could not connect", "connection refused", "nodename nor servname")):
            print(f"  Lambda connectivity issue: {type(exc).__name__}")
            return False
        print(f"  Lambda endpoint reachable ({type(exc).__name__}: {msg[:120]})")
        return True


def fetch_events_for_source(source: str, start_time: datetime, end_time: datetime) -> list[dict]:
    client = boto3.client(
        "cloudtrail",
        region_name=AWS_REGION,
        aws_access_key_id=AWS_KEY_ID,
        aws_secret_access_key=AWS_SECRET_KEY,
    )
    events: list[dict] = []
    kwargs: dict = dict(
        LookupAttributes=[{"AttributeKey": "EventSource", "AttributeValue": source}],
        StartTime=start_time,
        EndTime=end_time,
        MaxResults=50,
    )
    page = 0
    while True:
        resp  = client.lookup_events(**kwargs)
        batch = resp.get("Events", [])
        events.extend(batch)
        page += 1
        print(f"    Page {page}: {len(batch)} events")
        token = resp.get("NextToken")
        if not token:
            break
        kwargs["NextToken"] = token
    return events


def normalize_event(raw: dict) -> dict:
    event_time = raw.get("EventTime", datetime.now(timezone.utc))
    out: dict = {
        "EventId":     raw.get("EventId", ""),
        "EventName":   raw.get("EventName", ""),
        "EventTime":   event_time.isoformat() if isinstance(event_time, datetime) else str(event_time),
        "EventSource": raw.get("EventSource", ""),
        "Username":    raw.get("Username", ""),
        "Resources":   raw.get("Resources", []),
    }
    try:
        ct: dict = json.loads(raw.get("CloudTrailEvent", "{}"))
        out["userIdentity"]      = ct.get("userIdentity") or {}
        out["requestParameters"] = ct.get("requestParameters") or {}
        out["responseElements"]  = ct.get("responseElements") or {}
        out["awsRegion"]         = ct.get("awsRegion", "")
        out["sourceIPAddress"]   = ct.get("sourceIPAddress", "")
        out["errorCode"]         = ct.get("errorCode", "")
        out["errorMessage"]      = ct.get("errorMessage", "")
    except (json.JSONDecodeError, TypeError):
        pass
    return out


def extract_unique_resources(events: list[dict]) -> list[dict]:
    seen: set[tuple[str, str]] = set()
    resources: list[dict] = []
    for event in events:
        params = event.get("requestParameters") or {}
        if not isinstance(params, dict):
            continue
        for param_key, resource_type in RESOURCE_PARAM_KEYS.items():
            name = params.get(param_key)
            if name and (resource_type, name) not in seen:
                seen.add((resource_type, name))
                resources.append({"resource_type": resource_type, "resource_name": name})
    return resources


def describe_resource(resource_type: str, resource_name: str) -> dict:
    client = _lambda_client()
    result = {
        "resource_type":       resource_type,
        "resource_name":       resource_name,
        "arn":                 "",
        "runtime":             "",
        "handler":             "",
        "memory_size":         0,
        "timeout":             0,
        "last_modified":       "",
        "state":               "",
        "architecture":        "",
        "package_type":        "",
        "layer_arns":          [],
        "vpc_enabled":         False,
        "log_group":           "",
        "compatible_runtimes": [],
        "access_denied":       False,
        "not_found":           False,
    }
    try:
        if resource_type == "Function":
            resp   = client.get_function(FunctionName=resource_name)
            config = resp.get("Configuration") or {}
            result["arn"]           = config.get("FunctionArn", "")
            result["runtime"]       = config.get("Runtime", "")
            result["handler"]       = config.get("Handler", "")
            result["memory_size"]   = config.get("MemorySize", 0)
            result["timeout"]       = config.get("Timeout", 0)
            result["last_modified"] = config.get("LastModified", "")
            result["state"]         = config.get("State", "")
            archs = config.get("Architectures") or []
            result["architecture"]  = archs[0] if archs else ""
            result["package_type"]  = config.get("PackageType", "")
            result["layer_arns"]    = [l.get("Arn", "") for l in (config.get("Layers") or []) if isinstance(l, dict)]
            vpc = config.get("VpcConfig") or {}
            result["vpc_enabled"]   = bool(vpc.get("VpcId"))
            logging_cfg = config.get("LoggingConfig") or {}
            result["log_group"]     = logging_cfg.get("LogGroup", "")

        elif resource_type == "Layer":
            resp     = client.list_layer_versions(LayerName=resource_name, MaxItems=1)
            versions = resp.get("LayerVersions") or []
            if versions:
                latest = versions[0]
                result["arn"]                 = latest.get("LayerVersionArn", "")
                result["last_modified"]       = latest.get("CreatedDate", "")
                result["compatible_runtimes"] = latest.get("CompatibleRuntimes") or []

    except Exception as exc:
        msg = str(exc)
        if "AccessDenied" in msg or "AccessDeniedException" in msg:
            result["access_denied"] = True
            print(f"    Cannot describe {resource_type} {resource_name}: {type(exc).__name__}")
        elif "ResourceNotFoundException" in msg or "Function not found" in msg or "does not exist" in msg.lower():
            result["not_found"] = True
            print(f"    Not found: {resource_type}/{resource_name}")
        else:
            print(f"    Error describing {resource_type}/{resource_name}: {exc}")
    return result


def build_resource_inventory_event(resource: dict, event_time: datetime) -> dict:
    resource_type = resource["resource_type"]
    resource_name = resource["resource_name"]
    return {
        "EventId":           f"inventory-{resource_type}-{resource_name}",
        "EventName":         "LambdaResourceInventory",
        "EventSource":       "lambda-local-enumeration",
        "EventTime":         event_time.isoformat(),
        "Username":          "",
        "Resources":         [],
        "userIdentity":      {},
        "requestParameters": {"resourceType": resource_type, "resourceName": resource_name},
        "responseElements":  {},
        "awsRegion":         AWS_REGION,
        "sourceIPAddress":   "",
        "errorCode":         "AccessDenied" if resource.get("access_denied") else "",
        "errorMessage":      "",
        "inventory":         resource,
    }


def main() -> None:
    if not all([AWS_KEY_ID, AWS_SECRET_KEY]):
        print("Missing required credentials. Set AWS_LAMBDA_ACCESS_KEY_ID / AWS_LAMBDA_SECRET_ACCESS_KEY in .env")
        return

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    timestamp   = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H-%M-%S")
    output_file = OUTPUT_DIR / f"lambda_logs_{timestamp}.json"

    end_time   = datetime.now(timezone.utc)
    start_time = end_time - timedelta(hours=LOOKBACK_HOURS)

    print(f"Region : {AWS_REGION}")
    print(f"Window : {start_time.strftime('%Y-%m-%dT%H:%M:%SZ')} -> {end_time.strftime('%Y-%m-%dT%H:%M:%SZ')}\n")

    print("Checking Lambda availability...")
    check_lambda_availability()
    print()

    all_events: list[dict] = []
    for source in LAMBDA_EVENT_SOURCES:
        print(f"Fetching CloudTrail events: {source}")
        try:
            raw_events = fetch_events_for_source(source, start_time, end_time)
            normalised = [normalize_event(e) for e in raw_events]
            all_events.extend(normalised)
            print(f"  {len(normalised)} events from {source}\n")
        except Exception as exc:
            print(f"  Error fetching {source}: {exc}\n")

    unique_resources = extract_unique_resources(all_events)
    print(f"Describing {len(unique_resources)} unique Lambda resources...")

    for resource_ref in unique_resources:
        rtype = resource_ref["resource_type"]
        rname = resource_ref["resource_name"]
        print(f"  -> {rtype}/{rname}")
        details = describe_resource(rtype, rname)
        if not details["access_denied"] and not details["not_found"]:
            if rtype == "Function":
                runtime = details.get("runtime", "")
                state   = details.get("state", "")
                memory  = details.get("memory_size", 0)
                print(f"    runtime={runtime}, state={state}, memory={memory}MB")
            elif rtype == "Layer":
                runtimes = ", ".join(details.get("compatible_runtimes", []))
                print(f"    compatible_runtimes={runtimes or 'any'}")
        all_events.append(build_resource_inventory_event(details, end_time))

    with output_file.open("w", encoding="utf-8") as fh:
        json.dump(all_events, fh, indent=2, ensure_ascii=False)

    print(f"\nCompleted.")
    print(f"  Total events collected: {len(all_events)}")
    print(f"  Output saved to:        {output_file}")

    print("\nGenerating BOM report...")
    generate_bom.main(target_file=output_file)


if __name__ == "__main__":
    main()
