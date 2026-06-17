# Microsoft 365 — Audit Log Collector

This module connects to the **Microsoft 365 Management Activity API** and fetches audit logs across all major M365 workloads — Azure Active Directory, Exchange, SharePoint, DLP, and General. All collected logs are merged into a single JSON output file for analysis and retention.

---

## Folder Structure

```
Microsoft 365/
├── fetch_m365_logs.py       # Main script — fetches and aggregates M365 audit logs
└── m365_audit_logs.json     # Output — aggregated audit log records (~23.7 MB)
```

---

## Script: `fetch_m365_logs.py`

### Purpose
Authenticates with Microsoft 365 using OAuth2 Client Credentials, enables audit log subscriptions for all relevant content types, retrieves available content blobs for the last 24 hours, downloads each blob, and writes all records into a single aggregated JSON file.

### Configuration

Environment variables (set in root `.env`):

| Variable | Description |
|---|---|
| `M365_TENANT_ID` | Azure AD tenant ID for the M365 organization |
| `M365_CLIENT_ID` | Application (client) ID of the registered Azure AD app |
| `M365_CLIENT_SECRET` | Client secret for the Azure AD app |

Runtime constants:

```python
CONTENT_TYPES = [
    "Audit.AzureActiveDirectory",
    "Audit.Exchange",
    "Audit.SharePoint",
    "Audit.General",
    "DLP.All"
]

OUTPUT_FILE = "m365_audit_logs.json"
```

The time window is always **the last 24 hours** (computed at runtime using `datetime.utcnow()`).

### Authentication

Uses MSAL (`msal.ConfidentialClientApplication`) with the Client Credentials flow:

```python
app = msal.ConfidentialClientApplication(
    client_id=M365_CLIENT_ID,
    authority=f"https://login.microsoftonline.com/{M365_TENANT_ID}",
    client_credential=M365_CLIENT_SECRET
)
token = app.acquire_token_for_client(scopes=["https://manage.office.com/.default"])
```

### Functions

#### `get_access_token()`
Acquires an OAuth2 bearer token from Azure AD using MSAL. Raises `Exception` if token acquisition fails, logging the MSAL error description.

#### `start_subscription(token, content_type)`
Calls the Management Activity API to enable a subscription for the given content type. This is idempotent — if the subscription already exists (HTTP 400 with `AF20024` error code), the function silently continues. Subscriptions must be active before content can be listed.

API endpoint:
```
POST https://manage.office.com/api/v1.0/{tenant_id}/activity/feed/subscriptions/start
     ?contentType={content_type}&PublisherIdentifier={tenant_id}
```

#### `list_content(token, content_type)`
Lists available content blobs for the given content type within the 24-hour window. Returns a list of content descriptor objects, each containing a `contentUri` for downloading.

API endpoint:
```
GET https://manage.office.com/api/v1.0/{tenant_id}/activity/feed/subscriptions/content
    ?contentType={content_type}&startTime={start}&endTime={end}&PublisherIdentifier={tenant_id}
```

#### `fetch_content_blob(token, content_uri)`
Downloads the actual audit log records from a content blob URI. Returns a list of audit record objects.

#### `main()`
Full workflow:
1. Load `.env` variables
2. Acquire OAuth2 token
3. For each content type: start subscription → list blobs → download each blob → collect records
4. Write all records to `m365_audit_logs.json`

### Running the Script

```bash
# From the project root, with venv activated
cd "Microsoft 365"
python fetch_m365_logs.py
```

The script will print progress (subscription status, blob counts, record counts per content type) and write `m365_audit_logs.json` on completion.

---

## Output File: `m365_audit_logs.json`

A flat JSON array of all audit log records collected from all content types. Current size: **~23.7 MB** (~595,000 lines).

### Record Schema

Each record is a JSON object. Fields vary by workload, but common fields include:

| Field | Type | Description |
|---|---|---|
| `CreationTime` | ISO 8601 string | When the audited event occurred |
| `Id` | GUID string | Unique identifier for this audit record |
| `Operation` | string | The operation that was audited (e.g., `UserLoggedIn`, `FileAccessed`) |
| `OrganizationId` | GUID string | The M365 organization tenant ID |
| `RecordType` | integer | Numeric code indicating the workload/event category |
| `ResultStatus` | string | Outcome of the operation (`Success`, `Failure`, `Partial`) |
| `UserKey` | string | Unique key for the acting user |
| `UserId` | string | UPN of the user who performed the action |
| `Workload` | string | M365 service (e.g., `AzureActiveDirectory`, `Exchange`, `SharePoint`) |
| `ClientIP` | string | IP address of the client that initiated the action |
| `ObjectId` | string | The resource that was acted upon |

### Workload-Specific Fields

#### Azure Active Directory (`Workload: AzureActiveDirectory`)

| Field | Description |
|---|---|
| `ExtendedProperties` | Array of `{Name, Value}` pairs with extra context |
| `Actor` | Array of `{ID, Type}` for the principal performing the action |
| `Target` | Array of `{ID, Type}` for the object being acted upon |
| `DeviceProperties` | Array of `{Name, Value}` with OS, browser, compliance, session info |
| `ModifiedProperties` | Array of `{Name, NewValue, OldValue}` for directory object changes |

