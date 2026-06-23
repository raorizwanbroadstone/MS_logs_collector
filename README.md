# Logs Collector

A multi-cloud log collection and asset inventory tool. Each module connects to a cloud service, fetches the last 24 hours of activity, describes discovered resources, and produces a **CycloneDX 1.6 Bill of Materials** report.

---

## Cloud Providers

| Provider | Modules | SDK |
| --- | --- | --- |
| [AWS](AWS/) | CloudTrail, IAM, Bedrock, DynamoDB, EC2, Lambda, S3, SageMaker | `boto3` |
| [Microsoft](Microsoft/) | Azure OpenAI, Azure Machine Learning, Azure Storage, Microsoft 365 | Azure SDK |

---

## Project Layout

```
logs_collector/
├── .env                  # All credentials (never committed)
├── requirements.txt      # Python dependencies
├── AWS/                  # AWS service collectors — see AWS/README.md
└── Microsoft/            # Azure & M365 collectors — see Microsoft/README.md
```

Each module follows the same internal layout:

```
<Service>/
├── fetch_<service>_logs.py   # Queries the service API; enriches resources via Describe calls
├── generate_bom.py           # Streams logs/, deduplicates, writes a CycloneDX 1.6 BOM
├── logs/                     # Raw timestamped JSON (one file per run)
└── report/                   # CycloneDX BOM reports (one file per run)
```

---

## Setup

```bash
# 1. Create and activate a virtual environment
python -m venv venv
venv\Scripts\activate        # Windows

# 2. Install dependencies
pip install -r requirements.txt

# 3. Populate .env with credentials
#    See each module's README.md for the exact keys required.
```

---

## Running a Collector

```bash
cd "AWS/Amazon S3"
python fetch_s3_logs.py
```

Output lands in `logs/` and `report/` inside the module directory, timestamped on every run. `generate_bom.py` can also be run standalone to reprocess existing files in `logs/`.

---

## Output Format

| File | Contents |
| --- | --- |
| `logs/<service>_logs_<timestamp>.json` | Raw API responses with enriched resource descriptors |
| `report/bom_<timestamp>.json` | CycloneDX 1.6 BOM — deduplicated components, IAM principal access map |

---

## Dependencies

| Package | Purpose |
| --- | --- |
| `boto3` | AWS service API calls |
| `azure-identity`, `azure-mgmt-*`, `azure-monitor-query` | Azure authentication and service APIs |
| `ijson` | Memory-efficient streaming of large log files |
| `mmh3`, `bitarray` | Bloom filter for fast cross-event deduplication |
