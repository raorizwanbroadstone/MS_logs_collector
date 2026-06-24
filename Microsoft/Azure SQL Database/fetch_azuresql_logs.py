import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from azure.identity import ClientSecretCredential
from azure.mgmt.monitor import MonitorManagementClient
from azure.mgmt.resource import ResourceManagementClient
from azure.mgmt.subscription import SubscriptionClient
from dotenv import load_dotenv
import generate_bom

try:
    from azure.mgmt.sql import SqlManagementClient
    SQL_SDK_AVAILABLE = True
except ImportError:
    SQL_SDK_AVAILABLE = False

load_dotenv()

TENANT_ID     = os.getenv("AZURE_SQL_TENANT_ID")
CLIENT_ID     = os.getenv("AZURE_SQL_CLIENT_ID")
CLIENT_SECRET = os.getenv("AZURE_SQL_CLIENT_SECRET")

LOOKBACK_HOURS      = 24
OUTPUT_DIR          = Path(__file__).parent / "logs"
SERVER_RESOURCE_TYPE = "Microsoft.Sql/servers"


def get_subscriptions(credential: ClientSecretCredential) -> list[dict]:
    client = SubscriptionClient(credential)
    return [
        {"id": sub.subscription_id, "name": sub.display_name}
        for sub in client.subscriptions.list()
    ]


def get_sql_servers(credential: ClientSecretCredential, subscription_id: str) -> list:
    if SQL_SDK_AVAILABLE:
        client = SqlManagementClient(credential, subscription_id)
        return list(client.servers.list())
    client = ResourceManagementClient(credential, subscription_id)
    return list(client.resources.list(filter=f"resourceType eq '{SERVER_RESOURCE_TYPE}'"))


def get_server_details(credential: ClientSecretCredential, subscription_id: str, resource_group: str, server_name: str) -> dict:
    if not SQL_SDK_AVAILABLE:
        return {}
    try:
        client = SqlManagementClient(credential, subscription_id)
        server = client.servers.get(resource_group, server_name)
        return {
            "fqdn":                  getattr(server, "fully_qualified_domain_name", ""),
            "administrator_login":   getattr(server, "administrator_login", ""),
            "version":               getattr(server, "version", ""),
            "state":                 getattr(server, "state", ""),
            "public_network_access": str(getattr(server, "public_network_access", "")),
            "minimal_tls_version":   str(getattr(server, "minimal_tls_version", "")),
        }
    except Exception as exc:
        return {"error": str(exc)}


def get_databases(credential: ClientSecretCredential, subscription_id: str, resource_group: str, server_name: str) -> list[dict]:
    if not SQL_SDK_AVAILABLE:
        return []
    try:
        client    = SqlManagementClient(credential, subscription_id)
        databases = client.databases.list_by_server(resource_group, server_name)
        result    = []
        for db in databases:
            sku             = getattr(db, "sku", None)
            create_date     = getattr(db, "creation_date", None)
            max_size_bytes  = getattr(db, "max_size_bytes", 0) or 0
            elastic_pool_id = getattr(db, "elastic_pool_id", "") or ""
            result.append({
                "database_name":              db.name,
                "sku_name":                   getattr(sku, "name", "") if sku else "",
                "sku_tier":                   getattr(sku, "tier", "") if sku else "",
                "sku_capacity":               getattr(sku, "capacity", None) if sku else None,
                "status":                     str(getattr(db, "status", "")),
                "collation":                  getattr(db, "collation", ""),
                "creation_date":              create_date.isoformat() if create_date else "",
                "max_size_gb":                round(max_size_bytes / (1024 ** 3), 2) if max_size_bytes else 0,
                "zone_redundant":             getattr(db, "zone_redundant", False),
                "read_scale":                 str(getattr(db, "read_scale", "")),
                "high_availability_replica_count": getattr(db, "high_availability_replica_count", 0),
                "backup_storage_redundancy":  str(getattr(db, "requested_backup_storage_redundancy", "")),
                "in_elastic_pool":            bool(elastic_pool_id),
                "elastic_pool_id":            elastic_pool_id,
            })
        return result
    except Exception as exc:
        return [{"error": str(exc)}]


