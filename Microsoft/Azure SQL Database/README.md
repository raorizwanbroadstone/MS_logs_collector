# Azure SQL Database — Log Collector

Connects to **Azure SQL Database** via the Azure SDK, enumerates all SQL servers and their databases across all subscriptions, describes each with its full configuration (SKU tier, collation, max size, redundancy, backup policy), fetches the last 24 hours of activity logs, and generates a **CycloneDX 1.6 Bill of Materials** report.

---

## Structure

```
Azure SQL Database/
├── fetch_azuresql_logs.py    # Enumerates SQL servers and databases, fetches activity logs
├── generate_bom.py           # Streams logs, deduplicates entities, produces CycloneDX BOM
├── logs/                     # Output: timestamped raw JSON
└── report/                   # Output: timestamped CycloneDX BOM reports
```

---

## Setup

### 1 — Install the SQL management SDK

```bash
pip install azure-mgmt-sql
```

> This package is required for database enumeration and detailed configuration. Without it, only basic server enumeration is available.

### 2 — Add credentials to `.env`

Add the following to the root `.env` file:

```env
AZURE_SQL_TENANT_ID=<your-tenant-id>
AZURE_SQL_CLIENT_ID=<your-client-id>
AZURE_SQL_CLIENT_SECRET=<your-client-secret>
```

### How to create the Service Principal and generate credentials

**Step 1 — Register an application in Entra ID**

