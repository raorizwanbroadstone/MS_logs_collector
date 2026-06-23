import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import boto3
from dotenv import load_dotenv
import generate_bom

load_dotenv(Path(__file__).parent.parent.parent / ".env")

AWS_REGION     = os.getenv("AWS_DEFAULT_REGION", "us-east-1")
AWS_KEY_ID     = os.getenv("AWS_DYNAMODB_ACCESS_KEY_ID", "")
AWS_SECRET_KEY = os.getenv("AWS_DYNAMODB_SECRET_ACCESS_KEY", "")

LOOKBACK_HOURS = 24
OUTPUT_DIR     = Path(__file__).parent / "logs"

DYNAMODB_EVENT_SOURCES = ["dynamodb.amazonaws.com"]

RESOURCE_PARAM_KEYS = {
    "tableName":       "Table",
    "targetTableName": "Table",
}


def _dynamodb_client():
    return boto3.client(
        "dynamodb",
        region_name=AWS_REGION,
        aws_access_key_id=AWS_KEY_ID,
        aws_secret_access_key=AWS_SECRET_KEY,
    )


def _cloudtrail_client():
    return boto3.client(
        "cloudtrail",
        region_name=AWS_REGION,
        aws_access_key_id=AWS_KEY_ID,
        aws_secret_access_key=AWS_SECRET_KEY,
    )


def check_dynamodb_availability() -> bool:
    try:
        _dynamodb_client().list_tables(Limit=1)
        print("  DynamoDB is reachable")
        return True
    except Exception as exc:
        msg = str(exc)
        if any(kw in msg.lower() for kw in ("could not connect", "connection refused", "nodename")):
            print(f"  DynamoDB connectivity issue: {type(exc).__name__}")
            return False
        print(f"  DynamoDB endpoint reachable ({type(exc).__name__}: {msg[:120]})")
        return True


def fetch_events_for_source(source: str, start_time: datetime, end_time: datetime) -> list[dict]:
    client = _cloudtrail_client()
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
    seen: set[str] = set()
    resources: list[dict] = []
    for event in events:
        params = event.get("requestParameters") or {}
        if not isinstance(params, dict):
            continue
        for param_key, resource_type in RESOURCE_PARAM_KEYS.items():
            name = params.get(param_key)
            if name and name not in seen:
                seen.add(name)
                resources.append({"resource_type": resource_type, "resource_name": name})
    return resources


def describe_table(client, table_name: str) -> dict:
    result = {
        "resource_type":     "Table",
        "resource_name":     table_name,
        "arn":               "",
        "table_id":          "",
        "table_status":      "",
        "billing_mode":      "",
        "item_count":        0,
        "size_bytes":        0,
        "partition_key":     "",
        "sort_key":          "",
        "gsi_count":         0,
        "lsi_count":         0,
        "stream_enabled":    False,
        "stream_view_type":  "",
        "encryption_type":   "",
        "creation_datetime": "",
        "pitr_enabled":      False,
        "pitr_earliest":     "",
        "pitr_latest":       "",
        "access_denied":     False,
        "not_found":         False,
    }
    try:
        resp  = client.describe_table(TableName=table_name)
        table = resp.get("Table") or {}

        result["arn"]          = table.get("TableArn", "")
        result["table_id"]     = table.get("TableId", "")
        result["table_status"] = table.get("TableStatus", "")
        result["item_count"]   = table.get("ItemCount", 0)
        result["size_bytes"]   = table.get("TableSizeBytes", 0)

        billing = table.get("BillingModeSummary") or {}
        result["billing_mode"] = billing.get("BillingMode", "PROVISIONED")

        for key in (table.get("KeySchema") or []):
            if key.get("KeyType") == "HASH":
                result["partition_key"] = key.get("AttributeName", "")
            elif key.get("KeyType") == "RANGE":
                result["sort_key"] = key.get("AttributeName", "")

        result["gsi_count"] = len(table.get("GlobalSecondaryIndexes") or [])
        result["lsi_count"] = len(table.get("LocalSecondaryIndexes") or [])

        stream = table.get("StreamSpecification") or {}
        result["stream_enabled"]   = stream.get("StreamEnabled", False)
        result["stream_view_type"] = stream.get("StreamViewType", "")

        sse = table.get("SSEDescription") or {}
        result["encryption_type"] = sse.get("SSEType", "") if sse else "AWS_OWNED_KMS"

        ct = table.get("CreationDateTime")
        result["creation_datetime"] = ct.isoformat() if ct else ""

        try:
            pitr_resp = client.describe_continuous_backups(TableName=table_name)
            pitr_desc = pitr_resp.get("ContinuousBackupsDescription") or {}
            pitr      = pitr_desc.get("PointInTimeRecoveryDescription") or {}
            result["pitr_enabled"] = pitr.get("PointInTimeRecoveryStatus") == "ENABLED"
            earliest = pitr.get("EarliestRestorableDateTime")
            latest   = pitr.get("LatestRestorableDateTime")
            result["pitr_earliest"] = earliest.isoformat() if earliest else ""
            result["pitr_latest"]   = latest.isoformat() if latest else ""
        except Exception:
            pass

    except Exception as exc:
        msg = str(exc)
        if "AccessDenied" in msg or "AccessDeniedException" in msg:
            result["access_denied"] = True
            print(f"    Cannot describe Table {table_name}: {type(exc).__name__}")
        elif "ResourceNotFoundException" in msg:
            result["not_found"] = True
            print(f"    Not found: Table/{table_name}")
        else:
            print(f"    Error describing Table/{table_name}: {exc}")
    return result


