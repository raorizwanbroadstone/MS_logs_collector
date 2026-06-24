import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import msal
import requests
from dotenv import load_dotenv
import generate_bom

load_dotenv()

TENANT_ID     = os.getenv("AZURE_ENTRAID_TENANT_ID")
CLIENT_ID     = os.getenv("AZURE_ENTRAID_CLIENT_ID")
CLIENT_SECRET = os.getenv("AZURE_ENTRAID_CLIENT_SECRET")

LOOKBACK_HOURS = 24
OUTPUT_DIR     = Path(__file__).parent / "logs"

GRAPH_BASE  = "https://graph.microsoft.com/v1.0"
GRAPH_SCOPE = "https://graph.microsoft.com/.default"


def get_access_token() -> str:
    authority = f"https://login.microsoftonline.com/{TENANT_ID}"
    app = msal.ConfidentialClientApplication(
        CLIENT_ID, authority=authority, client_credential=CLIENT_SECRET
    )
    result = app.acquire_token_for_client(scopes=[GRAPH_SCOPE])
    if "access_token" in result:
        return result["access_token"]
    raise RuntimeError(f"Token acquisition failed: {result.get('error_description')}")


def graph_get(token: str, path: str, params: dict | None = None) -> list[dict]:
    headers = {"Authorization": f"Bearer {token}", "ConsistencyLevel": "eventual"}
    url     = f"{GRAPH_BASE}{path}"
    results: list[dict] = []
    while url:
        resp = requests.get(url, headers=headers, params=params)
        resp.raise_for_status()
        data = resp.json()
        results.extend(data.get("value", []))
        url    = data.get("@odata.nextLink")
        params = None
    return results


def list_users(token: str) -> list[dict]:
    return graph_get(
        token, "/users",
        {"$select": "id,displayName,userPrincipalName,accountEnabled,createdDateTime,mail,userType,jobTitle,department", "$top": "999"},
    )


def list_groups(token: str) -> list[dict]:
    return graph_get(
        token, "/groups",
        {"$select": "id,displayName,description,groupTypes,securityEnabled,mailEnabled,createdDateTime", "$top": "999"},
    )


def get_group_member_count(token: str, group_id: str) -> int:
    headers = {"Authorization": f"Bearer {token}", "ConsistencyLevel": "eventual"}
    resp = requests.get(f"{GRAPH_BASE}/groups/{group_id}/members/$count", headers=headers)
    if resp.status_code == 200:
        try:
            return int(resp.text)
        except ValueError:
            return 0
    return 0


def list_service_principals(token: str) -> list[dict]:
    return graph_get(
        token, "/servicePrincipals",
        {"$select": "id,displayName,appId,servicePrincipalType,accountEnabled,createdDateTime,description", "$top": "999"},
    )


def list_app_registrations(token: str) -> list[dict]:
    return graph_get(
        token, "/applications",
        {"$select": "id,displayName,appId,createdDateTime,signInAudience,description", "$top": "999"},
    )


def list_directory_roles(token: str) -> list[dict]:
    return graph_get(
        token, "/directoryRoles",
        {"$select": "id,displayName,description,roleTemplateId"},
    )


def get_directory_audit_logs(token: str, start_time: datetime) -> list[dict]:
    start_str = start_time.strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        return graph_get(
            token, "/auditLogs/directoryAudits",
            {"$filter": f"activityDateTime ge {start_str}", "$top": "999", "$orderby": "activityDateTime desc"},
        )
    except Exception as exc:
        print(f"  Could not fetch audit logs: {exc}")
        return []


