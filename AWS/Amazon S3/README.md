# Amazon S3 — Log Collector & BOM Generator

This module connects to **AWS CloudTrail** via boto3, fetches the last 24 hours of S3 API activity, deduplicates entities using a Bloom filter, and generates a **CycloneDX 1.6 Bill of Materials** report. It is the S3 equivalent of the Amazon Bedrock collector in this repository.
---

## Folder Structure

```
Amazon S3/
├── fetch_s3_logs.py    # Stage 1 — CloudTrail collector, writes logs/
├── generate_bom.py     # Stage 2 — Bloom filter + CycloneDX BOM writer
├── logs/               # Auto-created — timestamped raw CloudTrail JSON
│   └── s3_logs_YYYYMMDD_HHMMSS.json
└── report/             # Auto-created — CycloneDX 1.6 BOM output
    └── s3_bom_YYYYMMDD_HHMMSS.json
```

---

## Part 1 — AWS Account Setup

### 1.1 IAM User

The same IAM user **`amazon_bedrock_log`** created for the Bedrock collector is reused here. The `cloudtrail:LookupEvents` permission already attached to that user covers ALL AWS event sources — including `s3.amazonaws.com` and `s3control.amazonaws.com`.

No separate IAM user is needed for S3.

### 1.2 Permissions Required

The collector requires both CloudTrail and S3 read permissions.

#### Option A — AWS Managed Policies (Recommended)

Attach the following AWS managed policies to the IAM user:

| Policy | Purpose |
|----------|----------|
| AWSCloudTrail_ReadOnlyAccess | Allows CloudTrail event lookup |
| AmazonS3ReadOnlyAccess | Allows bucket discovery and metadata retrieval |

#### Option B — Custom IAM Policy (Least Privilege)

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "CloudTrailRead",
      "Effect": "Allow",
      "Action": [
        "cloudtrail:LookupEvents",
        "cloudtrail:DescribeTrails"
      ],
      "Resource": "*"
    },
    {
      "Sid": "S3Read",
      "Effect": "Allow",
      "Action": [
        "s3:ListAllMyBuckets",
        "s3:GetBucketLocation",
        "s3:ListBucket"
      ],
      "Resource": "*"
    }
  ]
}
```

#### What Each Permission Does

| Permission | Why Needed |
|------------|------------|
| `cloudtrail:LookupEvents` | Fetches CloudTrail event history for S3 API activity |
| `cloudtrail:DescribeTrails` | Determines whether Trails are configured for data events |
| `s3:ListAllMyBuckets` | `check_s3_availability()` calls `list_buckets()` |
| `s3:GetBucketLocation` | `get_bucket_region()` determines which region a bucket resides in |
| `s3:ListBucket` | `enumerate_bucket_contents()` calls `list_objects_v2()` |

### 1.3 Credentials in `.env`

The script reads credentials from `MS_logs_collector/.env`. It first looks for S3-specific vars, then falls back to the Bedrock credentials if they are not set — since it is the same AWS account and the same IAM user doing the reading:

```
# Optional — set these to use a dedicated S3 log reader
AWS_S3_ACCESS_KEY_ID=your_key_here
AWS_S3_SECRET_ACCESS_KEY=your_secret_here

# If the above are absent, the script uses these automatically
AWS_BEDROCK_ACCESS_KEY_ID=AKIA...
AWS_BEDROCK_SECRET_ACCESS_KEY=...
```

Fallback logic in `fetch_s3_logs.py`:
```python
AWS_KEY_ID     = os.getenv("AWS_S3_ACCESS_KEY_ID")     or os.getenv("AWS_BEDROCK_ACCESS_KEY_ID", "")
AWS_SECRET_KEY = os.getenv("AWS_S3_SECRET_ACCESS_KEY") or os.getenv("AWS_BEDROCK_SECRET_ACCESS_KEY", "")
```

---

## Part 2 — How the System Works

### 2.1 Overall Data Flow

```
AWS CloudTrail (eu-north-1)
        │
        │  lookup_events API — two S3 event sources, 24h window
        ▼
fetch_s3_logs.py
        │
        │  normalize_event() — flatten boto3 dict + parse embedded JSON
        ▼
logs/s3_logs_YYYYMMDD_HHMMSS.json
        │
        │  ijson streaming (memory-efficient, one event at a time)
        ▼
generate_bom.py
        │
        │  extract_s3_bucket()      → DeduplicatingSet → services[]
        │  extract_iam_principal()  → DeduplicatingSet → components[]
        │  principal_buckets dict   → tracks ALL buckets per principal
        │
        ▼
