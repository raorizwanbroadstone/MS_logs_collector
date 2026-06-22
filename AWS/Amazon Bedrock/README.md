# Amazon Bedrock — Log Collector & BOM Generator

This module connects to **AWS CloudTrail** via boto3, fetches the last 24 hours of API activity across all Amazon Bedrock event sources, deduplicates entities using a Bloom filter, and generates a **CycloneDX 1.6 Bill of Materials** report. It is the AWS equivalent of the Microsoft 365 log collector in this repository.

---

## Folder Structure

```
Amazon Bedrock/
├── fetch_bedrock_logs.py   # Stage 1 — CloudTrail collector, writes logs/
├── generate_bom.py         # Stage 2 — Bloom filter + CycloneDX BOM writer
├── logs/                   # Auto-created — timestamped raw CloudTrail JSON
│   └── bedrock_logs_YYYYMMDD_HHMMSS.json
└── report/                 # Auto-created — CycloneDX 1.6 BOM output
    └── bom_YYYYMMDD_HHMMSS.json
```

---

## Part 1 — AWS Account Setup

### 1.1 Why a Dedicated IAM User?

Rather than using root credentials or an existing user's keys, we created a dedicated IAM user named **`amazon_bedrock_log`** with the minimum permissions needed to read CloudTrail events and inspect Bedrock. This follows the principle of least privilege — if these credentials are ever compromised, the blast radius is limited to read-only observability actions.

### 1.2 How to Create the IAM User

1. Sign in to the **AWS Console** → search **IAM** → open it
2. Left sidebar → **Users** → **Create user**
3. Set **User name** to `amazon_bedrock_log`
4. On the "Set permissions" screen → choose **Attach policies directly**
5. Attach the following AWS-managed policy **and** the custom inline policy described below
6. Click through to **Create user**

### 1.3 Permissions Granted

#### AWS Managed Policy attached
```
ReadOnlyAccess
```
This gives broad read access across all AWS services — useful for future expansion of the collector beyond Bedrock.

#### Custom Inline Policy (minimum required)

Navigate to the user → **Add permissions** → **Create inline policy** → **JSON tab** → paste:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "CloudTrailReadBedrock",
      "Effect": "Allow",
      "Action": [
        "cloudtrail:LookupEvents",
        "cloudtrail:DescribeTrails",
        "cloudtrail:GetTrailStatus"
      ],
      "Resource": "*"
    },
    {
      "Sid": "BedrockReadOnly",
      "Effect": "Allow",
      "Action": [
        "bedrock:ListFoundationModels",
        "bedrock:GetFoundationModel",
        "bedrock:ListAgents",
        "bedrock:GetAgent",
        "bedrock:ListKnowledgeBases"
      ],
      "Resource": "*"
    }
  ]
}
```

**Why each permission:**

| Permission | Why it is needed |
|---|---|
| `cloudtrail:LookupEvents` | Core permission — queries the 90-day event history for Bedrock API calls |
| `cloudtrail:DescribeTrails` | Lets the script check if a paid CloudTrail trail exists (needed for InvokeModel data events) |
| `cloudtrail:GetTrailStatus` | Checks if the trail is actively logging |
| `bedrock:ListFoundationModels` | Used in `check_bedrock_availability()` to verify the region supports Bedrock |
| `bedrock:GetFoundationModel` | Future enrichment — fetch model metadata (provider, modalities, inference types) |
| `bedrock:ListAgents` | Enumerate Bedrock Agents in the account for BOM enrichment |
| `bedrock:GetAgent` | Fetch agent configuration details |
| `bedrock:ListKnowledgeBases` | Enumerate Bedrock Knowledge Bases |

### 1.4 Generating the Access Key

1. In IAM → Users → `amazon_bedrock_log` → **Security credentials** tab
2. Scroll to **Access keys** → **Create access key**
3. Use case: **Command Line Interface (CLI)** → confirm
4. AWS displays the **Access Key ID** and **Secret Access Key** exactly once
5. Copy both values — the secret cannot be retrieved again after you close this screen
6. If you lose it, delete the key and create a new one

### 1.5 Storing Credentials in `.env`

The credentials are stored in the **project-root** `.env` file at `MS_logs_collector/.env`. AWS credentials use custom-prefixed names (`AWS_BEDROCK_`) so they cannot be accidentally picked up by any other AWS SDK running elsewhere on the machine that looks for the standard `AWS_ACCESS_KEY_ID` variable name.

```
AWS_DEFAULT_REGION=eu-north-1
AWS_BEDROCK_ACCESS_KEY_ID=AKIA...your_key_id...
AWS_BEDROCK_SECRET_ACCESS_KEY=...your_secret...
```

The script loads this file with an explicit path:

```python
load_dotenv(Path(__file__).parent.parent.parent / ".env")
```

`Path(__file__).parent` = `Amazon Bedrock/`  
`Path(__file__).parent.parent` = `AWS/`  
`Path(__file__).parent.parent.parent` = `MS_logs_collector/`  → where `.env` lives

After loading, `os.getenv("AWS_BEDROCK_ACCESS_KEY_ID")` returns the key, and it is passed **explicitly** to every `boto3.client()` call — boto3 never touches `os.environ["AWS_ACCESS_KEY_ID"]` at all.

---

## Part 2 — How the System Works

### 2.1 Overall Data Flow

```
AWS CloudTrail (eu-north-1)
        │
        │  lookup_events API
        │  (3 Bedrock event sources, 24h window)
        ▼