1. Open the [Azure Portal](https://portal.azure.com/) and go to **Microsoft Entra ID** → **App registrations**
2. Click **New registration**
3. Enter a name (e.g. `azuresql-bom-collector`) → **Register**
4. Copy the **Directory (tenant) ID** → set as `AZURE_SQL_TENANT_ID`
5. Copy the **Application (client) ID** → set as `AZURE_SQL_CLIENT_ID`

**Step 2 — Create a client secret**

1. In the registered app, go to **Certificates & secrets** → **New client secret**
2. Enter a description and set an expiry → **Add**
3. Copy the **Value** immediately → set as `AZURE_SQL_CLIENT_SECRET`

> The secret value is shown only once. Store it immediately.

**Step 3 — Assign the Reader role at subscription scope**

1. Go to **Subscriptions** in the Azure Portal
2. Open the subscription that contains your SQL servers
3. Go to **Access control (IAM)** → **Add role assignment**
4. Select the **Reader** role → **Next**
5. Under **Members**, select **User, group, or service principal** → **Select members**
6. Search for and select your registered application (e.g. `azuresql-bom-collector`) → **Select**
7. **Review + assign** → **Review + assign**
8. Repeat for each subscription that has SQL servers

> The **Reader** role grants read access to all resources in the subscription, including SQL servers, database configurations, activity logs, and resource groups. It does not grant access to data stored in the databases.

---

## Required Azure Permissions

| Permission | Scope | Why Needed |
| --- | --- | --- |
| `Reader` (built-in role) | Subscription | `Microsoft.Sql/servers/read` and `Microsoft.Sql/servers/databases/read` — enumerate and describe SQL servers and databases |
| `Reader` (built-in role) | Subscription | `Microsoft.Insights/eventtypes/read` — read activity log events via Azure Monitor |

A single **Reader** role assignment at subscription scope covers both requirements.

---

## Usage

```bash
# Run from the Azure SQL Database directory with the project venv activated
python fetch_azuresql_logs.py
```

Output files are written to `logs/` and `report/` with timestamps in their filenames. `generate_bom.py` can also be run standalone to reprocess all existing files in `logs/`.

---

## How It Works

`fetch_azuresql_logs.py` executes the following pipeline on each run:

1. Enumerates all accessible **Azure subscriptions** via `SubscriptionClient`
2. For each subscription, lists all SQL servers via `SqlManagementClient.servers.list()`
3. For each server, calls `SqlManagementClient.databases.list_by_server()` to enumerate all databases including their SKU, status, collation, size, and redundancy configuration
4. Fetches the last 24 hours of **activity log events** via `MonitorManagementClient.activity_logs.list()` per server
5. Writes all data to `logs/azuresql_logs_<timestamp>.json`
6. Invokes `generate_bom.py` to produce `report/bom_<timestamp>.json`

**BOM mapping:**

| Entity | CycloneDX Role | BOM Properties |
| --- | --- | --- |
| SQL server | Service | FQDN, version, state, admin login, public network access, TLS version, database count |
| Database | Service (nested under server) | SKU tier, SKU name, capacity, status, collation, max size, zone redundancy, read scale, HA replica count, backup storage redundancy, elastic pool membership, created date |
| Resource provider (from activity logs) | Service | Resource provider name |
| Caller application (from activity logs) | Component | Azure app ID of the calling application |

**BOM properties captured per database:**

| Property | Source |
| --- | --- |
| `azure:ParentServer` | Name of the SQL server this database belongs to |
| `azure:SkuTier` | Service tier (Basic, Standard, Premium, GeneralPurpose, BusinessCritical, Hyperscale) |
| `azure:SkuName` | SKU name (e.g. GP_Gen5_2, S1, P2) |
| `azure:SkuCapacity` | DTUs or vCores, depending on tier |
| `azure:Status` | Online, Offline, Creating, Scaling, etc. |
| `azure:Collation` | Default database collation |
| `azure:MaxSizeGB` | Maximum data size in GB |
| `azure:ZoneRedundant` | Whether the database uses availability zone redundancy |
| `azure:ReadScale` | Whether read-only replicas serve read workloads (Enabled/Disabled) |
| `azure:HAReplicaCount` | Number of high-availability replicas |
| `azure:BackupStorageRedundancy` | Local, Zone, or Geo backup redundancy |
| `azure:InElasticPool` | Whether the database is in an elastic pool |
| `azure:CreatedAt` | Database creation timestamp |

**Activity log operations captured (examples):**

| Operation | What Changed |
| --- | --- |
| `Microsoft.Sql/servers/write` | SQL server created or modified |
| `Microsoft.Sql/servers/delete` | SQL server deleted |
| `Microsoft.Sql/servers/databases/write` | Database created or modified |
| `Microsoft.Sql/servers/databases/delete` | Database deleted |
| `Microsoft.Sql/servers/firewallRules/write` | Firewall rule added or changed |
| `Microsoft.Sql/servers/firewallRules/delete` | Firewall rule removed |
| `Microsoft.Sql/servers/administratorOperationResults/read` | Admin operation result read |
| `Microsoft.Sql/servers/databases/backupShortTermRetentionPolicies/write` | Backup retention policy changed |
| `Microsoft.Sql/servers/databases/export/action` | Database exported (BACPAC) |
| `Microsoft.Sql/servers/databases/import/action` | Database imported |

> **Security note:** `firewallRules/write` and `databases/export/action` are high-value audit signals — unexpected firewall changes or database exports should be investigated.

---

## Troubleshooting

| Error | Cause | Fix |
| --- | --- | --- |
| `Missing credentials` | Env vars not set in `.env` | Add `AZURE_SQL_TENANT_ID`, `AZURE_SQL_CLIENT_ID`, `AZURE_SQL_CLIENT_SECRET` |
| `AuthenticationError` | Invalid client ID, secret, or tenant ID | Verify credentials match the registered app in Entra ID |
| `Authorization failed` | Service principal missing the Reader role | Assign **Reader** role at subscription scope to the registered application |
| 0 servers found | No SQL servers in any accessible subscription | Check the service principal has access to the correct subscription |
| Database details empty | `azure-mgmt-sql` not installed | Run `pip install azure-mgmt-sql` |
| `master` database appears in BOM | System database included by the SDK | Expected — `master` is a valid part of the server's resource inventory |
