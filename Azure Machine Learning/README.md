# Azure Machine Learning Logs Collector

This module collects operational and inventory data from Azure Machine Learning environments. It authenticates to Azure using a Service Principal, discovers Azure ML workspaces across accessible subscriptions, retrieves activity logs and diagnostic settings, queries Log Analytics for Azure ML events, and inventories AI-related assets such as models, jobs, endpoints, compute resources, and data assets.

---

## Folder Structure

```text
Azure Machine Learning/
├── fetch_aml_logs.py              # Main script — collects logs and triggers BOM generation
├── generate_bom.py                # BOM generator — runs automatically after fetch; outputs CycloneDX 1.6 JSON
├── .env                           # Azure credentials and configuration
├── logs/                          # Collector output (created automatically on first run)
│   └── azure_ml_YYYY-MM-DD_HH-MM-SS.json
└── report/                        # BOM output (created automatically on first run)
    └── bom_YYYYMMDD_HHMMSS.json
```

---

## Script: `fetch_aml_logs.py`

### Purpose

The script performs the following tasks:

* Discovers Azure Machine Learning workspaces
* Retrieves workspace Diagnostic Settings
* Collects Azure Activity Logs
* Queries Azure ML Log Analytics tables
* Inventories Azure ML assets:

  * Models
  * Jobs
  * Online Endpoints
  * Compute Resources
  * Data Assets
* Exports all collected information to a timestamped JSON file

---

## Configuration

The script reads Azure credentials from a `.env` file:

| Variable              | Description                                  |
| --------------------- | -------------------------------------------- |
| `AZURE_TENANT_ID`     | Azure Active Directory Tenant ID             |
| `AZURE_CLIENT_ID`     | Service Principal Client ID                  |
| `AZURE_CLIENT_SECRET` | Service Principal Secret                     |
| `AZURE_WORKSPACE_ID`  | Optional fallback Log Analytics Workspace ID |

Runtime configuration:

```python
HOURS_BACK = 24
OUTPUT_DIR = Path(__file__).parent / "logs"
```

---

## Authentication

Authentication is performed using Azure Service Principal credentials via `ClientSecretCredential`:

```python
credential = ClientSecretCredential(
    tenant_id=AZURE_TENANT_ID,
    client_id=AZURE_CLIENT_ID,
    client_secret=AZURE_CLIENT_SECRET
)
```

The credential is used to access:

* Azure Resource Manager
* Azure Monitor
* Log Analytics
* Azure Machine Learning

---

## Azure ML Log Tables Queried

The collector attempts to retrieve events from the following AML diagnostic tables:

| Table                      | Description                              |
| -------------------------- | ---------------------------------------- |
| `AmlComputeJobEvents`      | Compute job activity                     |
| `AmlComputeClusterEvents`  | Cluster lifecycle events                 |
| `AmlComputeInstanceEvents` | Compute instance activity                |
| `AmlRunStatusChangedEvent` | Training and pipeline run status changes |
| `AmlDataSetEvent`          | Dataset operations                       |
| `AmlModelEvent`            | Model registration and updates           |
| `AmlDeploymentEvent`       | Endpoint deployment events               |
| `AmlInferencingEvent`      | Inference and scoring activity           |

---

## Output Format

Results are written to:

```text
logs/azure_ml_YYYY-MM-DD_HH-MM-SS.json
```

Example structure:

```json
{
  "collectionTime": "2026-06-17T10:00:00Z",
  "summary": {
    "subscriptions_processed": 1,
    "workspaces_processed": 1,
    "errors": 0
  },
  "workspaces": [
    {
      "workspace_name": "demo-workspace",
      "activity_logs": [],
      "diagnostic_settings": [],
      "aml_log_tables": {},
      "assets": {}
    }
  ]
}
```

---

## Script: `generate_bom.py`

Runs automatically after each fetch. Streams the freshly written log file, extracts BOM-relevant entities, deduplicates them with a Bloom filter backed by an exact set (zero duplicates guaranteed), and writes a CycloneDX 1.6 BOM JSON to `report/`.

Can also be run standalone:

```bash
cd "Azure Machine Learning"
python generate_bom.py
```

Extracted BOM entities:

| Entity | CycloneDX section | Type | Unique key |
|---|---|---|---|
| AML workspaces | `components` | application | Lowercase ARM `workspace_resource_id` |
| Registered ML models | `components` | library | `workspace_resource_id/models/name/version` |
| Compute clusters / instances | `components` | machine | `workspace_resource_id/compute/name` |
| Data assets | `components` | file | `workspace_resource_id/data/name/version` |
| Client applications (OAuth2) | `components` | application | `claims.appid` |
| Azure resource providers | `services` | — | Provider name lowercased |
| Online inference endpoints | `services` | — | `workspace_resource_id/endpoints/name` |
| Log Analytics workspaces | `services` | — | Workspace ID |

---

## Azure Resource Context

This collector targets Azure Machine Learning resources:

```text
/subscriptions/<subscription-id>
  /resourceGroups/<resource-group>
  /providers/Microsoft.MachineLearningServices
  /workspaces/<workspace-name>
```

The script can optionally query a connected Log Analytics Workspace where AML diagnostic logs are being ingested.

---

## Required Azure Permissions

The Service Principal should have access to:

* Reader
* Monitoring Reader
* Log Analytics Reader
* Azure Machine Learning Workspace Reader

These permissions allow the collector to discover resources and query monitoring data without modifying any Azure assets.

---

## Use Cases

* AI Bill of Materials (AIBOM) generation
* AI asset inventory
* Model governance
* Security assessments
* Compliance auditing
* Azure ML environment visibility

---

## Notes

The collector performs read-only operations and does not create, modify, or delete Azure resources. All collected data is exported as JSON for further analysis and reporting.