fetch_bedrock_logs.py
        │
        │  normalize_event() — flatten boto3 dict + parse embedded JSON
        │
        ▼
logs/bedrock_logs_YYYYMMDD_HHMMSS.json
        │
        │  ijson streaming (memory-efficient)
        ▼
generate_bom.py
        │
        │  extract_foundation_model()  → DeduplicatingSet (Bloom filter + exact set)
        │  extract_iam_principal()     → DeduplicatingSet
        │  extract_bedrock_agent()     → DeduplicatingSet
        │
        ▼
report/bom_YYYYMMDD_HHMMSS.json  (CycloneDX 1.6)
```

---

## Part 3 — Script: `fetch_bedrock_logs.py`

### 3.1 Purpose

Authenticates to AWS using the IAM credentials from `.env`, queries CloudTrail's `lookup_events` API across three Bedrock event sources, normalises each event into a flat JSON-serialisable dict, and writes the result to `logs/`.

### 3.2 CloudTrail vs. Bedrock Direct API

CloudTrail is AWS's audit log service — every API call made to any AWS service (including Bedrock) is recorded in CloudTrail automatically. We query CloudTrail rather than Bedrock directly because:

- Bedrock's own APIs only return current resource state (what models/agents exist now), not history
- CloudTrail gives us **who called what, when, from where, and whether it succeeded**
- CloudTrail event history is free for the last 90 days (management events)

### 3.3 Event Sources

```python
BEDROCK_EVENT_SOURCES = [
    "bedrock.amazonaws.com",                # InvokeModel, ListFoundationModels, CreateModelCustomizationJob
    "bedrock-agent.amazonaws.com",          # CreateAgent, CreateKnowledgeBase, CreatePrompt
    "bedrock-agent-runtime.amazonaws.com",  # InvokeAgent, Retrieve, RetrieveAndGenerate
]
```

Each source maps to a different Bedrock API surface:

| Source | What it covers |
|---|---|
| `bedrock.amazonaws.com` | Core Bedrock — foundation model invocations, model listing, guardrails, customisation jobs |
| `bedrock-agent.amazonaws.com` | Bedrock Agents control plane — creating, updating, deleting agents, knowledge bases, prompts |
| `bedrock-agent-runtime.amazonaws.com` | Bedrock Agents data plane — actually invoking agents and querying knowledge bases |

### 3.4 `check_bedrock_availability()`

```python
def check_bedrock_availability() -> bool:
```

Calls `bedrock:ListFoundationModels` in the configured region. This is a cheap read-only call used purely as a connectivity probe. The function distinguishes between:

- **Endpoint resolution failure** (`EndpointResolutionError`, `UnknownEndpoint`) → Bedrock is not available in this region
- **Auth error** (`AccessDeniedException`, `NoCredentialsError`) → Bedrock endpoint exists but credentials are wrong or missing permissions
- **Success** → region is fully supported

This is important because Bedrock is not available in every AWS region. If it is absent, all `InvokeModel` calls would fail at the client level before reaching CloudTrail.

### 3.5 `fetch_events_for_source(source)`

```python
def fetch_events_for_source(source: str) -> list[dict]:
```

Calls `cloudtrail:LookupEvents` with:
- `AttributeKey: "EventSource"` → filters to only the given Bedrock source
- `StartTime` / `EndTime` → the 24-hour window computed at startup
- `MaxResults: 50` → CloudTrail's max per page; the function handles pagination automatically via `NextToken`

**Why pagination matters:** CloudTrail returns a maximum of 50 events per API call. An active account can generate thousands of Bedrock events per day. The `while True` loop consumes every page until `NextToken` is absent.

**Management events vs. data events:**
CloudTrail's free event history contains **management events** (control-plane calls: creating agents, listing models, etc.). Actual model invocations (`InvokeModel`, `InvokeAgent` runtime calls) are **data events** and only appear if a paid CloudTrail Trail has been configured with Bedrock data event logging enabled. The script fetches whatever is available — on a free-tier account this means management events only.

### 3.6 `normalize_event(raw)`

```python
def normalize_event(raw: dict) -> dict:
```

boto3's `lookup_events` returns a dict where:
- `EventTime` is a Python `datetime` object (not a string) — must be serialised to ISO-8601 for JSON
- `CloudTrailEvent` is a **JSON string** — the full CloudTrail event record embedded as a string inside the outer dict

This function unpacks that structure:

```
boto3 raw event
├── EventId        → copied directly
├── EventName      → copied directly
├── EventTime      → .isoformat() → string
├── EventSource    → copied directly
├── Username       → copied directly
├── Resources      → copied directly
└── CloudTrailEvent (JSON string) → json.loads() →
    ├── userIdentity      → merged to top level
    ├── requestParameters → merged to top level
    ├── responseElements  → merged to top level
    ├── awsRegion         → merged to top level
    ├── sourceIPAddress   → merged to top level
    ├── errorCode         → merged to top level
    └── errorMessage      → merged to top level
