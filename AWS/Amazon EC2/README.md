# Amazon EC2 — Log Collector

Connects to **Amazon EC2** via boto3, fetches the last 24 hours of EC2 management events from CloudTrail, enumerates all current instances, security groups, key pairs, and EBS volumes, describes each resource in full, and generates a **CycloneDX 1.6 Bill of Materials** report.

---

## Structure

```
Amazon EC2/
├── fetch_ec2_logs.py    # Queries CloudTrail for EC2 events and 
├── generate_bom.py      # Streams logs, deduplicates entities, 
├── logs/                # Output: timestamped raw CloudTrail JSON
└── report/              # Output: timestamped CycloneDX BOM reports
```

---

## Setup

Add the following to the root `.env` file:

```env
AWS_DEFAULT_REGION=us-west-2
AWS_EC2_ACCESS_KEY_ID=<your-key-id>
AWS_EC2_SECRET_ACCESS_KEY=<your-secret>
```

### How to create the IAM user and generate credentials

**Step 1 — Create the IAM user**

1. Open the [AWS IAM Console](https://console.aws.amazon.com/iam/)
2. Go to **Users** → **Create user**
3. Enter a username (e.g. `ec2-bom-collector`) → **Next**
4. Select **Attach policies directly**
5. Search for and attach these two managed policies:
   - `AmazonEC2ReadOnlyAccess`
   - `AWSCloudTrail_ReadOnlyAccess`
6. **Next** → **Create user**

**Step 2 — Generate access keys**

1. In IAM, open the user you just created
2. Go to **Security credentials** → **Create access key**
3. Select **Application running outside AWS** → **Next** → **Create access key**
4. Copy the **Access key ID** → set as `AWS_EC2_ACCESS_KEY_ID`
5. Copy the **Secret access key** → set as `AWS_EC2_SECRET_ACCESS_KEY`

> The secret key is shown only once. Store it immediately.

**Step 3 — Set your region**

Your region code is shown in the top-right of the AWS Console (e.g. `us-west-2`, `eu-north-1`). Set it as `AWS_DEFAULT_REGION`.

---

## Required IAM permissions

| Managed Policy | Why Needed |
| --- | --- |
| `AmazonEC2ReadOnlyAccess` | Grants `ec2:DescribeInstances`, `ec2:DescribeSecurityGroups`, `ec2:DescribeKeyPairs`, `ec2:DescribeVolumes` — covers full resource enumeration and describe |
| `AWSCloudTrail_ReadOnlyAccess` | Grants `cloudtrail:LookupEvents` — fetches EC2 management event history |

---

## Usage

```bash
# Run from the Amazon EC2 directory with the project venv activated
python fetch_ec2_logs.py
```

Output files are written to `logs/` and `report/` with timestamps in their filenames. `generate_bom.py` can also be run standalone to reprocess all existing files in `logs/`.

---

## How It Works

`fetch_ec2_logs.py` executes the following pipeline on each run:

1. Verifies EC2 is reachable via `DescribeInstances`
2. Pages through `cloudtrail:LookupEvents` for `ec2.amazonaws.com` across a 24-hour window
3. Normalises each event: converts `EventTime` to ISO-8601 and unpacks the embedded `CloudTrailEvent` JSON string
4. Enumerates **all current resources** via paginated Describe calls (independent of CloudTrail event history)
5. Merges CloudTrail-referenced resources with enumerated resources, deduplicating by type and ID
6. Calls the appropriate Describe API for each resource to capture its current configuration
7. Appends a synthetic `EC2ResourceInventory` event per resource so the BOM generator can include describe output without a separate read pass
8. Writes all events to `logs/ec2_logs_<timestamp>.json`
9. Invokes `generate_bom.py` to produce `report/bom_<timestamp>.json`

**Resource types collected:**

| Resource Type | Enumerate API | Describe API | BOM Properties |
| --- | --- | --- | --- |
| Instance | `DescribeInstances` (paginated) | `DescribeInstances` | Instance type, state, AMI, key name, VPC, subnet, private/public IP, platform, architecture, monitoring, launch time, IAM instance profile, attached security groups, attached volumes |
| SecurityGroup | `DescribeSecurityGroups` (paginated) | `DescribeSecurityGroups` | Group name, description, VPC, ingress rule count, egress rule count |
| KeyPair | `DescribeKeyPairs` | `DescribeKeyPairs` | Key pair ID, key type (RSA/ED25519), creation time |
| Volume | `DescribeVolumes` (paginated) | `DescribeVolumes` | Volume type, size (GB), state, availability zone, encryption, IOPS, throughput, creation time, attached instances |

**CloudTrail events captured (examples):**

| Event Name | What Changed |
| --- | --- |
| `RunInstances` | New instance launched |
| `TerminateInstances` | Instance terminated |
| `StartInstances` | Instance started |
| `StopInstances` | Instance stopped |
| `CreateSecurityGroup` | New security group created |
| `AuthorizeSecurityGroupIngress` | Inbound rule added |
| `RevokeSecurityGroupIngress` | Inbound rule removed |
| `DeleteSecurityGroup` | Security group deleted |
| `CreateKeyPair` | New key pair created |
| `DeleteKeyPair` | Key pair deleted |
| `CreateVolume` | New EBS volume created |
| `DeleteVolume` | EBS volume deleted |
| `AttachVolume` | Volume attached to instance |
| `DetachVolume` | Volume detached from instance |
| `CreateSnapshot` | EBS snapshot created |
| `ModifyInstanceAttribute` | Instance attribute modified |

---

## Troubleshooting

| Error | Cause | Fix |
| --- | --- | --- |
| `NoCredentialsError` | Credentials not set in `.env` | Add `AWS_EC2_ACCESS_KEY_ID` and `AWS_EC2_SECRET_ACCESS_KEY` to `MS_logs_collector/.env` |
| `UnauthorizedOperation: DescribeInstances` | IAM user missing the managed policy | Attach `AmazonEC2ReadOnlyAccess` to the IAM user |
| `AccessDenied: cloudtrail:LookupEvents` | IAM user missing CloudTrail policy | Attach `AWSCloudTrail_ReadOnlyAccess` to the IAM user |
| 0 instances enumerated | No instances running in the configured region | Check that `AWS_DEFAULT_REGION` matches where your instances are deployed |
| Resource listed as `NotFound` | Resource deleted between the CloudTrail event and the describe call | Expected — recorded in BOM with `InventoryStatus: NotFound` |