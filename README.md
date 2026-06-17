# MS Logs Collector

A Python-based log aggregation tool for collecting diagnostic and audit logs from **Azure** and **Microsoft 365** environments. Designed for security monitoring, compliance auditing, and operational diagnostics.

---

## Project Structure

```
MS_logs/
├── Azure OpenAI and ML/              # Azure Log Analytics diagnostic log collector
│   ├── fetch_azure_diagnostic_logs.py
│   └── azure_diagnostic_logs/        # JSON output files from Azure queries
├── Microsoft 365/                    # Microsoft 365 audit log collector
│   ├── fetch_m365_logs.py
│   └── m365_audit_logs.json          # Aggregated M365 audit log output
├── requirements.txt                  # Python dependencies
├── .env                              # Credentials (not committed to git)
└── .gitignore
```

---

## Prerequisites

- Python 3.8+
- A Python virtual environment (included as `venv/`)
- Azure service principal with Log Analytics Reader access
- Microsoft 365 application with `ActivityFeed.Read` permissions

---

## Installation

```bash
# Clone the repository
git clone https://github.com/raorizwanbroadstone/MS_logs_collector.git
cd MS_logs_collector

# Create and activate virtual environment
python -m venv venv
venv\Scripts\activate        # Windows
# source venv/bin/activate   # Linux/macOS

# Install dependencies
pip install -r requirements.txt
```

---

## Configuration

Create a `.env` file in the project root with the following variables:

```env
# Azure Log Analytics credentials
AZURE_TENANT_ID=<your-azure-tenant-id>
AZURE_CLIENT_ID=<your-azure-client-id>
AZURE_CLIENT_SECRET=<your-azure-client-secret>
AZURE_WORKSPACE_ID=<your-log-analytics-workspace-id>

# Microsoft 365 credentials
M365_TENANT_ID=<your-m365-tenant-id>
M365_CLIENT_ID=<your-m365-client-id>
M365_CLIENT_SECRET=<your-m365-client-secret>
```

> **Security:** The `.env` file is excluded from version control via `.gitignore`. Never commit credentials.

---

## Dependencies

| Package | Version | Purpose |
|---|---|---|
| `azure-identity` | >=1.15.0 | Azure authentication (Service Principal) |
| `azure-mgmt-monitor` | >=6.0.0 | Azure Monitor management API |
| `azure-mgmt-resource` | >=23.0.0 | Azure resource management |
| `azure-monitor-query` | >=1.2.0 | Log Analytics KQL query client |
| `schedule` | >=1.2.0 | Task scheduling for periodic execution |

---

## Authentication Methods

### Azure (Log Analytics)
Uses **Service Principal** authentication via `ClientSecretCredential`:
- Requires: Tenant ID, Client ID, Client Secret
- Scope: Log Analytics Workspace Reader

### Microsoft 365 (Audit Logs)
Uses **OAuth2 Client Credentials Flow** via MSAL:
- Requires: Tenant ID, Client ID, Client Secret
- Scope: `https://manage.office.com/.default`
- Permission: `ActivityFeed.Read` (application-level)

---

## Modules

### [Azure OpenAI and ML/](Azure%20OpenAI%20and%20ML/)
Connects to Azure Log Analytics and runs KQL queries to pull diagnostic logs from Azure Cognitive Services / OpenAI resources. Outputs timestamped JSON files.

### [Microsoft 365/](Microsoft%20365/)
Connects to the Microsoft 365 Management Activity API to pull audit logs across Azure AD, Exchange, SharePoint, DLP, and General workloads. Outputs a single aggregated JSON file.

---

## Data Flow

```
.env credentials
      │
      ├──► Azure Service Principal ──► Log Analytics KQL ──► azure_diagnostic_logs/*.json
      │
      └──► M365 OAuth2 Token ──────────► Office 365 API  ──► m365_audit_logs.json
```

---

## Use Cases

- **Security Monitoring:** Track user sign-ins, IP addresses, device compliance, and suspicious activity.
- **Compliance Auditing:** Maintain audit trails for Azure resource changes and M365 user operations.
- **Incident Investigation:** Query specific time windows for events related to a security incident.
- **Operational Diagnostics:** Monitor Azure Cognitive Services / OpenAI resource health and activity.

---

## Repository

[https://github.com/raorizwanbroadstone/MS_logs_collector](https://github.com/raorizwanbroadstone/MS_logs_collector)