```

After normalisation every event is a flat, JSON-serialisable dict. The output file is a JSON array of these dicts.

---

## Part 4 — Script: `generate_bom.py`

### 4.1 Purpose

Streams the log file produced by `fetch_bedrock_logs.py`, extracts three categories of BOM-relevant entities, deduplicates them using a Bloom filter, and writes a CycloneDX 1.6 Bill of Materials report.

### 4.2 Why a Bloom Filter?

A naive deduplication approach would be: `if key not in seen_set: seen_set.add(key)`. For small log files this is fine. For large files (millions of events with hundreds of thousands of unique keys), an exact hash set uses memory proportional to the number of unique items stored.

A **Bloom filter** is a probabilistic data structure that answers "have I seen this key before?" in O(k) time using a fixed-size bit array — regardless of how many items have been inserted. The trade-off is a configurable **false positive rate**: it can occasionally say "yes, seen it" when it hasn't. This is resolved by a small exact backing set.

```
BLOOM_CAPACITY = 500_000   # max unique items expected
BLOOM_FPR      = 0.0001    # 0.01% false positive rate
```

With these parameters, the bit array is approximately **1.2 MB** and uses **13 hash functions**. An exact set of the same 500,000 keys would use ~40 MB of memory (Python `set` overhead ~80 bytes per string).

### 4.3 `BloomFilter`

```python
class BloomFilter:
    def __init__(self, capacity: int, fpr: float):
        m = ceil(-(capacity * ln(fpr)) / (ln(2)²))   # optimal bit array size
        k = max(1, round((m / capacity) * ln(2)))      # optimal number of hash functions
```

The bit array size formula minimises the false positive rate for a given capacity. With `capacity=500_000` and `fpr=0.0001`:
- `m` ≈ 9,575,656 bits (~1.2 MB)
- `k` = 13 hash functions

**Double-hashing with MurmurHash3:**

```python
def _positions(self, key: str) -> list[int]:
    h1 = mmh3.hash(key, seed=0, signed=False)
    h2 = mmh3.hash(key, seed=1, signed=False)
    return [(h1 + i * h2) % self._m for i in range(self._k)]