report/s3_bom_YYYYMMDD_HHMMSS.json  (CycloneDX 1.6)
```

---

## Part 3 — Script: `fetch_s3_logs.py`

### 3.1 Event Sources

```python
S3_EVENT_SOURCES = [
    "s3.amazonaws.com",         # Core S3 — ListBuckets, GetBucketLocation, CreateBucket, PutBucketPolicy
    "s3control.amazonaws.com",  # S3 Control — batch operations, access points, multi-region access points
]
```

Both sources are queried in sequence and their events merged into one output file.

**Why two sources:**

| Source | What it covers |
|---|---|
| `s3.amazonaws.com` | All bucket-level and object-level S3 operations |
| `s3control.amazonaws.com` | S3 Batch Operations jobs, S3 Access Points, S3 Multi-Region Access Points — these are routed through a different API endpoint |

### 3.2 Management Events vs. Data Events

CloudTrail's free **Event History** (90-day rolling, no Trail needed) captures **management events** only:

| Event type | Examples | Requires paid Trail? |
|---|---|---|
| Management | `ListBuckets`, `CreateBucket`, `PutBucketPolicy`, `DeleteBucket`, `GetBucketLocation` | No — free |
| Data | `GetObject`, `PutObject`, `DeleteObject`, `HeadObject` | Yes — Trail with S3 data events enabled |

The script fetches whatever CloudTrail has. On a free-tier setup you get bucket-level events. Once a Trail is configured with data events, you also get object-level operations.

### 3.3 `check_s3_availability()`

Calls `s3:ListBuckets` as a connectivity probe. Distinguishes between:
- **Endpoint / connectivity failure** → S3 unreachable
- **Auth error** (`AccessDenied`) → S3 reachable but credentials lack permission
- **Success** → fully operational

### 3.4 `fetch_events_for_source(source)`

Pages through `cloudtrail:LookupEvents` with `AttributeKey: "EventSource"`. CloudTrail returns at most 50 events per page — the `while True / NextToken` loop ensures all pages are consumed.

### 3.5 `normalize_event(raw)`

Unpacks the boto3 response structure:
- `EventTime` (Python `datetime`) → `.isoformat()` string
- `CloudTrailEvent` (embedded JSON string) → `json.loads()` → merged to top level

Result is a flat, JSON-serialisable dict written to the log file.

---

## Part 4 — Script: `generate_bom.py`

### 4.1 Entity Types and Their BOM Placement

| Entity | CycloneDX placement | Rationale |
|---|---|---|
| **S3 Buckets** | `services[]` | Buckets are external storage services consumed by the account — equivalent to Foundation Models in the Bedrock BOM |
| **IAM Principals** | `components[]` | Internal actors (users/roles) that interact with the buckets |

S3 buckets use `to_cyclonedx_service()` and appear in `services[]` — NOT `components[]`. This is the correct CycloneDX model: a service is an external capability your system depends on; a component is an internal software element.

### 4.2 Bloom Filter

Identical to the Bedrock implementation:
- `BLOOM_CAPACITY = 500_000`, `BLOOM_FPR = 0.0001`
- MurmurHash3 double-hashing, ~1.2 MB bit array, 13 hash functions
- Backed by an exact set to guarantee zero duplicates

See the Bedrock README for the full mathematical explanation.

### 4.3 `extract_s3_bucket(event)`

Checks multiple field names for the bucket name (S3 uses inconsistent casing across API versions):

```python
bucket_name = (
    params.get("bucketName")    # most common — ListBuckets, GetBucketLocation
    or params.get("Bucket")     # some S3 control operations
    or params.get("bucket")     # lower-case variant
)
```

Falls back to the `Resources` array if `requestParameters` has no bucket name. Also captures `awsRegion` from the event — important because S3 buckets can be in any region regardless of where CloudTrail was queried.

### 4.4 `extract_iam_principal(event)`

Same implementation as the Bedrock version — handles `IAMUser`, `AssumedRole`, `Root`, and other identity types. Uses the role ARN (not session ARN) as the dedup key for `AssumedRole` so all sessions from the same role collapse to one BOM component.

### 4.5 Principal→Bucket Relationship Tracking

A key improvement over a naive design: the `principal_buckets` dict tracks **every** bucket each principal accessed — not just the first one seen.

```python
principal_buckets: dict[str, set[str]] = {}

# For every event (even if principal is already deduplicated):
if principal and bucket:
    principal_buckets.setdefault(principal["key"], set()).add(bucket["key"])
