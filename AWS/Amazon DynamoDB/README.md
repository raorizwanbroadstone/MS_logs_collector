# Amazon DynamoDB — Log Collector

Connects to **Amazon DynamoDB** via boto3, fetches the last 24 hours of DynamoDB management events from CloudTrail, enumerates all current tables, describes each table with its full configuration and backup status, and generates a **CycloneDX 1.6 Bill of Materials** report.

---

## Structure

```
Amazon DynamoDB/
├── fetch_dynamodb_logs.py    # Queries CloudTrail for DynamoDB events and enumerates all tables
├── generate_bom.py           # Streams logs, deduplicates entities, produces CycloneDX BOM
├── logs/                     # Output: timestamped raw CloudTrail JSON
└── report/                   # Output: timestamped CycloneDX BOM reports
```

---

## Setup

Add the following to the root `.env` file:

```env
AWS_DEFAULT_REGION=us-west-2
AWS_DYNAMODB_ACCESS_KEY_ID=<your-key-id>
AWS_DYNAMODB_SECRET_ACCESS_KEY=<your-secret>
```

### How to create the IAM user and generate credentials

**Step 1 — Create the IAM user**

1. Open the [AWS IAM Console](https://console.aws.amazon.com/iam/)
2. Go to **Users** → **Create user**
3. Enter a username (e.g. `dynamodb-bom-collector`) → **Next**
4. Select **Attach policies directly**
5. Search for and attach these two managed policies:
   - `AmazonDynamoDBReadOnlyAccess`
   - `AWSCloudTrail_ReadOnlyAccess`
6. **Next** → **Create user**

**Step 2 — Generate access keys**

1. In IAM, open the user you just created
2. Go to **Security credentials** → **Create access key**
3. Select **Application running outside AWS** → **Next** → **Create access key**
4. Copy the **Access key ID** → set as `AWS_DYNAMODB_ACCESS_KEY_ID`
5. Copy the **Secret access key** → set as `AWS_DYNAMODB_SECRET_ACCESS_KEY`

> The secret key is shown only once. Store it immediately.

**Step 3 — Set your region**

Your region code is shown in the top-right of the AWS Console (e.g. `us-west-2`, `eu-north-1`). Set it as `AWS_DEFAULT_REGION`.

---

## Required IAM permissions

| Managed Policy | Why Needed |
| --- | --- |
| `AmazonDynamoDBReadOnlyAccess` | Grants `dynamodb:ListTables`, `dynamodb:DescribeTable`, `dynamodb:DescribeContinuousBackups` — covers full table enumeration and describe |
| `AWSCloudTrail_ReadOnlyAccess` | Grants `cloudtrail:LookupEvents` — fetches DynamoDB management event history |

---

## Usage

```bash
# Run from the Amazon DynamoDB directory with the project venv activated
python fetch_dynamodb_logs.py
```

Output files are written to `logs/` and `report/` with timestamps in their filenames. `generate_bom.py` can also be run standalone to reprocess all existing files in `logs/`.

---

## How It Works

`fetch_dynamodb_logs.py` executes the following pipeline on each run:

1. Verifies DynamoDB is reachable via `ListTables`
2. Pages through `cloudtrail:LookupEvents` for `dynamodb.amazonaws.com` across a 24-hour window
3. Normalises each event: converts `EventTime` to ISO-8601 and unpacks the embedded `CloudTrailEvent` JSON string
4. Enumerates **all current tables** via paginated `ListTables`
5. Merges CloudTrail-referenced tables with enumerated tables, deduplicating by table name
6. Calls `DescribeTable` + `DescribeContinuousBackups` for each table to capture full configuration
7. Appends a synthetic `DynamoDBResourceInventory` event per table so the BOM generator can include describe output without a separate read pass
8. Writes all events to `logs/dynamodb_logs_<timestamp>.json`
9. Invokes `generate_bom.py` to produce `report/bom_<timestamp>.json`

**BOM properties captured per table:**

| Property | Source |
| --- | --- |
| `aws:DynamoDBTableArn` | `DescribeTable` → `TableArn` |
| `aws:TableStatus` | `DescribeTable` → `TableStatus` (ACTIVE, CREATING, etc.) |
| `aws:BillingMode` | `DescribeTable` → `BillingModeSummary.BillingMode` (PAY_PER_REQUEST or PROVISIONED) |
| `aws:ItemCount` | `DescribeTable` → `ItemCount` |
| `aws:TableSizeMB` | `DescribeTable` → `TableSizeBytes` converted to MB |
| `aws:PartitionKey` | `DescribeTable` → `KeySchema[HASH].AttributeName` |
| `aws:SortKey` | `DescribeTable` → `KeySchema[RANGE].AttributeName` (if present) |
| `aws:GSICount` | Count of Global Secondary Indexes |
| `aws:LSICount` | Count of Local Secondary Indexes |
| `aws:StreamEnabled` | Whether DynamoDB Streams is enabled |
| `aws:StreamViewType` | NEW_IMAGE, OLD_IMAGE, NEW_AND_OLD_IMAGES, or KEYS_ONLY |
| `aws:EncryptionType` | AWS_OWNED_KMS, CUSTOMER_MANAGED_KMS |
| `aws:CreationDateTime` | Table creation timestamp |
| `aws:PITREnabled` | Whether Point-in-Time Recovery is enabled |
| `aws:PITREarliestRestore` | Earliest restorable point (if PITR enabled) |
| `aws:PITRLatestRestore` | Latest restorable point (if PITR enabled) |

**CloudTrail events captured (examples):**

| Event Name | What Changed |
| --- | --- |
| `CreateTable` | New table created |
| `DeleteTable` | Table deleted |
| `UpdateTable` | Table configuration changed (billing mode, GSI, stream, etc.) |
| `RestoreTableFromBackup` | Table restored from on-demand backup |
| `RestoreTableToPointInTime` | Table restored to a point in time |
| `CreateBackup` | On-demand backup created |
| `DeleteBackup` | On-demand backup deleted |
| `UpdateContinuousBackups` | Point-in-Time Recovery enabled or disabled |
| `UpdateTimeToLive` | TTL attribute configured or disabled |
| `TagResource` | Tags added to table |
| `UntagResource` | Tags removed from table |
| `CreateGlobalTable` | Table replicated to additional regions |
| `UpdateGlobalTable` | Global table replica configuration changed |

> **Note:** `PutItem`, `GetItem`, `DeleteItem`, and other data-plane events do not appear in CloudTrail event history by default. They require enabling DynamoDB data events on a paid CloudTrail trail.

---

## Troubleshooting

| Error | Cause | Fix |
| --- | --- | --- |
| `NoCredentialsError` | Credentials not set in `.env` | Add `AWS_DYNAMODB_ACCESS_KEY_ID` and `AWS_DYNAMODB_SECRET_ACCESS_KEY` to `MS_logs_collector/.env` |
| `AccessDeniedException: dynamodb:ListTables` | IAM user missing the managed policy | Attach `AmazonDynamoDBReadOnlyAccess` to the IAM user |
| `AccessDenied: cloudtrail:LookupEvents` | IAM user missing CloudTrail policy | Attach `AWSCloudTrail_ReadOnlyAccess` to the IAM user |
| 0 tables enumerated | No DynamoDB tables in the configured region | Check that `AWS_DEFAULT_REGION` matches where your tables are deployed |
| `services: []` in BOM | No table creation or modification events in the last 24 hours, but tables still enumerated | Tables appear via inventory enumeration — `services` is populated from both CloudTrail events and enumeration |