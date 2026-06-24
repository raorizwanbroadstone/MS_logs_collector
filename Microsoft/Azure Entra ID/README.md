# Azure Entra ID — Log Collector

Connects to **Microsoft Entra ID** (formerly Azure Active Directory) via the **Microsoft Graph API**, enumerates all directory resources in the tenant (users, groups, service principals, app registrations, directory roles), fetches the last 24 hours of directory audit logs, and generates a **CycloneDX 1.6 Bill of Materials** report.

---

## Structure

```
Azure Entra ID/
├── fetch_entraid_logs.py    # Queries Graph API and enumerates 
├── generate_bom.py          # Streams logs, deduplicates 
├── logs/                    # Output: timestamped raw JSON
└── report/                  # Output: timestamped CycloneDX BOM reports
```

---

## Setup

Add the following to the root `.env` file:

```env
AZURE_ENTRAID_TENANT_ID=<your-tenant-id>
AZURE_ENTRAID_CLIENT_ID=<your-client-id>
AZURE_ENTRAID_CLIENT_SECRET=<your-client-secret>
```

### How to create the Service Principal and generate credentials

**Step 1 — Register an application in Entra ID**

1. Open the [Azure Portal](https://portal.azure.com/) and go to **Microsoft Entra ID** → **App registrations**
2. Click **New registration**
3. Enter a name (e.g. `entraid-bom-collector`) → **Register**
4. Copy the **Directory (tenant) ID** → set as `AZURE_ENTRAID_TENANT_ID`
5. Copy the **Application (client) ID** → set as `AZURE_ENTRAID_CLIENT_ID`

**Step 2 — Create a client secret**

1. In the registered app, go to **Certificates & secrets** → **New client secret**
2. Enter a description and set an expiry → **Add**
3. Copy the **Value** immediately → set as `AZURE_ENTRAID_CLIENT_SECRET`

> The secret value is shown only once. Store it immediately.

**Step 3 — Grant Microsoft Graph API permissions**

1. In the registered app, go to **API permissions** → **Add a permission** → **Microsoft Graph** → **Application permissions**
2. Search for and add each of the following permissions:

   | Permission | Why Needed |
   | --- | --- |
   | `User.Read.All` | Enumerate all users and read their properties |
   | `Group.Read.All` | Enumerate all groups and read their properties |
   | `GroupMember.Read.All` | Read group member counts |
   | `Application.Read.All` | Enumerate app registrations and service principals |
   | `Directory.Read.All` | Read directory roles and other directory objects |
   | `AuditLog.Read.All` | Read directory audit logs |

3. Click **Grant admin consent for [your tenant]** → **Yes**

> All permissions are **Application** type (not Delegated) since this collector runs without a signed-in user.

---

## Required Microsoft Graph Permissions

| Permission | Type | Why Needed |
| --- | --- | --- |
| `User.Read.All` | Application | Read all users |
| `Group.Read.All` | Application | Read all groups |
| `GroupMember.Read.All` | Application | Read group member counts |
| `Application.Read.All` | Application | Read app registrations and service principals |
| `Directory.Read.All` | Application | Read directory roles |
| `AuditLog.Read.All` | Application | Read directory audit logs |

---

## Usage

```bash
# Run from the Azure Entra ID directory with the project venv activated
python fetch_entraid_logs.py
```

Output files are written to `logs/` and `report/` with timestamps in their filenames. `generate_bom.py` can also be run standalone to reprocess all existing files in `logs/`.

---

## How It Works

`fetch_entraid_logs.py` executes the following pipeline on each run:

1. Acquires an OAuth 2.0 client credentials token from Microsoft identity platform
2. Paginates through all **users** via `GET /v1.0/users` (handles `@odata.nextLink`)
3. Paginates through all **groups** via `GET /v1.0/groups` + per-group member count via `GET /v1.0/groups/{id}/members/$count`
4. Paginates through all **service principals** via `GET /v1.0/servicePrincipals`
5. Paginates through all **app registrations** via `GET /v1.0/applications`
6. Fetches all **active directory roles** via `GET /v1.0/directoryRoles`
7. Fetches the last 24 hours of **directory audit logs** via `GET /v1.0/auditLogs/directoryAudits`
8. Writes all collected resources and audit events to `logs/entraid_logs_<timestamp>.json`
9. Invokes `generate_bom.py` to produce `report/bom_<timestamp>.json`

**BOM mapping:**

| Resource Type | CycloneDX Role | BOM Properties |
| --- | --- | --- |
| User | Component | UPN, mail, account enabled, user type, job title, department, created date |
| Group | Component | Security enabled, mail enabled, is dynamic, member count, created date |
| ServicePrincipal | Component | App ID, SP type, account enabled, created date |
| AppRegistration | Service | App ID, created date, sign-in audience |
| DirectoryRole | Service | Role template ID, description |

**Audit log events captured (examples):**

| Activity | What Changed |
| --- | --- |
| `Add user` | New user created in directory |
| `Delete user` | User deleted |
| `Update user` | User properties modified |
| `Add member to group` | User added to group |
| `Remove member from group` | User removed from group |
| `Add application` | New app registration created |
| `Add service principal` | New service principal provisioned |
| `Add app role assignment to service principal` | Role assigned to service principal |
| `Update application` | App registration modified |
| `Add member to role` | User or SP assigned to directory role |
| `Remove member from role` | User or SP removed from directory role |
| `Reset user password` | User password reset |
| `Change user password` | User changed own password |

---

## Troubleshooting

| Error | Cause | Fix |
| --- | --- | --- |
| `Token acquisition failed` | Invalid client ID, secret, or tenant ID | Verify `AZURE_ENTRAID_*` values in `.env` match the app registration |
| `401 Unauthorized` | Token not granted or missing scope | Ensure admin consent was granted for all 6 permissions |
| `403 Forbidden on /users` | Missing `User.Read.All` | Add and grant admin consent for `User.Read.All` |
| `403 Forbidden on /auditLogs/directoryAudits` | Missing `AuditLog.Read.All` | Add and grant admin consent for `AuditLog.Read.All` |
| `403 Forbidden on /groups/{id}/members/$count` | Missing `GroupMember.Read.All` | Add and grant admin consent for `GroupMember.Read.All` |
| 0 resources collected | Correct credentials but empty tenant | Verify the tenant has users/groups configured |
| `ConsistencyLevel: eventual` header required | Advanced filters need this header | Already set in `graph_get()` — no action needed |