def get_activity_logs(monitor_client: MonitorManagementClient, resource_id: str, start_time: datetime) -> list[dict]:
    start_str  = start_time.strftime("%Y-%m-%dT%H:%M:%SZ")
    filter_str = f"eventTimestamp ge '{start_str}' and resourceId eq '{resource_id}'"
    events: list[dict] = []
    try:
        for event in monitor_client.activity_logs.list(
            filter=filter_str,
            select="eventTimestamp,operationName,status,caller,claims,resourceProviderName",
        ):
            claims   = getattr(event, "claims", {}) or {}
            app_id   = claims.get("appid", "") if isinstance(claims, dict) else ""
            caller   = getattr(event, "caller", "") or ""
            op_name  = getattr(event.operation_name, "value", "") if event.operation_name else ""
            status   = getattr(event.status, "value", "") if event.status else ""
            rp_name  = getattr(event.resource_provider_name, "value", "") if event.resource_provider_name else ""
            evt_time = getattr(event, "event_timestamp", None)
            events.append({
                "eventTimestamp":         evt_time.isoformat() if evt_time else "",
                "operationName":          op_name,
                "status":                 status,
                "caller":                 caller,
                "app_id":                 app_id,
                "resource_provider_name": rp_name,
            })
    except Exception as exc:
        events.append({"error": str(exc)})
    return events


def _parse_resource_group(resource_id: str) -> str:
    parts = resource_id.split("/")
    try:
        idx = [p.lower() for p in parts].index("resourcegroups")
        return parts[idx + 1]
    except (ValueError, IndexError):
        return ""


def main() -> None:
    if not all([TENANT_ID, CLIENT_ID, CLIENT_SECRET]):
        print("Missing credentials. Set AZURE_SQL_TENANT_ID / AZURE_SQL_CLIENT_ID / AZURE_SQL_CLIENT_SECRET in .env")
        return

    if not SQL_SDK_AVAILABLE:
        print("Warning: azure-mgmt-sql not installed. Database details will be unavailable.")
        print("  Run: pip install azure-mgmt-sql\n")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    timestamp   = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H-%M-%S")
    output_file = OUTPUT_DIR / f"azuresql_logs_{timestamp}.json"

    end_time   = datetime.now(timezone.utc)
    start_time = end_time - timedelta(hours=LOOKBACK_HOURS)

    credential = ClientSecretCredential(TENANT_ID, CLIENT_ID, CLIENT_SECRET)

    print("Fetching subscriptions...")
    subscriptions = get_subscriptions(credential)
    print(f"  {len(subscriptions)} subscription(s) found")

    all_servers: list[dict] = []

    for sub in subscriptions:
        sub_id   = sub["id"]
        sub_name = sub["name"]
        print(f"\nSubscription: {sub_name} ({sub_id})")

        monitor_client = MonitorManagementClient(credential, sub_id)

        print("  Enumerating SQL servers...")
        try:
            servers = get_sql_servers(credential, sub_id)
        except Exception as exc:
            print(f"  Error enumerating servers: {exc}")
            continue

        print(f"  {len(servers)} server(s) found")

        for server in servers:
            server_id   = getattr(server, "id", "")
            server_name = getattr(server, "name", "")
            location    = getattr(server, "location", "")
            rg          = _parse_resource_group(server_id)

            print(f"    Processing: {server_name}")

            details   = get_server_details(credential, sub_id, rg, server_name)
            databases = get_databases(credential, sub_id, rg, server_name)
            print(f"      {len(databases)} database(s)")

            print(f"      Fetching activity logs...")
            activity_logs = get_activity_logs(monitor_client, server_id, start_time)

            caller_apps:        list[str] = []
            resource_providers: list[str] = []
            for evt in activity_logs:
                if evt.get("app_id"):
                    caller_apps.append(evt["app_id"])
                if evt.get("resource_provider_name"):
                    resource_providers.append(evt["resource_provider_name"])

            all_servers.append({
                "server_name":       server_name,
                "resource_id":       server_id,
                "location":          location,
                "resource_group":    rg,
                "subscription_id":   sub_id,
                "subscription_name": sub_name,
                "details":           details,
                "databases":         databases,
                "activity_logs":     activity_logs,
                "caller_app_ids":    list(set(caller_apps)),
                "resource_providers": list(set(resource_providers)),
            })

    output_data = {
        "collectionTime": datetime.now(timezone.utc).isoformat(),
        "summary": {
            "subscriptions_scanned": len(subscriptions),
            "servers_collected":     len(all_servers),
            "databases_collected":   sum(len(s.get("databases", [])) for s in all_servers),
        },
        "servers": all_servers,
    }

    with output_file.open("w", encoding="utf-8") as fh:
        json.dump(output_data, fh, indent=2, ensure_ascii=False, default=str)

    print(f"\nCompleted.")
    print(f"  Total SQL servers   : {len(all_servers)}")
    print(f"  Total databases     : {output_data['summary']['databases_collected']}")
    print(f"  Output saved to     : {output_file}")

    print("\nGenerating BOM report...")
    generate_bom.main(target_file=output_file)


if __name__ == "__main__":
    main()
