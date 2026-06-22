# Azure Storage — Log Collector

Authenticates with Azure using a Service Principal, enumerates all storage accounts across accessible subscriptions, and collects two log sources per account: **Activity Logs** (management-plane events) and **Log Analytics diagnostic logs** (data-plane operations — blob reads, writes, deletes, and auth events). A CycloneDX 1.6 Bill of Materials report is generated automatically on each run.

---

## Structure

```
Azure Storage/
├── fetch_azure_storage_logs.py
├── generate_bom.py
├── README.md
├── logs/
│   └── azure_storage_YYYY-MM-DD_HH-MM-SS.json
└── report/
    └── bom_YYYYMMDD_HHMMSS.json
```

---

## Setup

### Step 1 — Install dependencies

```bash
pip install -r requirements.txt
```

### Step 2 — Create a Service Principal

1. Azure Portal → **Azure Active Directory** → **App registrations** → **New registration**
2. Name it (e.g. `logs-collector-storage-sp`), leave defaults → **Register**
3. Copy **Application (client) ID** → `AZURE_STORAGE_CLIENT_ID`
4. Copy **Directory (tenant) ID** → `AZURE_STORAGE_TENANT_ID`
5. Go to **Certificates & secrets** → **New client secret** → copy the value → `AZURE_STORAGE_CLIENT_SECRET`

### Step 3 — Create a Storage Account

1. Portal → **Storage accounts** → **Create**
2. Select or create a resource group, give the account a globally unique name, choose a region
3. Performance: Standard, Redundancy: LRS
4. **Review + Create** → **Create**

### Step 4 — Create a Log Analytics Workspace

1. Portal → **Log Analytics workspaces** → **Create**
2. Use the same resource group and region as the storage account
3. **Review + Create** → **Create**

### Step 5 — Enable Diagnostic Settings on the Storage Account

1. Portal → your **Storage account** → **Monitoring** → **Diagnostic settings**
2. Click **blob** → **Add diagnostic setting**
3. Check `StorageRead`, `StorageWrite`, `StorageDelete`; destination: **Log Analytics workspace** → select your workspace
4. **Save**

Repeat for **file**, **queue**, and **table** sub-resources as needed.

### Step 6 — Assign IAM Roles to the Service Principal

| Scope | Role | Purpose |
| --- | --- | --- |
| Subscription | Reader | List subscriptions and storage accounts |
| Storage account | Reader | Read diagnostic settings and activity logs |
| Log Analytics workspace | Log Analytics Reader | Query log tables |

For each: resource → **Access control (IAM)** → **Add role assignment** → select role → assign SP. IAM changes take 1–5 minutes to propagate.

### Step 7 — Wait for Logs to Arrive

Diagnostic logs take **5–15 minutes** to appear in Log Analytics after activity occurs. Verify in the Portal:

```kql
StorageBlobLogs
| order by TimeGenerated desc
| take 20
```

---

## Configuration

Add the following to the root `.env` file:

```env
AZURE_STORAGE_TENANT_ID=<tenant-id>
AZURE_STORAGE_CLIENT_ID=<client-id>
AZURE_STORAGE_CLIENT_SECRET=<client-secret>
AZURE_STORAGE_SUBSCRIPTION_ID=<subscription-id>
```

| Variable | Description |
| --- | --- |
| `AZURE_STORAGE_TENANT_ID` | Azure AD tenant ID |
| `AZURE_STORAGE_CLIENT_ID` | Service principal application (client) ID |
| `AZURE_STORAGE_CLIENT_SECRET` | Service principal client secret |
| `AZURE_STORAGE_SUBSCRIPTION_ID` | Fallback subscription ID if the SP cannot list subscriptions |

---

## Usage

```bash
# Run from the Azure Storage directory with the project venv activated
python fetch_azure_storage_logs.py
```

Output files are written to `logs/` and `report/` with timestamps in their filenames. `generate_bom.py` can also be run standalone to reprocess all existing files in `logs/`.

---

## How It Works

`fetch_azure_storage_logs.py` runs the following pipeline per subscription:

1. Lists all storage accounts via `ResourceManagementClient`
2. Collects diagnostic settings from the account and all four sub-resources (blob, file, queue, table)
3. Fetches Activity Logs for the last 24 hours via `MonitorManagementClient`
4. Queries all four storage log tables in each discovered Log Analytics workspace
5. Writes all results to `logs/azure_storage_<timestamp>.json`
6. Invokes `generate_bom.py` to produce `report/bom_<timestamp>.json`

**Log sources collected per account:**

| Source | Log Types |
| --- | --- |
| Azure Activity Logs | Storage account create/delete/update, key rotation, access policy changes |
| `StorageBlobLogs` | `PutBlob`, `GetBlob`, `DeleteBlob`, `CopyBlob`, `ListBlobs`, SAS auth events |
| `StorageFileLogs` | File share read/write/delete operations |
| `StorageQueueLogs` | Queue message enqueue/dequeue/delete |
| `StorageTableLogs` | Table entity insert/query/delete |

---

## Required Permissions

| Resource | Role |
| --- | --- |
| Subscription | Reader |
| Storage account | Reader |
| Log Analytics workspace | Log Analytics Reader |

---

## Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| `Found 0 subscription(s)` | SP has no role at subscription level | Assign Reader on the subscription in IAM, or set `AZURE_STORAGE_SUBSCRIPTION_ID` |
| `Cannot list storage accounts` | SP lacks Reader on the subscription | Assign Reader at subscription or resource group scope |
| `no_log_analytics_workspace_configured` | No diagnostic settings point to Log Analytics | Complete Step 5 to enable diagnostic settings on the storage account |
| `StorageBlobLogs` returns empty | Logs not yet arrived | Wait 5–15 minutes after activity occurs, then re-run |
| `insufficient_permissions` on diagnostic settings | SP cannot read diagnostic config | Assign Reader on the storage account in IAM |
| `AuthenticationError` | Incorrect credentials in `.env` | Verify the three `AZURE_STORAGE_*` values match the app registration |