def main() -> None:
    if not all([TENANT_ID, CLIENT_ID, CLIENT_SECRET]):
        print("Missing credentials. Set AZURE_ENTRAID_TENANT_ID / AZURE_ENTRAID_CLIENT_ID / AZURE_ENTRAID_CLIENT_SECRET in .env")
        return

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    timestamp   = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H-%M-%S")
    output_file = OUTPUT_DIR / f"entraid_logs_{timestamp}.json"

    end_time   = datetime.now(timezone.utc)
    start_time = end_time - timedelta(hours=LOOKBACK_HOURS)

    print(f"Tenant : {TENANT_ID}")
    print(f"Window : {start_time.strftime('%Y-%m-%dT%H:%M:%SZ')} -> {end_time.strftime('%Y-%m-%dT%H:%M:%SZ')}\n")

    print("Acquiring Graph token...")
    token = get_access_token()
    print("  Token acquired.\n")

    resources: list[dict] = []

    print("Enumerating users...")
    try:
        users = list_users(token)
        print(f"  {len(users)} users")
        for user in users:
            resources.append({
                "resource_type":       "User",
                "id":                  user.get("id", ""),
                "display_name":        user.get("displayName", ""),
                "user_principal_name": user.get("userPrincipalName", ""),
                "account_enabled":     user.get("accountEnabled", False),
                "created_datetime":    user.get("createdDateTime", ""),
                "mail":                user.get("mail", ""),
                "user_type":           user.get("userType", ""),
                "job_title":           user.get("jobTitle", ""),
                "department":          user.get("department", ""),
            })
    except Exception as exc:
        print(f"  Error listing users: {exc}")

    print("Enumerating groups...")
    try:
        groups = list_groups(token)
        print(f"  {len(groups)} groups")
        for group in groups:
            group_id     = group.get("id", "")
            group_types  = group.get("groupTypes") or []
            member_count = get_group_member_count(token, group_id) if group_id else 0
            resources.append({
                "resource_type":    "Group",
                "id":               group_id,
                "display_name":     group.get("displayName", ""),
                "description":      group.get("description", ""),
                "security_enabled": group.get("securityEnabled", False),
                "mail_enabled":     group.get("mailEnabled", False),
                "created_datetime": group.get("createdDateTime", ""),
                "is_dynamic":       "DynamicMembership" in group_types,
                "member_count":     member_count,
            })
    except Exception as exc:
        print(f"  Error listing groups: {exc}")

    print("Enumerating service principals...")
    try:
        sps = list_service_principals(token)
        print(f"  {len(sps)} service principals")
        for sp in sps:
            resources.append({
                "resource_type":          "ServicePrincipal",
                "id":                     sp.get("id", ""),
                "display_name":           sp.get("displayName", ""),
                "app_id":                 sp.get("appId", ""),
                "service_principal_type": sp.get("servicePrincipalType", ""),
                "account_enabled":        sp.get("accountEnabled", False),
                "created_datetime":       sp.get("createdDateTime", ""),
                "description":            sp.get("description", ""),
            })
    except Exception as exc:
        print(f"  Error listing service principals: {exc}")

    print("Enumerating app registrations...")
    try:
        apps = list_app_registrations(token)
        print(f"  {len(apps)} app registrations")
        for app in apps:
            resources.append({
                "resource_type":    "AppRegistration",
                "id":               app.get("id", ""),
                "display_name":     app.get("displayName", ""),
                "app_id":           app.get("appId", ""),
                "created_datetime": app.get("createdDateTime", ""),
                "sign_in_audience": app.get("signInAudience", ""),
                "description":      app.get("description", ""),
            })
    except Exception as exc:
        print(f"  Error listing app registrations: {exc}")

    print("Enumerating active directory roles...")
    try:
        roles = list_directory_roles(token)
        print(f"  {len(roles)} active directory roles")
        for role in roles:
            resources.append({
                "resource_type":    "DirectoryRole",
                "id":               role.get("id", ""),
                "display_name":     role.get("displayName", ""),
                "description":      role.get("description", ""),
                "role_template_id": role.get("roleTemplateId", ""),
            })
    except Exception as exc:
        print(f"  Error listing directory roles: {exc}")

    print("\nFetching directory audit logs (last 24h)...")
    audit_logs = get_directory_audit_logs(token, start_time)
    print(f"  {len(audit_logs)} audit events")

    output_data = {
        "collectionTime": datetime.now(timezone.utc).isoformat(),
        "tenant_id":      TENANT_ID,
        "summary": {
            "resources_collected": len(resources),
            "audit_log_events":    len(audit_logs),
        },
        "resources":  resources,
        "audit_logs": audit_logs,
    }

    with output_file.open("w", encoding="utf-8") as fh:
        json.dump(output_data, fh, indent=2, ensure_ascii=False)

    print(f"\nCompleted.")
    print(f"  Total resources collected: {len(resources)}")
    print(f"  Total audit log events:    {len(audit_logs)}")
    print(f"  Output saved to:           {output_file}")

    print("\nGenerating BOM report...")
    generate_bom.main(target_file=output_file)


if __name__ == "__main__":
    main()
