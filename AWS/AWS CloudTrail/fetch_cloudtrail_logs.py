import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import boto3
from dotenv import load_dotenv
import generate_bom

load_dotenv(Path(__file__).parent.parent.parent / ".env")

AWS_REGION     = os.getenv("AWS_DEFAULT_REGION", "eu-north-1")
AWS_KEY_ID     = os.getenv("AWS_CLOUDTRAIL_ACCESS_KEY_ID", "")
AWS_SECRET_KEY = os.getenv("AWS_CLOUDTRAIL_SECRET_ACCESS_KEY", "")

LOOKBACK_HOURS = 24
OUTPUT_DIR     = Path(__file__).parent / "logs"

CLOUDTRAIL_EVENT_SOURCES = [
    "cloudtrail.amazonaws.com",
]

TRAIL_EVENT_KEYWORDS = {
    "Trail", "Logging", "EventSelectors", "InsightSelectors",
}

EDS_EVENT_KEYWORDS = {
    "EventDataStore", "Query", "Channel",
}


def _cloudtrail_client():
    return boto3.client(
        "cloudtrail",
        region_name=AWS_REGION,
        aws_access_key_id=AWS_KEY_ID,
        aws_secret_access_key=AWS_SECRET_KEY,
    )


def check_cloudtrail_availability() -> bool:
    try:
        _cloudtrail_client().describe_trails(includeShadowTrails=False)
        print("  CloudTrail is reachable")
        return True
    except Exception as exc:
        msg = str(exc)
        if any(kw in msg.lower() for kw in ("could not connect", "connection refused", "nodename nor servname")):
            print(f"  CloudTrail connectivity issue: {type(exc).__name__}")
            return False
        print(f"  CloudTrail endpoint reachable ({type(exc).__name__}: {msg[:120]})")
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
        params     = event.get("requestParameters") or {}
        event_name = event.get("EventName", "")
        if not isinstance(params, dict):
            continue

        resource_type = ""
        resource_name = ""

        trail_name = params.get("trailName")
        if trail_name:
            resource_type = "Trail"
            resource_name = trail_name

        if not resource_name:
            name = params.get("name")
            if name:
                if any(kw in event_name for kw in EDS_EVENT_KEYWORDS):
                    resource_type = "EventDataStore"
                else:
                    resource_type = "Trail"
                resource_name = name

        if not resource_name:
            eds_ref = params.get("eventDataStore")
            if eds_ref:
                resource_type = "EventDataStore"
                resource_name = eds_ref

        if resource_name and (resource_type, resource_name) not in seen:
            seen.add((resource_type, resource_name))
            resources.append({"resource_type": resource_type, "resource_name": resource_name})

    return resources


