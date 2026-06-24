# Microsoft — Log Collectors

Six collectors spanning Azure and Microsoft 365. Each authenticates via a Service Principal or registered app, fetches the last 24 hours of service activity, and generates a CycloneDX 1.6 Bill of Materials report.

---

## Modules

| Module | What It Collects |
| --- | --- |
| [Azure Cosmos DB](Azure%20Cosmos%20DB/) | Cosmos DB accounts across all subscriptions; describes API type (SQL/MongoDB/Cassandra/Gremlin/Table), consistency level, backup policy, geo-replication locations, database list; activity logs via Azure Monitor |
| [Azure Entra ID](Azure%20Entra%20ID/) | Users, groups, service principals, app registrations, and directory roles via Microsoft Graph API; last 24 hours of directory audit logs |
| [Azure Machine Learning](Azure%20Machine%20Learning/) | Workspace, compute cluster, experiment run, and model registry events; describes AML resources (compute type, node count, VM SKU) |
| [Azure OpenAI](Azure%20OpenAI/) | Diagnostic logs from Azure Cognitive Services / OpenAI resources via KQL queries against a Log Analytics Workspace (connection test, table counts, diagnostic samples) |
| [Azure Storage](Azure%20Storage/) | Blob, Queue, File, and Table storage diagnostic logs; describes storage accounts (SKU, replication tier, access tier, encryption, lifecycle policies) |
| [Microsoft 365](Microsoft%20365/) | Audit logs across Exchange, SharePoint, Teams, and Azure AD via the M365 Management Activity API |

---

## Credentials

Each module uses its own Service Principal or registered application so permissions stay scoped to only what each collector needs. Add module-specific variables to the root `.env`:

| Module | Environment Variables |
| --- | --- |
| Azure Cosmos DB | `AZURE_COSMOSDB_TENANT_ID`, `AZURE_COSMOSDB_CLIENT_ID`, `AZURE_COSMOSDB_CLIENT_SECRET` |
| Azure Entra ID | `AZURE_ENTRAID_TENANT_ID`, `AZURE_ENTRAID_CLIENT_ID`, `AZURE_ENTRAID_CLIENT_SECRET` |
| Azure Machine Learning | `AZURE_AML_TENANT_ID`, `AZURE_AML_CLIENT_ID`, `AZURE_AML_CLIENT_SECRET`, `AZURE_AML_WORKSPACE_ID` |
| Azure OpenAI | `AZURE_TENANT_ID`, `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET`, `AZURE_WORKSPACE_ID` |
| Azure Storage | `AZURE_STORAGE_TENANT_ID`, `AZURE_STORAGE_CLIENT_ID`, `AZURE_STORAGE_CLIENT_SECRET`, `AZURE_STORAGE_SUBSCRIPTION_ID` |
| Microsoft 365 | `M365_TENANT_ID`, `M365_CLIENT_ID`, `M365_CLIENT_SECRET` |

The exact permission required per module (RBAC role or Graph API application permission) is listed in each module's `README.md`.

> **Note:** Entra ID and Microsoft 365 modules use Microsoft Graph API (`msal` + `requests`). All Azure resource modules use `azure-identity` (`ClientSecretCredential`) + Azure SDK clients.

---

## Running a Collector

```bash
cd "Microsoft/<Service Name>"
python fetch_<service>_logs.py
```

Outputs are written to `logs/` and `report/` with timestamps. `generate_bom.py` can be run standalone to reprocess any existing `logs/` files.
