# Azure OpenAI — Diagnostic Log Collector

Authenticates with Azure using a Service Principal, connects to a Log Analytics Workspace, and runs a suite of KQL queries to retrieve diagnostic logs from Azure Cognitive Services and Azure OpenAI resources. A CycloneDX 1.6 Bill of Materials report is generated automatically on each run.

---

## Structure

```
Azure OpenAI/
├── fetch_azure_diagnostic_logs.py
├── generate_bom.py
├── README.md
├── logs/
│   └── workspace_test_YYYY-MM-DD_HH-MM-SS.json
└── report/
    └── bom_YYYYMMDD_HHMMSS.json
```

---

## Configuration

Add the following to the root `.env` file:

```env
AZURE_TENANT_ID=<tenant-id>
AZURE_CLIENT_ID=<client-id>
AZURE_CLIENT_SECRET=<client-secret>
AZURE_WORKSPACE_ID=<log-analytics-workspace-id>
```

| Variable | Description |
| --- | --- |
| `AZURE_TENANT_ID` | Azure Active Directory tenant ID |
| `AZURE_CLIENT_ID` | Service principal application (client) ID |
| `AZURE_CLIENT_SECRET` | Service principal client secret |
| `AZURE_WORKSPACE_ID` | Log Analytics Workspace resource ID |

---

## Usage

```bash
# Run from the Azure OpenAI directory with the project venv activated
python fetch_azure_diagnostic_logs.py
```

Output files are written to `logs/` and `report/` with timestamps in their filenames. `generate_bom.py` can also be run standalone to reprocess all existing files in `logs/`.

---

## How It Works

`fetch_azure_diagnostic_logs.py` runs the following pipeline:

1. Authenticates using `ClientSecretCredential` and creates a `LogsQueryClient`
2. Executes all four KQL queries against the workspace for the last 24 hours
3. Writes results to `logs/workspace_test_<timestamp>.json`
4. Invokes `generate_bom.py` to produce `report/bom_<timestamp>.json`

**KQL queries executed:**

| Query | Purpose |
| --- | --- |
| `Connection_Test` | Confirms workspace connectivity |
| `Workspace_Time` | Returns current server time from the workspace |
| `Table_Counts` | Lists all tables and their record counts |
| `AzureDiagnostics_Sample` | Returns up to 10 sample diagnostic records |

---

## Required Permission

The service principal must have the **Log Analytics Reader** role on the Log Analytics Workspace.

```bash
az role assignment create \
  --assignee <client-id> \
  --role "Log Analytics Reader" \
  --scope /subscriptions/<sub-id>/resourceGroups/<rg>/providers/Microsoft.OperationalInsights/workspaces/<workspace-name>
```

---

## Troubleshooting

| Issue | Cause | Fix |
| --- | --- | --- |
| `AuthenticationError` | Incorrect service principal credentials | Verify the three `AZURE_*` values in `.env` match the app registration |
| Empty `AzureDiagnostics_Sample` | No logs ingested yet | Enable diagnostic settings on the target Cognitive Services resource |
| `WorkspaceNotFound` | Incorrect `AZURE_WORKSPACE_ID` | Confirm the workspace ID in Portal → Log Analytics Workspace → Overview |
| `Table_Counts` is empty | Workspace accessible but no data ingested | Enable diagnostic settings and wait for logs to arrive |