def describe_resource(resource_type: str, resource_name: str) -> dict:
    client = _cloudtrail_client()
    result = {
        "resource_type":       resource_type,
        "resource_name":       resource_name,
        "arn":                 "",
        "home_region":         "",
        "s3_bucket":           "",
        "log_group_arn":       "",
        "is_multi_region":     False,
        "is_organization":     False,
        "is_logging":          False,
        "log_validation":      False,
        "include_global":      False,
        "has_custom_selectors": False,
        "kms_key_id":          "",
        "latest_delivery":     "",
        "latest_error":        "",
        "status":              "",
        "retention_days":      0,
        "creation_time":       "",
        "access_denied":       False,
        "not_found":           False,
    }
    try:
        if resource_type == "Trail":
            resp   = client.describe_trails(trailNameList=[resource_name])
            trails = resp.get("trailList") or []
            if not trails:
                result["not_found"] = True
                return result

            trail = trails[0]
            result["arn"]                  = trail.get("TrailARN", "")
            result["home_region"]          = trail.get("HomeRegion", "")
            result["s3_bucket"]            = trail.get("S3BucketName", "")
            result["log_group_arn"]        = trail.get("CloudWatchLogsLogGroupArn", "")
            result["is_multi_region"]      = trail.get("IsMultiRegionTrail", False)
            result["is_organization"]      = trail.get("IsOrganizationTrail", False)
            result["log_validation"]       = trail.get("LogFileValidationEnabled", False)
            result["kms_key_id"]           = trail.get("KMSKeyId", "")
            result["include_global"]       = trail.get("IncludeGlobalServiceEvents", True)
            result["has_custom_selectors"] = trail.get("HasCustomEventSelectors", False)

            try:
                status = client.get_trail_status(Name=resource_name)
                result["is_logging"]    = status.get("IsLogging", False)
                latest = status.get("LatestDeliveryTime")
                result["latest_delivery"] = latest.isoformat() if latest else ""
                result["latest_error"]    = status.get("LatestDeliveryError", "")
            except Exception:
                pass

        elif resource_type == "EventDataStore":
            resp = client.get_event_data_store(EventDataStore=resource_name)
            result["arn"]            = resp.get("EventDataStoreArn", "")
            result["status"]         = resp.get("Status", "")
            result["is_multi_region"] = resp.get("MultiRegionEnabled", False)
            result["is_organization"] = resp.get("OrganizationEnabled", False)
            result["retention_days"] = resp.get("RetentionPeriod", 0)
            ct = resp.get("CreatedTimestamp")
            result["creation_time"]  = ct.isoformat() if ct else ""

    except Exception as exc:
        msg = str(exc)
        if "AccessDenied" in msg or "AccessDeniedException" in msg:
            result["access_denied"] = True
            print(f"    Cannot describe {resource_type} {resource_name}: {type(exc).__name__}")
        elif (
            "TrailNotFoundException" in msg
            or "EventDataStoreNotFoundException" in msg
            or "does not exist" in msg.lower()
        ):
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
        "EventName":         "CloudTrailResourceInventory",
        "EventSource":       "cloudtrail-local-enumeration",
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
        print("Missing required credentials. Set AWS_CLOUDTRAIL_ACCESS_KEY_ID / AWS_CLOUDTRAIL_SECRET_ACCESS_KEY in .env")
        return

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    timestamp   = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H-%M-%S")
    output_file = OUTPUT_DIR / f"cloudtrail_logs_{timestamp}.json"

    end_time   = datetime.now(timezone.utc)
    start_time = end_time - timedelta(hours=LOOKBACK_HOURS)

    print(f"Region : {AWS_REGION}")
    print(f"Window : {start_time.strftime('%Y-%m-%dT%H:%M:%SZ')} -> {end_time.strftime('%Y-%m-%dT%H:%M:%SZ')}\n")

    print("Checking CloudTrail availability...")
    check_cloudtrail_availability()
    print()

    all_events: list[dict] = []
    for source in CLOUDTRAIL_EVENT_SOURCES:
        print(f"Fetching CloudTrail events: {source}")
        try:
            raw_events = fetch_events_for_source(source, start_time, end_time)
            normalised = [normalize_event(e) for e in raw_events]
            all_events.extend(normalised)
            print(f"  {len(normalised)} events from {source}\n")
        except Exception as exc:
            print(f"  Error fetching {source}: {exc}\n")

    unique_resources = extract_unique_resources(all_events)
    print(f"Describing {len(unique_resources)} unique CloudTrail resources...")

    for resource_ref in unique_resources:
        rtype = resource_ref["resource_type"]
        rname = resource_ref["resource_name"]
        print(f"  -> {rtype}/{rname}")
        details = describe_resource(rtype, rname)
        if not details["access_denied"] and not details["not_found"]:
            if rtype == "Trail":
                logging_state  = "LOGGING" if details.get("is_logging") else "NOT LOGGING"
                multi          = "multi-region" if details.get("is_multi_region") else "single-region"
                s3             = details.get("s3_bucket", "")
                print(f"    {logging_state}, {multi}, s3={s3}")
            elif rtype == "EventDataStore":
                status         = details.get("status", "")
                retention      = details.get("retention_days", 0)
                print(f"    status={status}, retention={retention} days")
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
