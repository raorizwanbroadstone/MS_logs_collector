# Azure Cosmos DB — Log Collector

Connects to **Azure Cosmos DB** via the Azure SDK, enumerates all Cosmos DB accounts across all subscriptions, describes each account's full configuration (API type, consistency level, backup policy, locations, databases), fetches the last 24 hours of activity logs, and generates a **CycloneDX 1.6 Bill of Materials** report.

---

## Structure

```
Azure Cosmos DB/
├── fetch_cosmosdb_logs.py    # Enumerates Cosmos DB accounts
├── generate_bom.py           # Streams logs, deduplicates 
├── logs/                     # Output: timestamped raw JSON
└── report/                   # Output: timestamped CycloneDX BOM reports
```

---

## Setup

### 1 — Install the optional SDK

```bash
pip install azure-mgmt-cosmosdb
```

> This package is required for full account details (API type, consistency level, databases). Without it, basic account enumeration still works but properties are limited.

### 2 — Add credentials to `.env`

Add the following to the root `.env` file:

```env
AZURE_COSMOSDB_TENANT_ID=<your-tenant-id>
AZURE_COSMOSDB_CLIENT_ID=<your-client-id>
AZURE_COSMOSDB_CLIENT_SECRET=<your-client-secret>
```

### How to create the Service Principal and generate credentials

**Step 1 — Register an application in Entra ID**

