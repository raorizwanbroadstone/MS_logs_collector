# AWS CloudTrail — Log Collector

Connects to **AWS CloudTrail** via boto3, fetches the last 24 hours of CloudTrail management events (changes to trails and event data stores), describes each discovered resource in full, and generates a **CycloneDX 1.6 Bill of Materials** report.

This collector audits your **audit infrastructure** — it tells you exactly what is being logged, where logs are delivered, whether logging is active, and whether tamper detection is enabled.

---

## Structure

```
AWS CloudTrail/
├── fetch_cloudtrail_logs.py    # Queries CloudTrail for trail/EDS events and 
├── generate_bom.py             # Streams logs, deduplicates entities, produces 
├── logs/                       # Output: timestamped raw CloudTrail JSON
└── report/                     # Output: timestamped CycloneDX BOM reports
```

---

## Setup

Add the following to the root `.env` file:

```env
AWS_DEFAULT_REGION=us-west-2
AWS_CLOUDTRAIL_ACCESS_KEY_ID=<your-key-id>
AWS_CLOUDTRAIL_SECRET_ACCESS_KEY=<your-secret>
```

### How to create the IAM user and generate credentials

**Step 1 — Create the IAM user**

1. Open the [AWS IAM Console](https://console.aws.amazon.com/iam/)
2. Go to **Users** → **Create user**
3. Enter a username (e.g. `cloudtrail-bom-collector`) → **Next**
4. Select **Attach policies directly**
5. Search for and attach the managed policy:
   - `AWSCloudTrail_ReadOnlyAccess`
6. **Next** → **Create user**

**Step 2 — Generate access keys**

1. In IAM, open the user you just created
2. Go to **Security credentials** → **Create access key**
3. Select **Application running outside AWS** → **Next** → **Create access key**
4. Copy the **Access key ID** → set as `AWS_CLOUDTRAIL_ACCESS_KEY_ID`
5. Copy the **Secret access key** → set as `AWS_CLOUDTRAIL_SECRET_ACCESS_KEY`

> The secret key is shown only once. Store it immediately.

**Step 3 — Set your region**

Your region code is shown in the top-right of the AWS Console (e.g. `us-west-2`, `eu-north-1`). Set it as `AWS_DEFAULT_REGION`.

---

## Required IAM permissions

A single AWS managed policy covers everything this collector needs:

| Managed Policy | Why Needed |
| --- | --- |
| `AWSCloudTrail_ReadOnlyAccess` | Grants `cloudtrail:LookupEvents`, `cloudtrail:DescribeTrails`, `cloudtrail:GetTrailStatus`, `cloudtrail:GetEventDataStore` — covers event history, trail describe, logging status, and event data store describe in one policy |

---

## Usage

```bash
# Run from the AWS CloudTrail directory with the project venv activated
python fetch_cloudtrail_logs.py
```

Output files are written to `logs/` and `report/` with timestamps in their filenames. `generate_bom.py` can also be run standalone to reprocess all existing files in `logs/`.

---

## How It Works

`fetch_cloudtrail_logs.py` executes the following pipeline on each run:

1. Verifies CloudTrail is reachable via `DescribeTrails`
2. Pages through `cloudtrail:LookupEvents` for `cloudtrail.amazonaws.com` across a 24-hour window
3. Normalises each event: converts `EventTime` to ISO-8601 and unpacks the embedded `CloudTrailEvent` JSON string
4. Extracts every unique resource reference (trails and event data stores) from `requestParameters`, disambiguating by event name where needed
5. Calls `DescribeTrails` + `GetTrailStatus` for each trail, and `GetEventDataStore` for each event data store
6. Appends a synthetic `CloudTrailResourceInventory` event per resource so the BOM generator can include describe output without a separate read pass
7. Writes all events to `logs/cloudtrail_logs_<timestamp>.json`
8. Invokes `generate_bom.py` to produce `report/bom_<timestamp>.json`

`generate_bom.py` streams log events using `ijson`, deduplicates entities via a Bloom filter backed by an exact set, tracks every resource each IAM principal accessed across all events (not just first occurrence), and serialises the results into a CycloneDX 1.6 document.

**Resource types collected:**

| Resource Type | Extracted from | Describe API | BOM Properties |
| --- | --- | --- | --- |
| Trail | `trailName` / `name` | `DescribeTrails` + `GetTrailStatus` | ARN, home region, S3 bucket, log group, is logging, multi-region, org trail, log validation, KMS key, custom event selectors, latest delivery time, delivery errors |
| EventDataStore | `name` / `eventDataStore` | `GetEventDataStore` | ARN, status, multi-region, org, retention period, creation time |

**CloudTrail events captured (examples):**

| Event Name | What Changed |
| --- | --- |
| `CreateTrail` | New trail created |
| `UpdateTrail` | Trail configuration modified (S3 bucket, log group, etc.) |
| `StartLogging` | Trail logging enabled |
| `StopLogging` | Trail logging **disabled** — high-value security signal |
| `DeleteTrail` | Trail permanently deleted |
| `PutEventSelectors` | Data event logging configuration changed |
| `PutInsightSelectors` | CloudTrail Insights enabled or disabled |
| `CreateEventDataStore` | CloudTrail Lake event data store created |
| `UpdateEventDataStore` | Event data store configuration modified |
| `DeleteEventDataStore` | Event data store deleted |

> **Security note:** `StopLogging` and `DeleteTrail` events are critical signals — they indicate someone disabled your audit log. The BOM property `aws:IsLogging: false` highlights trails that are not currently active.

---

## Troubleshooting

| Error | Cause | Fix |
| --- | --- | --- |
| `NoCredentialsError` | Credentials not set in `.env` | Add `AWS_CLOUDTRAIL_ACCESS_KEY_ID` and `AWS_CLOUDTRAIL_SECRET_ACCESS_KEY` to `MS_logs_collector/.env` |
| `AccessDenied: cloudtrail:LookupEvents` | IAM user missing the managed policy | Attach `AWSCloudTrail_ReadOnlyAccess` to the IAM user |
| `AccessDenied: cloudtrail:DescribeTrails` | Same as above | Attach `AWSCloudTrail_ReadOnlyAccess` to the IAM user |
| 0 events from `cloudtrail.amazonaws.com` | No trail/EDS changes in the last 24 hours | Normal — events appear when trails are created, updated, started, or stopped |
| Trail `GetTrailStatus` returns no delivery time | Trail exists but has never delivered a log | Trail was recently created or has never been active |
| Resource listed as `NotFound` | Trail or EDS deleted between the CloudTrail event and the describe call | Expected — recorded in BOM with `InventoryStatus: NotFound` |
| Empty BOM | No CloudTrail management activity in the last 24 hours | Expected — BOM populates when trail configuration changes occur |
