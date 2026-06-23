# Amazon Lambda — CloudTrail Log Collector

Connects to **AWS CloudTrail** via boto3, fetches the last 24 hours of Lambda API activity, describes each discovered function (runtime, handler, memory, timeout, architecture, layers, VPC config) and layer, and generates a **CycloneDX 1.6 Bill of Materials** report.

---

## Structure

```
Amazon Lambda/
├── fetch_lambda_logs.py    # Queries CloudTrail for Lambda events and describes 
├── generate_bom.py         # Streams logs, deduplicates entities, produces 
├── logs/                   # Output: timestamped raw CloudTrail JSON
└── report/                 # Output: timestamped CycloneDX BOM reports
```

---

## Setup

Add the following to the root `.env` file:

```env
AWS_DEFAULT_REGION=us-west-2
AWS_LAMBDA_ACCESS_KEY_ID=<your-key-id>
AWS_LAMBDA_SECRET_ACCESS_KEY=<your-secret>
```

### How to create the IAM user and generate credentials

**Step 1 — Create the IAM user**

1. Open the [AWS IAM Console](https://console.aws.amazon.com/iam/)
2. Go to **Users** → **Create user**
3. Enter a username (e.g. `lambda-bom-collector`) → **Next**
4. Select **Attach policies directly**
5. Search for and attach both managed policies:
   - `AWSLambda_ReadOnlyAccess`
   - `AWSCloudTrail_ReadOnlyAccess`
6. **Next** → **Create user**

**Step 2 — Generate access keys**

1. In IAM, open the user you just created
2. Go to **Security credentials** → **Create access key**
3. Select **Application running outside AWS** → **Next** → **Create access key**
4. Copy the **Access key ID** → set as `AWS_LAMBDA_ACCESS_KEY_ID`
5. Copy the **Secret access key** → set as `AWS_LAMBDA_SECRET_ACCESS_KEY`

> The secret key is shown only once. Store it immediately.

**Step 3 — Set your region**

Your Lambda region code is shown in the top-right of the AWS Console (e.g. `us-west-2`, `eu-north-1`). Set it as `AWS_DEFAULT_REGION`.

---

## Required IAM permissions

Attach these two AWS managed policies to the IAM user:

| Managed Policy | Why Needed |
| --- | --- |
| `AWSLambda_ReadOnlyAccess` | Grants `lambda:GetFunction`, `lambda:ListFunctions`, `lambda:ListLayerVersions` — covers the availability probe and all resource describe calls |
| `AWSCloudTrail_ReadOnlyAccess` | Grants `cloudtrail:LookupEvents` — fetches Lambda API activity from CloudTrail event history |

---

## Usage

```bash
# Run from the Amazon Lambda directory with the project venv activated
python fetch_lambda_logs.py
```

Output files are written to `logs/` and `report/` with timestamps in their filenames. `generate_bom.py` can also be run standalone to reprocess all existing files in `logs/`.

---

## How It Works

`fetch_lambda_logs.py` executes the following pipeline on each run:

1. Verifies Lambda is reachable via `ListFunctions`
2. Pages through `cloudtrail:LookupEvents` for `lambda.amazonaws.com` across a 24-hour window
3. Normalises each event: converts `EventTime` to ISO-8601 and unpacks the embedded `CloudTrailEvent` JSON string
4. Extracts every unique resource reference (functions and layers) from `requestParameters`
5. Calls `GetFunction` or `ListLayerVersions` for each resource to capture runtime, memory, timeout, architecture, state, layers, VPC, and log group
6. Appends a synthetic `LambdaResourceInventory` event per resource so the BOM generator can include describe output without a separate read pass
7. Writes all events to `logs/lambda_logs_<timestamp>.json`
8. Invokes `generate_bom.py` to produce `report/bom_<timestamp>.json`

`generate_bom.py` streams log events using `ijson`, deduplicates entities via a Bloom filter backed by an exact set, tracks every resource each IAM principal accessed across all events (not just first occurrence), and serialises the results into a CycloneDX 1.6 document.

**Resource types collected:**

| Resource Type | requestParameters key | Describe API | BOM Properties |
| --- | --- | --- | --- |
| Function | `functionName` | `GetFunction` | runtime, handler, memory, timeout, architecture, package type, state, VPC, layers, log group |
| Layer | `layerName` | `ListLayerVersions` | latest version ARN, compatible runtimes, created date |

**CloudTrail events captured (examples):**

| Event Name | Trigger |
| --- | --- |
| `CreateFunction20150331` | New function deployed |
| `UpdateFunctionCode20150331v2` | Function code updated |
| `UpdateFunctionConfiguration20150331v2` | Memory, timeout, env vars changed |
| `PublishVersion20150331` | New function version published |
| `CreateAlias20150331` | Alias created (e.g. `prod`, `staging`) |
| `AddPermission20150331v2` | Trigger or cross-account permission added |
| `PublishLayerVersion20181031` | New layer version published |
| `CreateEventSourceMapping` | Trigger wired (SQS, DynamoDB, Kinesis, etc.) |

> **Note:** Function invocation events (`InvokeFunction`) are data events and do not appear in free-tier CloudTrail event history. They require a paid CloudTrail Trail with Lambda data event logging enabled.

---

## Troubleshooting

| Error | Cause | Fix |
| --- | --- | --- |
| `NoCredentialsError` | Credentials not set in `.env` | Add `AWS_LAMBDA_ACCESS_KEY_ID` and `AWS_LAMBDA_SECRET_ACCESS_KEY` to `MS_logs_collector/.env` |
| `AccessDenied: cloudtrail:LookupEvents` | IAM user missing CloudTrail permission | Attach `AWSCloudTrail_ReadOnlyAccess` to the IAM user |
| `AccessDenied: lambda:GetFunction` | IAM user missing Lambda permission | Attach `AWSLambda_ReadOnlyAccess` to the IAM user |
| 0 events from `lambda.amazonaws.com` | No Lambda API calls in the last 24 hours | Normal — events appear once Lambda APIs are used (deploy, update, configure) |
| Resource listed as `NotFound` | Function deleted between the CloudTrail event and the describe call | Expected — recorded in BOM with `InventoryStatus: NotFound` |
| `InvokeFunction` events missing | Data events not enabled on a Trail | Configure a Trail in AWS Console → CloudTrail → enable Lambda data events |
| Empty BOM | No Lambda activity in the last 24 hours | Expected — logs populate once Lambda APIs are called |
