# AWS — Log Collectors

Eight collectors, one per AWS service. Each uses CloudTrail's `LookupEvents` to find resource change events over the last 24 hours, then calls the service's own Describe APIs to enrich every discovered resource before writing a CycloneDX 1.6 BOM.

---

## Modules

| Module | What It Collects |
| --- | --- |
| [AWS CloudTrail](AWS%20CloudTrail/) | Trail and Event Data Store configuration changes; describes each trail (S3 delivery, KMS key, logging status, tamper detection) and event data store |
| [AWS IAM](AWS%20IAM/) | User, group, role, and policy change events; describes each entity with attached policies and last-used metadata |
| [Amazon Bedrock](Amazon%20Bedrock/) | Model invocation and guardrail events; describes foundation model access and custom model configurations |
| [Amazon DynamoDB](Amazon%20DynamoDB/) | Table create/update/delete events; describes tables (key schema, billing mode, GSIs, streams, encryption, TTL) |
| [Amazon EC2](Amazon%20EC2/) | Instance launch, stop, and termination events; describes instances (type, AMI, VPC, security groups, tags) |
| [Amazon Lambda](Amazon%20Lambda/) | Function create/update/delete events; describes functions (runtime, memory, timeout, layers, environment config) |
| [Amazon S3](Amazon%20S3/) | Bucket create/delete and policy change events; describes buckets (versioning, encryption, ACL, replication, access logging) |
| [Amazon SageMaker](Amazon%20SageMaker/) | Training job, endpoint, and model registry events; describes each resource (instance type, model data, container image) |

---

## Credentials

Each module uses its own dedicated IAM user with the minimum read-only policy required. Add the module-specific keys to the root `.env` file — see each module's `README.md` for the exact variable names and the IAM policy to attach.

All modules share one region setting:

```env
AWS_DEFAULT_REGION=us-east-1
```

---

## Running a Collector

```bash
cd "AWS/<Service Name>"
python fetch_<service>_logs.py
```

Outputs are written to `logs/` and `report/` with timestamps. `generate_bom.py` can be run standalone to reprocess any existing `logs/` files.
