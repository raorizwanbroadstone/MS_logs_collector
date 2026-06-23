# Microsoft — Log Collectors

Four collectors spanning Azure and Microsoft 365. Each authenticates via a Service Principal, fetches the last 24 hours of service activity, and generates a CycloneDX 1.6 Bill of Materials report.

---

## Modules

| Module | What It Collects |
| --- | --- |
| [Azure OpenAI](Azure%20OpenAI/) | Diagnostic logs from Azure Cognitive Services / OpenAI resources via KQL queries against a Log Analytics Workspace (connection test, table counts, diagnostic samples) |
| [Azure Machine Learning](Azure%20Machine%20Learning/) | Workspace, compute cluster, experiment run, and model registry events; describes AML resources (compute type, node count, VM SKU) |
| [Azure Storage](Azure%20Storage/) | Blob, Queue, File, and Table storage diagnostic logs; describes storage accounts (SKU, replication tier, access tier, encryption, lifecycle policies) |
| [Microsoft 365](Microsoft%20365/) | Audit logs across Exchange, SharePoint, Teams, and Azure AD via the M365 Management Activity API |

---

## Credentials

All Azure modules share a Service Principal. Add these to the root `.env`:

```env
AZURE_TENANT_ID=<tenant-id>
AZURE_CLIENT_ID=<client-id>
AZURE_CLIENT_SECRET=<client-secret>
```

Additional variables per module (workspace IDs, subscription IDs, etc.) are listed in each module's `README.md`, along with the exact RBAC role assignment required.

---

## Running a Collector

```bash
cd "Microsoft/<Service Name>"
python fetch_<service>_logs.py
```

Outputs are written to `logs/` and `report/` with timestamps. `generate_bom.py` can be run standalone to reprocess any existing `logs/` files.
