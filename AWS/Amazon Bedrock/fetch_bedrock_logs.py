"""
fetch_bedrock_logs.py

Connects to AWS CloudTrail via boto3, fetches the last 24 hours of events
from three Bedrock event sources, normalises each event into a flat JSON-
serialisable dict, writes the result to logs/, then calls generate_bom.main()
to produce a CycloneDX 1.6 BOM report.

Dependencies: boto3, python-dotenv  (pip install boto3 python-dotenv)
AWS credentials are read from the project-root .env file.
"""

import boto3
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import os
from dotenv import load_dotenv
import generate_bom

load_dotenv(Path(__file__).parent.parent.parent / ".env")

AWS_REGION     = os.getenv("AWS_DEFAULT_REGION",         "eu-north-1")
AWS_KEY_ID     = os.getenv("AWS_BEDROCK_ACCESS_KEY_ID",  "")
AWS_SECRET_KEY = os.getenv("AWS_BEDROCK_SECRET_ACCESS_KEY", "")

# CloudTrail event sources that cover all Bedrock activity
BEDROCK_EVENT_SOURCES = [
    "bedrock.amazonaws.com",                # InvokeModel, ListFoundationModels, etc.
    "bedrock-agent.amazonaws.com",          # CreateAgent, CreateKnowledgeBase, etc.
    "bedrock-agent-runtime.amazonaws.com",  # InvokeAgent, Retrieve, etc.
]

now        = datetime.now(timezone.utc)
START_TIME = now - timedelta(hours=24)

LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_FILE = LOG_DIR / f"bedrock_logs_{now.strftime('%Y%m%d_%H%M%S')}.json"


def check_bedrock_availability() -> bool:
    """
    Calls ListFoundationModels to verify that Bedrock is reachable in the
    configured region. Returns True even on auth errors — only endpoint
    resolution failures indicate the region is unsupported.
    """
    try:
        boto3.client("bedrock", region_name=AWS_REGION,
                     aws_access_key_id=AWS_KEY_ID,
                     aws_secret_access_key=AWS_SECRET_KEY).list_foundation_models()
        print(f"  Bedrock is available in {AWS_REGION}")
        return True
    except Exception as exc:
        msg = str(exc)
        if any(kw in msg for kw in ("EndpointResolutionError", "UnknownEndpoint", "Could not connect")):
            print(f"  Bedrock is NOT available in {AWS_REGION}.")
            print("  Confirmed Bedrock regions: us-east-1, us-west-2, eu-central-1, eu-west-1")
            return False
        print(f"  Bedrock endpoint reachable ({type(exc).__name__}: {msg[:120]})")
        return True


def fetch_events_for_source(source: str) -> list[dict]:
    """
    Pages through CloudTrail lookup_events for the given event source across
    the 24-hour window. Free CloudTrail event history covers the last 90 days
    of management events; data events (InvokeModel payloads) require a paid trail.
    """
    client = boto3.client("cloudtrail", region_name=AWS_REGION,
                          aws_access_key_id=AWS_KEY_ID,
                          aws_secret_access_key=AWS_SECRET_KEY)
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
    """
    Flattens a CloudTrail event dict returned by boto3.
    EventTime is a datetime object from boto3 — serialised to ISO-8601 string.
    CloudTrailEvent is a JSON string — parsed and merged into the top level.
    """
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


def main() -> None:
    print(f"Region : {AWS_REGION}")
    print(f"Window : {START_TIME.strftime('%Y-%m-%dT%H:%M:%SZ')} → {now.strftime('%Y-%m-%dT%H:%M:%SZ')}\n")

    print("Checking Bedrock availability...")
    check_bedrock_availability()
    print()

    all_events: list[dict] = []

    for source in BEDROCK_EVENT_SOURCES:
        print(f"Fetching CloudTrail events: {source}")
        try:
            raw_events = fetch_events_for_source(source)
            normalised = [normalize_event(e) for e in raw_events]
            all_events.extend(normalised)
            print(f"  {len(normalised)} events from {source}\n")
        except Exception as exc:
            print(f"  Error fetching {source}: {exc}\n")

    with OUTPUT_FILE.open("w", encoding="utf-8") as f:
        json.dump(all_events, f, indent=2, ensure_ascii=False)

    print(f"Saved {len(all_events)} events → {OUTPUT_FILE}")

    print("\nGenerating BOM report...")
    generate_bom.main(target_file=OUTPUT_FILE)


if __name__ == "__main__":
    main()
