# Azure Machine Learning — Log Collector

Authenticates with Azure using a Service Principal, discovers Azure Machine Learning workspaces across all accessible subscriptions, and collects diagnostic settings, activity logs, AML Log Analytics table events, and a full asset inventory (models, jobs, endpoints, compute, data assets) via the `azure-ai-ml` SDK. A CycloneDX 1.6 Bill of Materials report is generated automatically on each run.

---

## Structure

```
Azure Machine Learning/
├── fetch_aml_logs.py
├── generate_bom.py
├── README.md
├── logs/
│   └── azure_ml_YYYY-MM-DD_HH-MM-SS.json
└── report/
    └── bom_YYYYMMDD_HHMMSS.json
```

---

## Configuration

Add the following to the root `.env` file:

```env
AZURE_AML_TENANT_ID=<tenant-id>
AZURE_AML_CLIENT_ID=<client-id>
AZURE_AML_CLIENT_SECRET=<client-secret>
```

| Variable | Description |
| --- | --- |
| `AZURE_AML_TENANT_ID` | Azure Active Directory tenant ID |
| `AZURE_AML_CLIENT_ID` | Service principal application (client) ID |
| `AZURE_AML_CLIENT_SECRET` | Service principal client secret |

---

## Usage

```bash
# Run from the Azure Machine Learning directory with the project venv activated
python fetch_aml_logs.py
```

Output files are written to `logs/` and `report/` with timestamps in their filenames. `generate_bom.py` can also be run standalone to reprocess all existing files in `logs/`.

---

## How It Works

`fetch_aml_logs.py` runs the following pipeline per subscription:

1. Lists all AML workspaces via `ResourceManagementClient`
2. Reads diagnostic settings and activity logs via `MonitorManagementClient`
3. Queries all AML Log Analytics tables in each discovered workspace
4. Inventories assets (models, jobs, endpoints, compute, data assets) via `MLClient`
5. Writes results to `logs/azure_ml_<timestamp>.json`
6. Invokes `generate_bom.py` to produce `report/bom_<timestamp>.json`

**AML Log Analytics tables queried:**

| Table | Events captured |
| --- | --- |
| `AmlComputeJobEvents` | Compute job activity |
| `AmlComputeClusterEvents` | Cluster lifecycle events |
| `AmlComputeInstanceEvents` | Compute instance activity |
| `AmlRunStatusChangedEvent` | Training and pipeline run status changes |
| `AmlDataSetEvent` | Dataset operations |
| `AmlModelEvent` | Model registration and updates |
| `AmlDeploymentEvent` | Endpoint deployment events |
| `AmlInferencingEvent` | Inference and scoring activity |

**Asset inventory** (requires `azure-ai-ml`):

| Asset | Description |
| --- | --- |
| Models | Registered ML models with version and type |
| Jobs | Training and pipeline job history |
| Online endpoints | Deployed inference endpoints with scoring URIs |
| Compute | Clusters and instances with provisioning state |
| Data assets | Versioned datasets registered in the workspace |

---

## Required Permissions

| Resource | Role |
| --- | --- |
| Subscription | Reader |
| AML workspace | Reader |
| Log Analytics workspace | Log Analytics Reader |
| AML workspace (assets) | AzureML Data Scientist or Reader |

---

## Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| `Found 0 subscription(s)` | SP has no role at subscription level | Assign Reader on the subscription in IAM |
| `Cannot list AML workspaces` | SP lacks Reader on the subscription | Assign Reader at subscription or resource group scope |
| AML log tables all `table_not_found` | Diagnostic settings not configured | Enable diagnostic settings on the AML workspace and point them to a Log Analytics workspace |
| `assets` section empty or missing | `azure-ai-ml` not installed | Run `pip install azure-ai-ml` |
| `AuthenticationError` | Incorrect credentials in `.env` | Verify the three `AZURE_AML_*` values match the app registration |
| `no_log_analytics_workspace_configured` | No diagnostic settings point to Log Analytics | Add diagnostic settings on the AML workspace in Azure Portal |
