# Azure Storage — Log Collector

This module authenticates with Azure using a dedicated Service Principal, enumerates all storage accounts across accessible subscriptions, retrieves Azure Monitor **Activity Logs** (management-plane events) and **Log Analytics diagnostic logs** (data-plane: blob read/write/delete, container ops, auth events), and writes results to a timestamped JSON file.

---

## Folder Structure

```
Azure Storage/
├── fetch_azure_storage_logs.py   # Main collector — queries activity logs + Log Analytics
├── generate_bom.py               # BOM generator — runs automatically after fetch; outputs CycloneDX 1.6 JSON
├── insert_data.py                # Test data generator — uploads, reads, and deletes a blob
├── README.md                     # This file
├── logs/                         # Collector output (created automatically on first run)
│   └── azure_storage_YYYY-MM-DD_HH-MM-SS.json
└── report/                       # BOM output (created automatically on first run)
    └── bom_YYYYMMDD_HHMMSS.json
```

---

## Environment Variables

Add these to the root `.env` file:

| Variable | Description |
|---|---|
| `AZURE_STORAGE_TENANT_ID` | Azure AD tenant ID |
| `AZURE_STORAGE_CLIENT_ID` | Service principal application (client) ID |
| `AZURE_STORAGE_CLIENT_SECRET` | Service principal client secret |
| `AZURE_WORKSPACE_ID` | Log Analytics Workspace ID (used as fallback if not discovered from diagnostic settings) |
| `AZURE_STORAGE_SUBSCRIPTION_ID` | Subscription ID fallback (used if the SP cannot list subscriptions automatically) |
| `AZURE_STORAGE_CONNECTION_STRING` | Storage account connection string (used only by `insert_data.py`) |

Example `.env` entries:

```env
AZURE_STORAGE_TENANT_ID=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
AZURE_STORAGE_CLIENT_ID=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
AZURE_STORAGE_CLIENT_SECRET=your-client-secret-here
AZURE_WORKSPACE_ID=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
AZURE_STORAGE_SUBSCRIPTION_ID=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
AZURE_STORAGE_CONNECTION_STRING=DefaultEndpointsProtocol=https;AccountName=...;AccountKey=...;EndpointSuffix=core.windows.net
```

---

## Full Setup — Step by Step

### Step 1 — Install dependencies

```bash
pip install -r requirements.txt
pip install azure-storage-blob   # required by insert_data.py
```

`requirements.txt` includes:
```
azure-identity>=1.15.0
azure-mgmt-monitor>=6.0.0
azure-mgmt-resource>=23.0.0
azure-mgmt-subscription>=3.0.0
azure-monitor-query>=1.2.0
```

---

### Step 2 — Create a Service Principal