**Common `ExtendedProperties` Names:**
- `ResultStatusDetail` — detailed outcome (e.g., `Redirect`, `MFARequired`)
- `UserAgent` — browser/client user agent string
- `RequestType` — OAuth2 flow type (e.g., `OAuth2:Authorize`, `SAML:Token`)
- `UserAuthenticationMethod` — how the user authenticated

**Common `DeviceProperties` Names:**
- `OS` — operating system (e.g., `Windows10`, `MacOS`)
- `BrowserType` — browser (e.g., `Chrome`, `Edge`)
- `IsCompliantAndManaged` — Intune compliance status
- `SessionId` — authentication session identifier
- `TrustType` — device trust type (e.g., `Workplace`, `AzureAD`)

#### Exchange (`Workload: Exchange`)
Includes fields like `MailboxOwnerUPN`, `ClientInfoString`, `MailboxGuid`, `LogonType`, `OperationProperties`.

#### SharePoint (`Workload: SharePoint`)
Includes fields like `SiteUrl`, `ItemType`, `ItemName`, `ListId`, `SourceFileName`, `UserAgent`.

#### DLP (`ContentType: DLP.All`)
Includes fields like `PolicyDetails`, `SensitiveInfoDetectionIsIncluded`, `SharePointMetaData`.

### Sample Record (Azure AD Login)

```json
{
  "CreationTime": "2026-06-16T10:35:33",
  "Id": "bff620d4-6669-4212-91fa-13b6a6485000",
  "Operation": "UserLoggedIn",
  "OrganizationId": "76961126-e318-48ae-a31f-884b4af49fd1",
  "RecordType": 15,
  "ResultStatus": "Success",
  "UserKey": "c239af0e-1484-42a6-bb79-134bda4e212d",
  "UserId": "anns@cytexio.onmicrosoft.com",
  "Workload": "AzureActiveDirectory",
  "ClientIP": "203.128.27.61",
  "ObjectId": "499b84ac-1321-427f-aa17-267ca6975798",
  "ExtendedProperties": [
    { "Name": "ResultStatusDetail", "Value": "Redirect" },
    { "Name": "UserAgent", "Value": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)..." },
    { "Name": "RequestType", "Value": "OAuth2:Authorize" }
  ],
  "Actor": [
    { "ID": "c239af0e-1484-42a6-bb79-134bda4e212d", "Type": 0 }
  ],
  "Target": [
    { "ID": "499b84ac-1321-427f-aa17-267ca6975798", "Type": 0 }
  ],
  "DeviceProperties": [
    { "Name": "OS", "Value": "Windows10" },
    { "Name": "BrowserType", "Value": "Chrome" }
  ]
}
```

---

## Content Types Reference

| Content Type | Workload | Typical Operations |
|---|---|---|
| `Audit.AzureActiveDirectory` | Azure AD | `UserLoggedIn`, `Add member to role`, `Update user`, `Delete application` |
| `Audit.Exchange` | Exchange Online | `MailboxLogin`, `Send`, `MoveToDeletedItems`, `Set-Mailbox` |
| `Audit.SharePoint` | SharePoint / OneDrive | `FileAccessed`, `FileModified`, `SharingInvitationCreated`, `PageViewed` |
| `Audit.General` | M365 General | Teams events, Planner, Sway, Stream |
| `DLP.All` | All DLP-enabled services | Policy matches, sensitive data detections, overrides |

---

## Required Azure AD App Permissions

The registered Azure AD application must have the following **application permission** (not delegated):

| API | Permission | Type |
|---|---|---|
| Office 365 Management APIs | `ActivityFeed.Read` | Application |

After granting, an admin must click **Grant admin consent** in the Azure Portal.

To register and configure via Azure CLI:
```bash
# Create app registration
az ad app create --display-name "M365LogCollector"

# Add ActivityFeed.Read permission (requires manual admin consent in portal)
# App ID for Office 365 Management APIs: c5393580-f805-4401-95e8-94b7a6ef2fc2
# Permission GUID for ActivityFeed.Read: 594c1fb6-4f81-4475-ae41-0c394909246c
```

---

## Troubleshooting

| Issue | Likely Cause | Resolution |
|---|---|---|
| `AADSTS700016: Application not found` | Wrong `M365_CLIENT_ID` | Verify the app registration exists in the correct tenant |
| `AADSTS70011: Invalid scope` | Wrong or missing API permission | Add `ActivityFeed.Read` application permission and grant admin consent |
| HTTP 403 on subscription start | Admin consent not granted | Go to Azure Portal > App registrations > API permissions > Grant admin consent |
| Empty results for a content type | No events in the 24h window, or subscription was just enabled | Subscriptions can take up to 12 hours after first enablement to surface content |
| `m365_audit_logs.json` is very large | High-volume tenant | Consider filtering by `UserId`, `Operation`, or `Workload` inside `fetch_content_blob()` |