def enumerate_all_tables() -> list[dict]:
    client    = _dynamodb_client()
    resources: list[dict] = []
    try:
        paginator  = client.get_paginator("list_tables")
        for page in paginator.paginate():
            for name in (page.get("TableNames") or []):
                resources.append({"resource_type": "Table", "resource_name": name})
        print(f"  {len(resources)} tables")
    except Exception as exc:
        print(f"  Error enumerating tables: {exc}")
    return resources


def build_inventory_event(resource: dict, event_time: datetime) -> dict:
    resource_type = resource["resource_type"]
    resource_name = resource["resource_name"]
    return {
        "EventId":           f"inventory-{resource_type}-{resource_name}",
        "EventName":         "DynamoDBResourceInventory",
        "EventSource":       "dynamodb-local-enumeration",
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
        print("Missing credentials. Set AWS_DYNAMODB_ACCESS_KEY_ID / AWS_DYNAMODB_SECRET_ACCESS_KEY in .env")
        return

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    timestamp   = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H-%M-%S")
    output_file = OUTPUT_DIR / f"dynamodb_logs_{timestamp}.json"

    end_time   = datetime.now(timezone.utc)
    start_time = end_time - timedelta(hours=LOOKBACK_HOURS)

    print(f"Region : {AWS_REGION}")
    print(f"Window : {start_time.strftime('%Y-%m-%dT%H:%M:%SZ')} -> {end_time.strftime('%Y-%m-%dT%H:%M:%SZ')}\n")

    print("Checking DynamoDB availability...")
    if not check_dynamodb_availability():
        return
    print()

    all_events: list[dict] = []
    for source in DYNAMODB_EVENT_SOURCES:
        print(f"Fetching CloudTrail events: {source}")
        try:
            raw_events = fetch_events_for_source(source, start_time, end_time)
            normalised = [normalize_event(e) for e in raw_events]
            all_events.extend(normalised)
            print(f"  {len(normalised)} events from {source}\n")
        except Exception as exc:
            print(f"  Error fetching {source}: {exc}\n")

    print("Enumerating current DynamoDB tables...")
    enumerated = enumerate_all_tables()

    ct_resources = extract_unique_resources(all_events)
    seen: set[str] = {r["resource_name"] for r in enumerated}
    for r in ct_resources:
        if r["resource_name"] not in seen:
            seen.add(r["resource_name"])
            enumerated.append(r)

    client = _dynamodb_client()
    print(f"\nDescribing {len(enumerated)} unique DynamoDB tables...")
    for resource_ref in enumerated:
        rname = resource_ref["resource_name"]
        print(f"  -> Table/{rname}")
        details = describe_table(client, rname)
        if not details.get("access_denied") and not details.get("not_found"):
            status = details.get("table_status", "")
            mode   = details.get("billing_mode", "")
            items  = details.get("item_count", 0)
            print(f"    status={status}, billing={mode}, items={items}")
        all_events.append(build_inventory_event(details, end_time))

    with output_file.open("w", encoding="utf-8") as fh:
        json.dump(all_events, fh, indent=2, ensure_ascii=False)

    print(f"\nCompleted.")
    print(f"  Total events collected: {len(all_events)}")
    print(f"  Output saved to:        {output_file}")

    print("\nGenerating BOM report...")
    generate_bom.main(target_file=output_file)


if __name__ == "__main__":
    main()