```

Two independent hashes are computed once, then combined arithmetically to produce `k` positions. MurmurHash3 is used because it is non-cryptographic (fast), has excellent bit distribution, and is deterministic (same seed → same hash across runs).

**Guarantee:** `might_contain(key)` returning `False` means the key is **definitely not** in the filter. It is mathematically impossible to have a false negative. False positives (returning `True` for a key never inserted) occur at rate ≤ 0.01%.

### 4.4 `DeduplicatingSet`

```python
class DeduplicatingSet:
    def add_if_new(self, key: str) -> bool:
        if self._bloom.might_contain(key) and key in self._seen:
            return False    # definitely a duplicate
        self._bloom.add(key)
        self._seen.add(key)
        return True         # definitely new
```

Two-layer strategy:
1. **Bloom filter fast path** — 99.99% of new keys are caught here in O(k) with no hash set lookup
2. **Exact backing set** — the 0.01% of false positives are resolved by the exact `set`, guaranteeing zero duplicates in the output

The backing set `self._seen` grows only with unique keys, so its size equals the actual number of distinct entities — typically far smaller than the number of events.

### 4.5 Entity Extractors

#### `extract_foundation_model(event)`

Looks for `requestParameters.modelId` or `requestParameters.modelArn` in any Bedrock event. The model ID encodes the provider:

```
anthropic.claude-3-sonnet-20240229-v1:0
│         │
│         └── model family + version
└── provider (anthropic, amazon, meta, ai21, cohere, etc.)
```

The provider is extracted as `model_id.split(".")[0]` and stored as a separate `provider` field. This becomes the `aws:ModelProvider` property in the BOM.

#### `extract_iam_principal(event)`

Reads the `userIdentity` block present in every CloudTrail event:

| `type` value | Dedup key used | Why |
|---|---|---|
| `IAMUser` | Full user ARN | Each IAM user has a globally unique ARN |
| `AssumedRole` | Role ARN from `sessionContext.sessionIssuer.arn` | Each `AssumeRole` session has a unique session ARN but all sessions from the same role share the same role ARN — collapsing them to one BOM component is correct |
| `Root` | `arn:aws:iam::{accountId}:root` | Synthetic ARN for the root account |
| Other | `arn` or `principalId` | Federation, service accounts, etc. |

The extractor also captures `observed_model` — the model ID from `requestParameters` in the same event. This is used later in `build_dependency_graph()` to draw a direct edge from the IAM principal to the specific model it called, rather than linking everyone to everything.

#### `extract_bedrock_agent(event)`

Only fires on events from `bedrock-agent.amazonaws.com` or `bedrock-agent-runtime.amazonaws.com`. The `agentId` in `requestParameters` is the unique dedup key. The `agentAliasId` is also captured — agents can have multiple published versions (aliases), and each alias can point to a different agent version.

### 4.6 CycloneDX 1.6 BOM Structure

CycloneDX is an OWASP standard format for Software Bill of Materials (SBOM). The output JSON follows this structure:

```json
{
  "bomFormat": "CycloneDX",
  "specVersion": "1.6",
  "serialNumber": "urn:uuid:...",
  "metadata": {
    "component": {
      "bom-ref": "root-aws-account",
      "name": "AWS Account",
      "properties": [
        {"name": "aws:AccountId",   "value": "986601184113"},
        {"name": "aws:SourceFiles", "value": "bedrock_logs_20260622_083319.json"}
      ]
    }
  },
  "components": [
    {
      "type": "application",
      "bom-ref": "iam_principal-arn:aws:iam::986601184113:user/agentic-access",
      "name": "agentic-access",
      "properties": [
        {"name": "aws:IAMPrincipalARN", "value": "arn:aws:iam::986601184113:user/agentic-access"},
        {"name": "aws:IdentityType",    "value": "IAMUser"},
        {"name": "aws:AccountId",       "value": "986601184113"},
        {"name": "aws:EventSource",     "value": "bedrock.amazonaws.com"}
      ]
    }
  ],
  "services": [
    {
      "bom-ref": "model-anthropic.claude-3-sonnet-20240229-v1-0",
      "name": "anthropic.claude-3-sonnet-20240229-v1:0",
      "authenticated": true,
      "properties": [
        {"name": "aws:ModelProvider", "value": "anthropic"},
        {"name": "aws:ModelId",       "value": "anthropic.claude-3-sonnet-20240229-v1:0"},
        {"name": "aws:EventSource",   "value": "bedrock.amazonaws.com"}
      ]
    }
  ],
  "dependencies": [
    {
      "ref": "root-aws-account",
      "dependsOn": ["model-anthropic.claude-3-sonnet-20240229-v1-0"]
    },
    {
      "ref": "iam_principal-arn:aws:iam::986601184113:user/agentic-access",
      "dependsOn": ["model-anthropic.claude-3-sonnet-20240229-v1-0"]
    }
  ]
}
```

**Mapping to CycloneDX concepts:**

| BOM element | What it represents | Bedrock equivalent |
|---|---|---|
| Root `component` | The system boundary | AWS Account |
| `services[]` | External services consumed | Foundation models (Claude, Titan, Llama…) |
| `components[]` | Software/application entities | IAM principals + Bedrock Agents |
| `dependencies[]` | Who uses what | Account → models, Principal → model observed calling |

**Why models are `services` not `components`:** In CycloneDX, `services` are external capabilities consumed by the system (APIs, external platforms). Foundation models are exactly this — they are hosted by AWS/Anthropic/Meta and consumed via API. IAM principals and agents are internal actors, making them `components`.

**`bom-ref` sanitisation:** Model IDs contain colons (e.g., `anthropic.claude-3-sonnet-20240229-v1:0`) which some BOM parsers treat specially. The `_make_bom_ref()` function replaces `:` and `/` with `-` in all `bom-ref` values.

### 4.7 `stream_events()` — Memory-Efficient Streaming

```python
def stream_events(log_file: Path):
    with log_file.open("rb") as fh:
        yield from ijson.items(fh, "item")
