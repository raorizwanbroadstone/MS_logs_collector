# Amazon RDS — Log Collector

Connects to **Amazon RDS** via boto3, fetches the last 24 hours of RDS management events from CloudTrail, enumerates all current DB instances and Aurora clusters, describes each with its full configuration, and generates a **CycloneDX 1.6 Bill of Materials** report.

---

## Structure

```
Amazon RDS/
├── fetch_rds_logs.py    # Queries CloudTrail for RDS events and enumerates all instances and clusters
├── generate_bom.py      # Streams logs, deduplicates entities, produces CycloneDX BOM
├── logs/                # Output: timestamped raw CloudTrail JSON
└── report/              # Output: timestamped CycloneDX BOM reports
```

---

## Setup

Add the following to the root `.env` file:

```env
AWS_DEFAULT_REGION=us-east-1
AWS_RDS_ACCESS_KEY_ID=<your-key-id>
AWS_RDS_SECRET_ACCESS_KEY=<your-secret>
```

### How to create the IAM user and generate credentials

**Step 1 — Create the IAM user**

1. Open the [AWS IAM Console](https://console.aws.amazon.com/iam/)
2. Go to **Users** → **Create user**
3. Enter a username (e.g. `rds-bom-collector`) → **Next**
4. Select **Attach policies directly**
5. Search for and attach these two managed policies:
   - `AmazonRDSReadOnlyAccess`
   - `AWSCloudTrail_ReadOnlyAccess`
6. **Next** → **Create user**

**Step 2 — Generate access keys**

1. In IAM, open the user you just created
2. Go to **Security credentials** → **Create access key**
3. Select **Application running outside AWS** → **Next** → **Create access key**
4. Copy the **Access key ID** → set as `AWS_RDS_ACCESS_KEY_ID`
5. Copy the **Secret access key** → set as `AWS_RDS_SECRET_ACCESS_KEY`

> The secret key is shown only once. Store it immediately.

**Step 3 — Set your region**

Your region code is shown in the top-right of the AWS Console (e.g. `us-east-1`, `eu-west-1`). Set it as `AWS_DEFAULT_REGION`. RDS is a regional service — only instances in the configured region are enumerated.

---

## Required IAM Permissions

| Managed Policy | Why Needed |
| --- | --- |
| `AmazonRDSReadOnlyAccess` | Grants `rds:DescribeDBInstances`, `rds:DescribeDBClusters` — covers full instance and cluster enumeration and describe |
| `AWSCloudTrail_ReadOnlyAccess` | Grants `cloudtrail:LookupEvents` — fetches RDS management event history |

---

## Usage

```bash
# Run from the Amazon RDS directory with the project venv activated
python fetch_rds_logs.py
```

Output files are written to `logs/` and `report/` with timestamps in their filenames. `generate_bom.py` can also be run standalone to reprocess all existing files in `logs/`.

---

## How It Works

`fetch_rds_logs.py` executes the following pipeline on each run:

1. Verifies RDS access via `DescribeDBInstances`
2. Pages through `cloudtrail:LookupEvents` for `rds.amazonaws.com` across a 24-hour window
3. Normalises each event: converts `EventTime` to ISO-8601 and unpacks the embedded `CloudTrailEvent` JSON string
4. Enumerates **all current DB instances** via paginated `DescribeDBInstances`
5. Enumerates **all current Aurora clusters** via paginated `DescribeDBClusters`
6. Merges CloudTrail-referenced resources with enumerated resources, deduplicating by type and identifier
7. Calls `DescribeDBInstances` or `DescribeDBClusters` per resource to capture its full current configuration
8. Appends a synthetic `RDSResourceInventory` event per resource so the BOM generator can include describe output without a separate read pass
9. Writes all events to `logs/rds_logs_<timestamp>.json`
10. Invokes `generate_bom.py` to produce `report/bom_<timestamp>.json`

**Resource types collected:**

| Resource Type | Enumerate API | BOM Properties |
| --- | --- | --- |
| DBInstance | `DescribeDBInstances` (paginated) | Engine, version, instance class, status, Multi-AZ, storage encryption, KMS key, storage type, allocated storage, deletion protection, publicly accessible, IAM DB auth, endpoint, AZ, subnet group, VPC, DB name, CA certificate, backup retention, create time |
| DBCluster | `DescribeDBClusters` (paginated) | Engine, version, engine mode (provisioned/serverless), status, Multi-AZ, storage encryption, KMS key, deletion protection, writer endpoint, reader endpoint, cluster member count, AZs, subnet group, backup retention, create time |

**CloudTrail events captured (examples):**

| Event Name | What Changed |
| --- | --- |
| `CreateDBInstance` | New DB instance created |
| `DeleteDBInstance` | DB instance deleted |
| `ModifyDBInstance` | Instance configuration changed (class, storage, etc.) |
| `RebootDBInstance` | Instance rebooted |
| `StartDBInstance` | Stopped instance started |
| `StopDBInstance` | Instance stopped |
| `CreateDBCluster` | New Aurora cluster created |
| `DeleteDBCluster` | Aurora cluster deleted |
| `ModifyDBCluster` | Cluster configuration changed |
| `FailoverDBCluster` | Manual or automatic failover triggered |
| `CreateDBSnapshot` | Manual snapshot created |
| `DeleteDBSnapshot` | Snapshot deleted |
| `RestoreDBInstanceFromDBSnapshot` | Instance restored from snapshot |
| `RestoreDBClusterFromSnapshot` | Aurora cluster restored from snapshot |
| `CreateDBInstanceReadReplica` | Read replica created |
| `ModifyDBParameterGroup` | Parameter group modified |
| `CreateDBSubnetGroup` | Subnet group created |

> **Note:** `ModifyDBInstance` events that change `MasterUserPassword` are a high-value security signal — any password rotation should be expected and documented.

---

## Troubleshooting

| Error | Cause | Fix |
| --- | --- | --- |
| `NoCredentialsError` | Credentials not set in `.env` | Add `AWS_RDS_ACCESS_KEY_ID` and `AWS_RDS_SECRET_ACCESS_KEY` to `MS_logs_collector/.env` |
| `AccessDenied: rds:DescribeDBInstances` | IAM user missing the managed policy | Attach `AmazonRDSReadOnlyAccess` to the IAM user |
| `AccessDenied: cloudtrail:LookupEvents` | IAM user missing CloudTrail policy | Attach `AWSCloudTrail_ReadOnlyAccess` to the IAM user |
| 0 instances enumerated | No RDS instances in the configured region | Check that `AWS_DEFAULT_REGION` matches where your databases are deployed |
| Clusters listed as 0 | No Aurora clusters (standard RDS only) | Expected — standard RDS instances appear under DBInstance, not DBCluster |
| Resource listed as `NotFound` | Instance deleted between the CloudTrail event and the describe call | Expected — recorded in BOM with `InventoryStatus: NotFound` |