1. Open the [Azure Portal](https://portal.azure.com/) and go to **Microsoft Entra ID** → **App registrations**
2. Click **New registration**
3. Enter a name (e.g. `cosmosdb-bom-collector`) → **Register**
4. Copy the **Directory (tenant) ID** → set as `AZURE_COSMOSDB_TENANT_ID`
5. Copy the **Application (client) ID** → set as `AZURE_COSMOSDB_CLIENT_ID`

**Step 2 — Create a client secret**

1. In the registered app, go to **Certificates & secrets** → **New client secret**
2. Enter a description and set an expiry → **Add**
3. Copy the **Value** immediately → set as `AZURE_COSMOSDB_CLIENT_SECRET`

> The secret value is shown only once. Store it immediately.

**Step 3 — Assign the Reader role at subscription scope**

1. Go to **Subscriptions** in the Azure Portal
2. Open the subscription that contains your Cosmos DB accounts
3. Go to **Access control (IAM)** → **Add role assignment**
4. Select the **Reader** role → **Next**
5. Under **Members**, select **User, group, or service principal** → **Select members**
6. Search for and select your registered application (e.g. `cosmosdb-bom-collector`) → **Select**
7. **Review + assign** → **Review + assign**
8. Repeat for each subscription that has Cosmos DB accounts

> The **Reader** role grants read access to all resources in the subscription, including Cosmos DB accounts, activity logs, and resource groups. It does not grant access to data stored in the databases.

---

## Required Azure Permissions

| Permission | Scope | Why Needed |
| --- | --- | --- |
| `Reader` (built-in role) | Subscription | `Microsoft.DocumentDB/databaseAccounts/read` — enumerate and describe Cosmos DB accounts |
| `Reader` (built-in role) | Subscription | `Microsoft.Insights/eventtypes/read` — read activity log events via Azure Monitor |

A single **Reader** role assignment at subscription scope covers both requirements.

---

## Usage

```bash
# Run from the Azure Cosmos DB directory with the project venv activated
python fetch_cosmosdb_logs.py
```

Output files are written to `logs/` and `report/` with timestamps in their filenames. `generate_bom.py` can also be run standalone to reprocess all existing files in `logs/`.

---

## How It Works

`fetch_cosmosdb_logs.py` executes the following pipeline on each run:

1. Enumerates all accessible **Azure subscriptions** via `SubscriptionClient`
2. For each subscription, lists all Cosmos DB accounts via `ResourceManagementClient.resources.list(filter="resourceType eq 'Microsoft.DocumentDB/databaseAccounts'")`
3. For each account (if `azure-mgmt-cosmosdb` is installed), calls `CosmosDBManagementClient.database_accounts.get()` to retrieve:
   - API type (SQL, MongoDB, Cassandra, Gremlin, Table) — resolved from account capabilities
   - Consistency policy (level, max staleness prefix, max interval)
   - Backup policy type (Periodic or Continuous)
   - Geo-replication locations
   - Free tier and automatic failover status
   - Database list via the appropriate per-API resource client
4. Fetches the last 24 hours of **activity log events** via `MonitorManagementClient.activity_logs.list()` per account
5. Writes all data to `logs/cosmosdb_logs_<timestamp>.json`
6. Invokes `generate_bom.py` to produce `report/bom_<timestamp>.json`

**BOM mapping:**

| Entity | CycloneDX Role | BOM Properties |
| --- | --- | --- |
| Cosmos DB account | Service | API type, kind, provisioning state, public network access, free tier, automatic failover, backup policy type, document endpoint, consistency level, locations, database count |
| Resource provider (from activity logs) | Service | Resource provider name |
| Caller application (from activity logs) | Component | Azure app ID of the calling application |

**BOM properties captured per Cosmos DB account:**

| Property | Source |
| --- | --- |
| `azure:ApiType` | Account capabilities (SQL, MongoDB, Cassandra, Gremlin, Table) |
| `azure:Kind` | Account kind field |
| `azure:ProvisioningState` | Account provisioning state |
| `azure:PublicNetworkAccess` | Whether public access is enabled or disabled |
| `azure:FreeTierEnabled` | Whether the free tier is active |
| `azure:AutomaticFailoverEnabled` | Whether automatic geo-failover is enabled |
| `azure:BackupPolicyType` | Periodic or Continuous backup |
| `azure:DocumentEndpoint` | Account endpoint URL |
| `azure:ConsistencyLevel` | Default consistency level |
| `azure:MaxStalenessPrefix` | Bounded staleness: max stale requests |
| `azure:MaxIntervalSeconds` | Bounded staleness: max lag in seconds |
| `azure:Locations` | Comma-separated list of geo-replication regions |
| `azure:LocationCount` | Number of replication regions |
| `azure:DatabaseCount` | Number of databases/keyspaces in the account |
| `azure:Databases` | Comma-separated list of database names |

**Activity log operations captured (examples):**

| Operation | What Changed |
| --- | --- |
| `Microsoft.DocumentDB/databaseAccounts/write` | Account created or modified |
| `Microsoft.DocumentDB/databaseAccounts/delete` | Account deleted |
| `Microsoft.DocumentDB/databaseAccounts/sqlDatabases/write` | SQL database created or modified |
| `Microsoft.DocumentDB/databaseAccounts/sqlDatabases/delete` | SQL database deleted |
| `Microsoft.DocumentDB/databaseAccounts/mongodbDatabases/write` | MongoDB database created or modified |
| `Microsoft.DocumentDB/databaseAccounts/failoverPriorityChange/action` | Failover priority changed |
| `Microsoft.DocumentDB/databaseAccounts/regenerateKey/action` | Account key regenerated |
| `Microsoft.DocumentDB/databaseAccounts/listKeys/action` | Account keys listed |

> **Security note:** `regenerateKey/action` and `listKeys/action` are high-value audit signals indicating credential rotation or potential credential exposure.

---

## Troubleshooting

| Error | Cause | Fix |
| --- | --- | --- |
| `Missing credentials` | Env vars not set in `.env` | Add `AZURE_COSMOSDB_TENANT_ID`, `AZURE_COSMOSDB_CLIENT_ID`, `AZURE_COSMOSDB_CLIENT_SECRET` |
| `AuthenticationError` | Invalid client ID, secret, or tenant ID | Verify credentials match the registered app in Entra ID |
| `Authorization failed` | Service principal missing the Reader role | Assign **Reader** role at subscription scope to the registered application |
| 0 accounts found | No Cosmos DB accounts in any accessible subscription | Check that the service principal has access to the subscription containing your accounts |
| Account details empty (`details: {}`) | `azure-mgmt-cosmosdb` not installed | Run `pip install azure-mgmt-cosmosdb` |
| `Warning: azure-mgmt-cosmosdb not installed` | Package missing | Run `pip install azure-mgmt-cosmosdb` to enable full property collection |
| Databases list empty | API type mismatch or insufficient permissions | The Reader role covers Cosmos DB resource metadata but not data-plane access |
