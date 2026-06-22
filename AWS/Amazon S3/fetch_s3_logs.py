"""
fetch_s3_logs.py

Stage 1 of two-stage pipeline:
  1. Fetch CloudTrail events for all S3 event sources (last 24h)
  2. Enumerate the contents of every discovered bucket (top-level prefixes,
     object count, storage class breakdown) via list_objects_v2
  3. Append the inventory as synthetic events to the log file so generate_bom.py
     can enrich each bucket service entry without needing live AWS access
  4. Call generate_bom.main() to produce the CycloneDX 1.6 BOM

Synthetic inventory events have EventSource "s3-local-enumeration" so they are
never confused with real CloudTrail records.

Dependencies: boto3, python-dotenv  (pip install boto3 python-dotenv)
"""

import boto3
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import os
from dotenv import load_dotenv
import generate_bom

load_dotenv(Path(__file__).parent.parent.parent / ".env")

AWS_REGION     = os.getenv("AWS_DEFAULT_REGION", "eu-north-1")
AWS_KEY_ID     = os.getenv("AWS_S3_ACCESS_KEY_ID")
AWS_SECRET_KEY = os.getenv("AWS_S3_SECRET_ACCESS_KEY")

S3_EVENT_SOURCES = [
    "s3.amazonaws.com",         # GetBucketLocation, ListBuckets, CreateBucket, PutBucketPolicy, etc.
    "s3control.amazonaws.com",  # Batch operations, access points, multi-region access points
]

# Max objects returned per bucket during content enumeration.
# Uses Delimiter='/' so only the first-level tree is fetched regardless of depth.
BUCKET_ENUM_MAX_KEYS = 1000

now        = datetime.now(timezone.utc)
START_TIME = now - timedelta(hours=24)

LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_FILE = LOG_DIR / f"s3_logs_{now.strftime('%Y%m%d_%H%M%S')}.json"


def _s3_client(region: str = "us-east-1"):
    """Returns a boto3 S3 client. Defaults to us-east-1 because ListBuckets
    is a global operation and some bucket regions need a region-aware client."""
    return boto3.client(
        "s3",
        region_name=region,
        aws_access_key_id=AWS_KEY_ID,
        aws_secret_access_key=AWS_SECRET_KEY,
    )


def check_s3_availability() -> bool:
    try:
        _s3_client().list_buckets()
        print(f"  S3 is reachable")
        return True
    except Exception as exc:
        msg = str(exc)
        if any(kw in msg.lower() for kw in ("endpoint", "could not connect")):
            print(f"  S3 connectivity issue: {type(exc).__name__}")
            return False
        print(f"  S3 endpoint reachable ({type(exc).__name__}: {msg[:120]})")
        return True