```

This dict is mutable and shared across all log files. Even after a principal has been deduplicated (skipped on second occurrence), subsequent events from that same principal continue updating `principal_buckets`. The dependency graph is built from this complete picture.

**Why this matters:** Without it, the IAM principal `agentic-access` would only show a dependency on `test-dontdelete` (the first bucket seen), even though it also accessed `cytex`, `entjedi`, `bstlegal`, etc.

### 4.6 CycloneDX 1.6 BOM Structure

```json
{
  "bomFormat": "CycloneDX",
  "specVersion": "1.6",
  "metadata": {
    "component": {
      "bom-ref": "root-aws-account",
      "name": "AWS Account",
      "properties": [
        {"name": "aws:AccountId",   "value": "986601184113"},
        {"name": "aws:SourceFiles", "value": "s3_logs_20260622_091502.json"}
      ]
    }
  },
  "components": [
    {
      "type": "application",
      "bom-ref": "iam_principal-arn-aws-iam--986601184113-user-agentic-access",
      "name": "agentic-access",
      "properties": [
        {"name": "aws:IAMPrincipalARN", "value": "arn:aws:iam::986601184113:user/agentic-access"},
        {"name": "aws:IdentityType",    "value": "IAMUser"},
        {"name": "aws:AccountId",       "value": "986601184113"}
      ]
    }
  ],
  "services": [
    {
      "bom-ref": "s3_bucket-test-dontdelete",
      "name": "test-dontdelete",
      "authenticated": true,
      "properties": [
        {"name": "aws:S3BucketName", "value": "test-dontdelete"},
        {"name": "aws:Region",       "value": "us-west-2"},
        {"name": "aws:EventSource",  "value": "s3.amazonaws.com"}
      ]
    }
  ],
  "dependencies": [
    {
      "ref": "root-aws-account",
      "dependsOn": ["s3_bucket-test-dontdelete", "s3_bucket-cytex", "...all buckets"]
    },
    {
      "ref": "iam_principal-arn-aws-iam--986601184113-user-agentic-access",
      "dependsOn": ["s3_bucket-test-dontdelete", "s3_bucket-cytex", "s3_bucket-entjedi", "..."]
    }
  ]
}
```

**Dependency graph logic:**
- `root-aws-account` → depends on **all** S3 buckets (the account owns them all)
- Each IAM principal → depends on **all buckets it accessed** (from `principal_buckets` dict)

---

## Part 5 — Live Log Sample

From `logs/s3_logs_20260622_091502.json` — 9 unique buckets observed, all accessed by `agentic-access`:

| Bucket | Region | Operation |
|---|---|---|
| `test-dontdelete` | us-west-2 | `GetBucketLocation` |
| `cytex` | us-west-2 | `GetBucketLocation` |
| `entjedi` | us-west-2 | `GetBucketLocation` |
| `jediar` | us-west-2 | `GetBucketLocation` |
| `cytex-security-scan-reports` | us-west-2 | `GetBucketLocation` |
| `cytex-cur-report` | us-west-2 | `GetBucketLocation` |
| `localaimodels` | us-west-2 | `GetBucketLocation` |
| `bstlegal` | us-west-2 | `GetBucketLocation` |
| `bstfintech` | us-west-2 | `GetBucketLocation` |

All buckets are in `us-west-2` (shown in `requestParameters.Host: s3.us-west-2.amazonaws.com`), while CloudTrail was queried in `eu-north-1`. This works because a multi-region trail aggregates events from all regions, and CloudTrail's `lookup_events` API searches the trail's global index.

---

## Part 6 — Running the Scripts

```bash
# From the Amazon S3 folder, with venv activated
python fetch_s3_logs.py
```

`fetch_s3_logs.py` automatically calls `generate_bom.main()` on completion.

To reprocess existing logs without re-fetching:

```bash
python generate_bom.py
```

---

## Part 7 — Troubleshooting

| Error | Cause | Fix |
|---|---|---|
| `NoCredentialsError` | Neither `AWS_S3_ACCESS_KEY_ID` nor `AWS_BEDROCK_ACCESS_KEY_ID` is set in `.env` | Add at least one credential pair to `MS_logs_collector/.env` |
| `AccessDenied` on `cloudtrail:LookupEvents` | IAM user missing CloudTrail permission | Add `cloudtrail:LookupEvents` to the `amazon_bedrock_log` user's inline policy |
| 0 events from `s3control.amazonaws.com` | No S3 Batch/Access Point operations in the last 24 hours | Normal — only fires when S3 Control APIs are used |
| Buckets appear in logs but not in BOM | `requestParameters` has no `bucketName` field | Check event's `Resources` array for `AWS::S3::Bucket` type entries; the extractor handles this automatically |
| Object-level events (`GetObject`, `PutObject`) missing | Data events require a paid CloudTrail Trail | Enable a Trail in the AWS Console → CloudTrail → Create trail → enable S3 data events |