```

`ijson` is an incremental JSON parser. Instead of loading the entire log file into memory (`json.load()`), it parses the JSON array token-by-token and yields one event dict at a time. For large log files (millions of events, hundreds of MB) this keeps RAM usage flat regardless of file size — only one event is in memory at any moment.

---

## Part 5 — CloudTrail Event Schema

Each event in `logs/bedrock_logs_*.json` follows this structure after normalisation:

| Field | Type | Description |
|---|---|---|
| `EventId` | UUID string | CloudTrail's unique identifier for this event |
| `EventName` | string | The API call made (e.g., `ListFoundationModels`, `InvokeModel`) |
| `EventTime` | ISO-8601 string | When the API call occurred (UTC) |
| `EventSource` | string | Which AWS service received the call (`bedrock.amazonaws.com`, etc.) |
| `Username` | string | IAM username shortcut (same as `userIdentity.userName`) |
| `Resources` | array | AWS resources involved (ARNs) |
| `userIdentity` | object | Full identity of the caller (see below) |
| `requestParameters` | object | Parameters sent with the API call |
| `responseElements` | object | Response returned (may be `null` for read-only calls) |
| `awsRegion` | string | Region where the call was processed |
| `sourceIPAddress` | string | Client IP or AWS service name |
| `errorCode` | string | Empty on success; AWS error code on failure (e.g., `AccessDenied`) |
| `errorMessage` | string | Human-readable error detail |

### `userIdentity` object

| Field | Description |
|---|---|
| `type` | `IAMUser`, `AssumedRole`, `Root`, `FederatedUser`, `AWSService` |
| `principalId` | Unique principal ID (stable, unlike ARN for assumed roles) |
| `arn` | Full ARN of the caller |
| `accountId` | AWS account number |
| `accessKeyId` | Access key used for this call |
| `userName` | Short username (IAMUser type only) |
| `sessionContext` | Present for AssumedRole — contains `sessionIssuer` (role info) and `attributes` (MFA status, session creation time) |

### Common Bedrock EventNames

| EventName | Source | Description |
|---|---|---|
| `ListFoundationModels` | `bedrock.amazonaws.com` | List all available foundation models |
| `GetUseCaseForModelAccess` | `bedrock.amazonaws.com` | Check Anthropic model access form status |
| `InvokeModel` | `bedrock.amazonaws.com` | Synchronous model invocation (data event — needs Trail) |
| `InvokeModelWithResponseStream` | `bedrock.amazonaws.com` | Streaming model invocation (data event) |
| `Converse` | `bedrock.amazonaws.com` | Unified conversation API across models |
| `CreateAgent` | `bedrock-agent.amazonaws.com` | Create a new Bedrock Agent |
| `InvokeAgent` | `bedrock-agent-runtime.amazonaws.com` | Invoke a Bedrock Agent (data event) |
| `Retrieve` | `bedrock-agent-runtime.amazonaws.com` | Query a Knowledge Base |

### Sample Event from Live Logs

```json
{
  "EventId": "f64bf16b-eb08-4bef-ba59-293dd17073cc",
  "EventName": "ListFoundationModels",
  "EventTime": "2026-06-22T13:06:36+05:00",
  "EventSource": "bedrock.amazonaws.com",
  "Username": "agentic-access",
  "Resources": [],
  "userIdentity": {
    "type": "IAMUser",
    "principalId": "AIDA6LNQDLNYQWKABZEKD",
    "arn": "arn:aws:iam::986601184113:user/agentic-access",
    "accountId": "986601184113",
    "accessKeyId": "ASIA6LNQDLNYTPYUKEOC",
    "userName": "agentic-access",
    "sessionContext": {
      "attributes": {
        "creationDate": "2026-06-22T08:05:30Z",
        "mfaAuthenticated": "false"
      }
    }
  },
  "requestParameters": {},
  "responseElements": {},
  "awsRegion": "eu-north-1",
  "sourceIPAddress": "203.128.27.61",
  "errorCode": "",
  "errorMessage": ""
}
```

---

## Part 6 — Running the Scripts

### Prerequisites

```bash
# From the project root, with venv activated
pip install boto3 mmh3 bitarray ijson python-dotenv
```

### Run the full pipeline

```bash
cd "AWS\Amazon Bedrock"
python fetch_bedrock_logs.py
```

`fetch_bedrock_logs.py` calls `generate_bom.main()` automatically after saving the log file. Both outputs are written in one run.

### Run BOM generation only (on existing logs)

```bash
cd "AWS\Amazon Bedrock"
python generate_bom.py
```

This processes all `*.json` files in `logs/` and writes a single combined BOM. Useful for reprocessing historical logs without re-fetching.

### Expected output

```
Region : eu-north-1
Window : 2026-06-21T08:24:47Z → 2026-06-22T08:24:47Z