1. [portal.azure.com](https://portal.azure.com) → **Azure Active Directory** → **App registrations** → **New registration**
2. Name it (e.g. `logs-collector-storage-sp`), leave everything else default → **Register**
3. On the app overview page, copy:
   - **Application (client) ID** → `AZURE_STORAGE_CLIENT_ID`
   - **Directory (tenant) ID** → `AZURE_STORAGE_TENANT_ID`
4. Go to **Certificates & secrets** → **New client secret** → copy the **Value** → `AZURE_STORAGE_CLIENT_SECRET`

---

### Step 3 — Create an Azure Storage Account

1. Portal → **Storage accounts** → **Create**
2. Fill in:
   - **Resource group**: create new (e.g. `logs-test-rg`)
   - **Storage account name**: globally unique (e.g. `azuredblogstest12345`)
   - **Region**: closest to you
   - **Performance**: Standard
   - **Redundancy**: LRS *(cheapest, free-tier compatible)*
3. **Review + Create** → **Create**
4. After creation → **Security + networking** → **Access keys** → copy **Connection string** → `AZURE_STORAGE_CONNECTION_STRING`

---

### Step 4 — Create a Log Analytics Workspace

1. Portal → search **Log Analytics workspaces** → **Create**
2. Fill in:
   - **Resource group**: `logs-test-rg`
   - **Name**: e.g. `logs-test-workspace`
   - **Region**: same as storage account
   - **Pricing tier**: Pay-as-you-go *(free tier: 500 MB/day)*
3. **Review + Create** → **Create**
4. After creation → **Overview** → copy **Workspace ID** → `AZURE_WORKSPACE_ID`

---

### Step 5 — Enable Diagnostic Settings on the Storage Account

This routes data-plane logs (blob read/write/delete) to Log Analytics.

1. Portal → your **Storage account** → **Monitoring** → **Diagnostic settings**
2. Click **blob** → **Add diagnostic setting**
3. Configure:
   - **Name**: e.g. `blob-to-la`
   - **Categories**: check `StorageRead`, `StorageWrite`, `StorageDelete`
   - **Destination**: check **Send to Log Analytics workspace** → select your workspace
4. **Save**

Repeat for **file**, **queue**, **table** sub-resources if you want those logs too.

---

### Step 6 — Grant IAM Roles to the Service Principal

The SP needs three role assignments:

| Scope | Role | Purpose |
|---|---|---|
| Subscription | **Reader** | List subscriptions and storage accounts |
| Storage account | **Reader** | Read diagnostic settings and activity logs |
| Log Analytics workspace | **Log Analytics Reader** | Query log tables |

To assign each:

1. Go to the resource → **Access control (IAM)** → **Add role assignment**
2. Select the role → **Next** → **Select members** → search your SP name → **Select** → **Review + assign**

> IAM changes take 1–5 minutes to propagate.

---

### Step 7 — Create the Blob Container and Generate Test Data

The `insert_data.py` script needs a container called `rag-documents`. It creates it automatically if missing.

Run it 2–3 times to generate enough log events:

```bash
cd "Azure Storage"
python insert_data.py
```

Each run performs: **upload** → **read** → **delete** — generating `PutBlob`, `GetBlob`, and `DeleteBlob` events in Log Analytics.

---

### Step 8 — Wait for Logs to Arrive in Log Analytics

Azure diagnostic logs take **5–15 minutes** to appear after the operations happen.

Verify in the Portal → your **Log Analytics workspace** → **Logs** → run:

```kql
StorageBlobLogs
| order by TimeGenerated desc
| take 20
```

Once rows appear, the collector will be able to retrieve them.

---

### Step 9 — Run the Collector

```bash
cd "Azure Storage"
python fetch_azure_storage_logs.py
```

Expected output:

```
🚀 Azure Storage Logs Collector
==================================================
  🔍 Listing subscriptions...
  ✅ Found 1 subscription(s)

📋 Subscription: My Subscription (xxxxxxxx-...)
  ✅ Found 1 storage account(s)
    📦 azuredblogstest12345 (logs-test-rg)
      🔍 Checking diagnostic settings...
      ✅ 1 diagnostic setting(s) found
      🔍 Fetching activity logs...
      ✅ 3 activity log event(s)
      🔍 Querying 1 Log Analytics workspace(s)...
      ✅ 6 storage diagnostic log event(s)

🎉 Done!
  📊 Subscriptions processed: 1
  📦 Storage accounts processed: 1
  📄 Total log events collected: 9
  💾 Output saved to: ...\Azure Storage\logs\azure_storage_2026-06-17_12-06-29.json

Generating BOM report...
Processing azure_storage_2026-06-17_12-06-29.json ...
  3 new components, 2 new services

Report : ...\Azure Storage\report\bom_20260617_120629.json
Total  : 3 components, 2 services
```

---

## Script Reference

### `fetch_azure_storage_logs.py`

#### Authentication

```python
credential = ClientSecretCredential(
    tenant_id=AZURE_STORAGE_TENANT_ID,
    client_id=AZURE_STORAGE_CLIENT_ID,
    client_secret=AZURE_STORAGE_CLIENT_SECRET,
)
```

#### Key Functions

| Function | Description |
|---|---|
| `get_credential()` | Returns a `ClientSecretCredential` from env vars |
| `get_subscriptions(credential)` | Lists all subscriptions the SP can access; falls back to `AZURE_STORAGE_SUBSCRIPTION_ID` if none found |
| `get_storage_accounts(credential, sub_id)` | Lists all `Microsoft.Storage/storageAccounts` resources in a subscription via `ResourceManagementClient` |
| `get_diagnostic_settings(monitor_client, uri)` | Returns diagnostic settings for a resource URI; handles 401/403 gracefully |
| `collect_all_diagnostic_settings(monitor_client, account_id)` | Collects settings from the account itself and all four sub-resources (blob, file, queue, table) |
| `extract_workspace_ids(diag_settings)` | Pulls unique Log Analytics workspace IDs from the diagnostic settings |
| `get_activity_logs(monitor_client, account_id)` | Fetches management-plane Activity Logs for the last 24 hours |
| `query_log_analytics(logs_client, workspace_id, account_name)` | Queries all four storage tables in Log Analytics for the last 24 hours |
| `process_storage_account(...)` | Orchestrates all collection steps for a single storage account; continues on error |
| `main()` | Top-level entry point |

#### Collected Log Sources

| Source | API | Log Types |
|---|---|---|
| Azure Activity Logs | `MonitorManagementClient.activity_logs` | Create/delete/update storage account, key rotation, access policy changes |
| `StorageBlobLogs` | Log Analytics | `PutBlob`, `GetBlob`, `DeleteBlob`, `CopyBlob`, `ListBlobs`, SAS auth events |
| `StorageFileLogs` | Log Analytics | File share read/write/delete operations |
| `StorageQueueLogs` | Log Analytics | Queue message enqueue/dequeue/delete |
| `StorageTableLogs` | Log Analytics | Table entity insert/query/delete |

#### Runtime Constants

```python
HOURS_BACK = 24        # Look-back window in hours
OUTPUT_DIR = Path(__file__).parent / "logs"   # Azure Storage/logs/
```

---

### `generate_bom.py`

Streams the log file produced by `fetch_azure_storage_logs.py`, deduplicates extracted entities with a Bloom filter backed by an exact set (zero duplicate guarantee), and writes a CycloneDX 1.6 BOM JSON report to `report/`.

Extracted entities:

| Entity | CycloneDX section | Unique key |
|---|---|---|
| Storage accounts | `components` (type: application) | Full ARM resource ID |
| Client applications | `components` (type: application) | `claims.appid` from activity log JWT |
| Resource providers | `services` | Provider name (e.g. `Microsoft.Storage`) |
| Log Analytics workspaces | `services` | Workspace ID |

Called automatically at the end of `fetch_azure_storage_logs.py`. Can also be run standalone to reprocess existing logs:

```bash
cd "Azure Storage"
python generate_bom.py
```

---

### `insert_data.py`

Generates test blob operations to verify the collector end-to-end. On first run it creates the container; subsequent runs skip that step.

```python
container.upload_blob("test.txt", b"hello", overwrite=True)   # → PutBlob
blob.download_blob().readall()                                  # → GetBlob
blob.delete_blob()                                              # → DeleteBlob
```

Requires `AZURE_STORAGE_CONNECTION_STRING` in `.env`.

---

## Output Format

Each run writes a new file to `Azure Storage/logs/azure_storage_YYYY-MM-DD_HH-MM-SS.json`.

```json
{
  "collectionTime": "2026-06-17T12:06:29.123456+00:00",
  "summary": {
    "subscriptions_processed": 1,
    "storage_accounts_processed": 1,
    "total_log_events_collected": 9,
    "errors": 0
  },
  "storage_accounts": [
    {
      "subscription_id": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
      "subscription_name": "My Subscription",
      "storage_account_id": "/subscriptions/.../storageAccounts/azuredblogstest12345",
      "storage_account_name": "azuredblogstest12345",
      "resource_group": "logs-test-rg",
      "location": "eastus",
      "diagnostic_logging_enabled": true,
      "diagnostic_settings": {
        "account": [],
        "blobServices/default": [
          {
            "id": "/subscriptions/.../blobServices/default/providers/microsoft.insights/diagnosticSettings/blob-to-la",
            "name": "blob-to-la",
            "workspace_id": "/subscriptions/.../workspaces/logs-test-workspace",
            "logs": [
              { "category": "StorageRead",   "enabled": true },
              { "category": "StorageWrite",  "enabled": true },
              { "category": "StorageDelete", "enabled": true }
            ]
          }
        ],
        "fileServices/default": [],
        "queueServices/default": [],
        "tableServices/default": []
      },
      "activity_logs": [
        {
          "event_timestamp": "2026-06-17T11:55:00+00:00",
          "operation_name": { "value": "Microsoft.Storage/storageAccounts/write" },
          "status": { "value": "Succeeded" }
        }
      ],
      "storage_diagnostic_logs": {
        "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx": {
          "StorageBlobLogs": [
            {
              "TimeGenerated": "2026-06-17T11:51:30Z",
              "AccountName": "azuredblogstest12345",
              "OperationName": "PutBlob",
              "StatusCode": 201,
              "DurationMs": 42,
              "CallerIpAddress": "203.x.x.x",
              "Uri": "https://azuredblogstest12345.blob.core.windows.net/rag-documents/test.txt"
            }
          ],
          "StorageFileLogs":  [],
          "StorageQueueLogs": [],
          "StorageTableLogs": []
        }
      },
      "error": null
    }
  ]
}
```

If diagnostic logging is not enabled on a storage account, the output records:

```json
{
  "diagnostic_logging_enabled": false,
  "storage_diagnostic_logs": {
    "status": "no_log_analytics_workspace_configured"
  }
}
```

---

## Required Azure Permissions Summary

| Resource | Role | Required For |
|---|---|---|
| Subscription | Reader | Listing subscriptions and storage accounts |
| Storage account | Reader | Reading diagnostic settings and activity logs |
| Log Analytics workspace | Log Analytics Reader | Querying `StorageBlobLogs` and other storage tables |

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `Found 0 subscription(s)` | SP has no role at subscription level | Assign **Reader** on the subscription in IAM; or set `AZURE_STORAGE_SUBSCRIPTION_ID` in `.env` as immediate fallback |
| `Cannot list storage accounts` | SP lacks Reader on subscription/resource group | Assign **Reader** at subscription or resource group scope |
| `storage_diagnostic_logs` is `no_log_analytics_workspace_configured` | No diagnostic settings pointing to Log Analytics | Complete Step 5; or set `AZURE_WORKSPACE_ID` in `.env` |
| `StorageBlobLogs` returns `[]` | Logs haven't arrived yet | Wait 5–15 min after running `insert_data.py`, then re-run the collector |
| `insufficient_permissions` on diagnostic settings | SP can't read diagnostic config | Assign **Reader** on the storage account in IAM |
| `ValueError: Connection string missing` | `AZURE_STORAGE_CONNECTION_STRING` not set in `.env` | Copy the connection string from Portal → Storage account → Access keys |
| `ContainerNotFound` | Container `rag-documents` doesn't exist | The updated `insert_data.py` creates it automatically on first run |
| `AuthenticationError` | Wrong client ID, secret, or tenant | Verify the three `AZURE_STORAGE_*` values in `.env` match the app registration |