def fetch_events_for_source(source: str) -> list[dict]:
    """
    Pages through CloudTrail lookup_events for the given event source across
    the 24-hour window. Management events are free; data events (e.g. GetObject)
    require a configured Trail with S3 data event logging enabled.
    """
    client = boto3.client(
        "cloudtrail",
        region_name=AWS_REGION,
        aws_access_key_id=AWS_KEY_ID,
        aws_secret_access_key=AWS_SECRET_KEY,
    )
    events: list[dict] = []
    kwargs: dict = dict(
        LookupAttributes=[{"AttributeKey": "EventSource", "AttributeValue": source}],
        StartTime=START_TIME,
        EndTime=now,
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
    """Flattens a CloudTrail boto3 event dict into a JSON-serialisable form."""
    event_time = raw.get("EventTime", now)
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


def get_bucket_region(bucket_name: str) -> str:
    """Returns the AWS region a bucket resides in via GetBucketLocation."""
    try:
        resp = _s3_client().get_bucket_location(Bucket=bucket_name)
        # us-east-1 returns None from GetBucketLocation
        return resp.get("LocationConstraint") or "us-east-1"
    except Exception:
        return ""


def enumerate_bucket_contents(bucket_name: str, region: str) -> dict:
    """
    Lists top-level virtual folders (prefixes delimited by '/') and any
    direct top-level objects in the bucket. Uses MaxKeys=BUCKET_ENUM_MAX_KEYS
    so very large buckets are capped — IsTruncated will be True in that case.

    Returns a dict with:
      prefixes          — list of first-level "folder" paths  (e.g. ["logs/", "data/"])
      top_level_objects — list of {key, size_bytes, storage_class} for root-level files
      total_listed      — count of items returned (prefixes + objects)
      is_truncated      — True if the bucket has more than BUCKET_ENUM_MAX_KEYS entries
      access_denied     — True if s3:ListObjectsV2 was denied
    """
    result = {
        "prefixes":          [],
        "top_level_objects": [],
        "total_listed":      0,
        "is_truncated":      False,
        "access_denied":     False,
    }
    try:
        client = _s3_client(region or "us-east-1")
        resp   = client.list_objects_v2(
            Bucket=bucket_name,
            Delimiter="/",
            MaxKeys=BUCKET_ENUM_MAX_KEYS,
        )
        result["prefixes"] = [p["Prefix"] for p in resp.get("CommonPrefixes") or []]
        result["top_level_objects"] = [
            {
                "key":           obj["Key"],
                "size_bytes":    obj.get("Size", 0),
                "storage_class": obj.get("StorageClass", ""),
                "last_modified": obj["LastModified"].isoformat() if isinstance(obj.get("LastModified"), datetime) else str(obj.get("LastModified", "")),
            }
            for obj in resp.get("Contents") or []
        ]
        result["total_listed"] = resp.get("KeyCount", 0)
        result["is_truncated"] = resp.get("IsTruncated", False)
    except Exception as exc:
        msg = str(exc)
        if "AccessDenied" in msg or "NoSuchBucket" in msg or "AllAccessDisabled" in msg:
            result["access_denied"] = True
            print(f"    Cannot enumerate {bucket_name}: {type(exc).__name__}")
        else:
            print(f"    Error enumerating {bucket_name}: {exc}")
    return result


def extract_unique_buckets(events: list[dict]) -> list[str]:
    """Collects every distinct bucket name seen across all normalised events."""
    seen: set[str] = set()
    for event in events:
        params = event.get("requestParameters") or {}
        name = (
            params.get("bucketName")
            or params.get("Bucket")
            or params.get("bucket")
        )
        if name and name not in seen:
            seen.add(name)
    return sorted(seen)


def build_inventory_event(bucket_name: str, region: str, inventory: dict) -> dict:
    """
    Wraps bucket enumeration results in a synthetic event that looks like a
    CloudTrail event so generate_bom.py can stream it alongside real events.
    EventSource 's3-local-enumeration' is the discriminator used by the extractor.
    """
    return {
        "EventId":     f"inventory-{bucket_name}",
        "EventName":   "BucketContentsInventory",
        "EventSource": "s3-local-enumeration",
        "EventTime":   now.isoformat(),
        "Username":    "",
        "Resources":   [],
        "userIdentity":      {},
        "requestParameters": {"bucketName": bucket_name},
        "responseElements":  {},
        "awsRegion":         region,
        "sourceIPAddress":   "",
        "errorCode":         "AccessDenied" if inventory["access_denied"] else "",
        "errorMessage":      "",
        "inventory":         inventory,
    }


def main() -> None:
    print(f"Region : {AWS_REGION}")
    print(f"Window : {START_TIME.strftime('%Y-%m-%dT%H:%M:%SZ')} → {now.strftime('%Y-%m-%dT%H:%M:%SZ')}\n")

    print("Checking S3 availability...")
    check_s3_availability()
    print()

    # ── Stage 1: CloudTrail events ──────────────────────────────────────────
    all_events: list[dict] = []
    for source in S3_EVENT_SOURCES:
        print(f"Fetching CloudTrail events: {source}")
        try:
            raw_events = fetch_events_for_source(source)
            normalised = [normalize_event(e) for e in raw_events]
            all_events.extend(normalised)
            print(f"  {len(normalised)} events from {source}\n")
        except Exception as exc:
            print(f"  Error fetching {source}: {exc}\n")

    # ── Stage 2: Bucket content enumeration ─────────────────────────────────
    unique_buckets = extract_unique_buckets(all_events)
    print(f"Enumerating contents of {len(unique_buckets)} unique buckets...")

    for bucket_name in unique_buckets:
        print(f"  → {bucket_name}")
        region    = get_bucket_region(bucket_name)
        inventory = enumerate_bucket_contents(bucket_name, region)
        if not inventory["access_denied"]:
            print(f"    {len(inventory['prefixes'])} prefixes, "
                  f"{len(inventory['top_level_objects'])} root objects"
                  + (" (truncated)" if inventory["is_truncated"] else ""))
        all_events.append(build_inventory_event(bucket_name, region, inventory))

    # ── Write log file ───────────────────────────────────────────────────────
    with OUTPUT_FILE.open("w", encoding="utf-8") as f:
        json.dump(all_events, f, indent=2, ensure_ascii=False)

    print(f"\nSaved {len(all_events)} events → {OUTPUT_FILE}")

    print("\nGenerating BOM report...")
    generate_bom.main(target_file=OUTPUT_FILE)


if __name__ == "__main__":
    main()