Checking Bedrock availability...
  Bedrock is available in eu-north-1

Fetching CloudTrail events: bedrock.amazonaws.com
    Page 1: 50 events
    Page 2: 23 events
  73 events from bedrock.amazonaws.com

Fetching CloudTrail events: bedrock-agent.amazonaws.com
    Page 1: 4 events
  4 events from bedrock-agent.amazonaws.com

Fetching CloudTrail events: bedrock-agent-runtime.amazonaws.com
    Page 1: 0 events
  0 events from bedrock-agent-runtime.amazonaws.com

Saved 77 events → logs/bedrock_logs_20260622_083319.json

Generating BOM report...
Processing bedrock_logs_20260622_083319.json ...
  5 new components, 3 new models

Report : report/bom_20260622_083319.json
Total  : 5 components, 3 models
```

---

## Part 7 — Troubleshooting

| Error | Cause | Fix |
|---|---|---|
| `NoCredentialsError: Unable to locate credentials` | `.env` not found or not loaded; credentials not in `.env` | Verify `AWS_BEDROCK_ACCESS_KEY_ID` and `AWS_BEDROCK_SECRET_ACCESS_KEY` are set in `MS_logs_collector/.env` |
| `AccessDenied: not authorized to perform bedrock:ListFoundationModels` | IAM user missing the Bedrock inline policy | Attach the inline policy from Part 1.3 to the `amazon_bedrock_log` user |
| `AccessDenied` on `cloudtrail:LookupEvents` | Missing CloudTrail permission | Add `cloudtrail:LookupEvents` to the user's inline policy |
| `ResourceNotFoundException: You have not filled out the request form` | Anthropic models require a one-time use-case form | Go to AWS Console → Amazon Bedrock → Model catalog → select an Anthropic model → complete the use-case form |
| Events show 0 results for `bedrock-agent-runtime.amazonaws.com` | No agent runtime calls have been made, or they are data events requiring a Trail | Normal if no agents have been invoked; set up a CloudTrail Trail with Bedrock data events to capture `InvokeAgent` calls |
| `Bedrock is NOT available in eu-north-1` | Region does not yet support Bedrock | Switch `AWS_DEFAULT_REGION` to `us-east-1`, `eu-central-1`, or `eu-west-1` |
| Empty BOM with 0 components and 0 models | No Bedrock activity in the last 24 hours | Expected for a new setup — logs will populate once Bedrock starts being used |
