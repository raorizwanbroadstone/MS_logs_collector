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
    from azure.mgmt.cosmosdb import CosmosDBManagementClient
    COSMOSDB_SDK_AVAILABLE = True
except ImportError:
    COSMOSDB_SDK_AVAILABLE = False

load_dotenv()

TENANT_ID     = os.getenv("AZURE_COSMOSDB_TENANT_ID")
CLIENT_ID     = os.getenv("AZURE_COSMOSDB_CLIENT_ID")
CLIENT_SECRET = os.getenv("AZURE_COSMOSDB_CLIENT_SECRET")

LOOKBACK_HOURS = 24
OUTPUT_DIR     = Path(__file__).parent / "logs"
RESOURCE_TYPE  = "Microsoft.DocumentDB/databaseAccounts"


def get_subscriptions(credential: ClientSecretCredential) -> list[dict]:
    client = SubscriptionClient(credential)
    return [
        {"id": sub.subscription_id, "name": sub.display_name}
        for sub in client.subscriptions.list()
    ]


def get_cosmosdb_accounts(credential: ClientSecretCredential, subscription_id: str) -> list[dict]:
    client = ResourceManagementClient(credential, subscription_id)
    return list(client.resources.list(filter=f"resourceType eq '{RESOURCE_TYPE}'"))


def get_account_details(credential: ClientSecretCredential, subscription_id: str, resource_group: str, account_name: str) -> dict:
    if not COSMOSDB_SDK_AVAILABLE:
        return {}
    try:
        client  = CosmosDBManagementClient(credential, subscription_id)
        account = client.database_accounts.get(resource_group, account_name)
        props   = account.properties if hasattr(account, "properties") else account

        locations = []
        if hasattr(account, "locations") and account.locations:
            locations = [loc.location_name for loc in account.locations if hasattr(loc, "location_name")]
        elif hasattr(props, "locations") and props.locations:
            locations = [loc.location_name for loc in props.locations if hasattr(loc, "location_name")]

        kind     = getattr(account, "kind", "")
        api_type = _resolve_api_type(account)

        consistency_policy = {}
        cp = getattr(account, "consistency_policy", None) or getattr(props, "consistency_policy", None)
        if cp:
            consistency_policy = {
                "level":               getattr(cp, "default_consistency_level", ""),
                "max_staleness_prefix": getattr(cp, "max_staleness_prefix", None),
                "max_interval_in_seconds": getattr(cp, "max_interval_in_seconds", None),
            }

        backup_policy_type = ""
        bp = getattr(account, "backup_policy", None) or getattr(props, "backup_policy", None)
        if bp:
            backup_policy_type = type(bp).__name__

        databases = _list_databases(client, resource_group, account_name, api_type)

        return {
            "kind":                     kind,
            "api_type":                 api_type,
            "consistency_policy":       consistency_policy,
            "locations":                locations,
            "backup_policy_type":       backup_policy_type,
            "public_network_access":    str(getattr(account, "public_network_access", "") or getattr(props, "public_network_access", "")),
            "enable_free_tier":         getattr(account, "enable_free_tier", False) or getattr(props, "enable_free_tier", False),
            "enable_automatic_failover": getattr(account, "enable_automatic_failover", False) or getattr(props, "enable_automatic_failover", False),
            "document_endpoint":        getattr(account, "document_endpoint", "") or getattr(props, "document_endpoint", ""),
            "provisioning_state":       getattr(account, "provisioning_state", "") or getattr(props, "provisioning_state", ""),
            "databases":                databases,
        }
    except Exception as exc:
        return {"error": str(exc)}


def _resolve_api_type(account) -> str:
    capabilities = getattr(account, "capabilities", []) or []
    cap_names    = {getattr(c, "name", "") for c in capabilities}
    if "EnableMongo" in cap_names:
        return "MongoDB"
    if "EnableCassandra" in cap_names:
        return "Cassandra"
    if "EnableGremlin" in cap_names:
        return "Gremlin"
    if "EnableTable" in cap_names:
        return "Table"
    return "SQL"


def _list_databases(client, resource_group: str, account_name: str, api_type: str) -> list[str]:
    try:
        if api_type == "SQL":
            dbs = client.sql_resources.list_sql_databases(resource_group, account_name)
            return [db.name for db in dbs]
        if api_type == "MongoDB":
            dbs = client.mongo_db_resources.list_mongo_db_databases(resource_group, account_name)
            return [db.name for db in dbs]
        if api_type == "Cassandra":
            dbs = client.cassandra_resources.list_cassandra_keyspaces(resource_group, account_name)
            return [db.name for db in dbs]
        if api_type == "Gremlin":
            dbs = client.gremlin_resources.list_gremlin_databases(resource_group, account_name)
            return [db.name for db in dbs]
        if api_type == "Table":
            dbs = client.table_resources.list_tables(resource_group, account_name)
            return [db.name for db in dbs]
    except Exception:
        pass
    return []


def get_activity_logs(monitor_client: MonitorManagementClient, resource_id: str, start_time: datetime) -> list[dict]:
    start_str = start_time.strftime("%Y-%m-%dT%H:%M:%SZ")
    filter_str = f"eventTimestamp ge '{start_str}' and resourceId eq '{resource_id}'"
    events: list[dict] = []
    try:
        for event in monitor_client.activity_logs.list(filter=filter_str, select="eventTimestamp,operationName,status,caller,claims,resourceProviderName"):
            claims   = getattr(event, "claims", {}) or {}
            app_id   = claims.get("appid", "") if isinstance(claims, dict) else ""
            caller   = getattr(event, "caller", "") or ""
            op_name  = getattr(event.operation_name, "value", "") if event.operation_name else ""
            status   = getattr(event.status, "value", "") if event.status else ""
            rp_name  = getattr(event.resource_provider_name, "value", "") if event.resource_provider_name else ""
            evt_time = getattr(event, "event_timestamp", None)
            events.append({
                "eventTimestamp":        evt_time.isoformat() if evt_time else "",
                "operationName":         op_name,
                "status":                status,
                "caller":                caller,
                "app_id":                app_id,
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
        print("Missing credentials. Set AZURE_COSMOSDB_TENANT_ID / AZURE_COSMOSDB_CLIENT_ID / AZURE_COSMOSDB_CLIENT_SECRET in .env")
        return

    if not COSMOSDB_SDK_AVAILABLE:
        print("Warning: azure-mgmt-cosmosdb not installed. Account details will be limited.")
        print("  Run: pip install azure-mgmt-cosmosdb\n")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    timestamp   = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H-%M-%S")
    output_file = OUTPUT_DIR / f"cosmosdb_logs_{timestamp}.json"

    end_time   = datetime.now(timezone.utc)
    start_time = end_time - timedelta(hours=LOOKBACK_HOURS)

    credential = ClientSecretCredential(TENANT_ID, CLIENT_ID, CLIENT_SECRET)

    print("Fetching subscriptions...")
    subscriptions = get_subscriptions(credential)
    print(f"  {len(subscriptions)} subscription(s) found")

    all_accounts: list[dict] = []

    for sub in subscriptions:
        sub_id   = sub["id"]
        sub_name = sub["name"]
        print(f"\nSubscription: {sub_name} ({sub_id})")

        monitor_client = MonitorManagementClient(credential, sub_id)

        print("  Enumerating Cosmos DB accounts...")
        try:
            accounts = get_cosmosdb_accounts(credential, sub_id)
        except Exception as exc:
            print(f"  Error enumerating accounts: {exc}")
            continue

        print(f"  {len(accounts)} account(s) found")

        for acct in accounts:
            acct_id   = acct.id
            acct_name = acct.name
            location  = getattr(acct, "location", "")
            rg        = _parse_resource_group(acct_id)

            print(f"    Processing: {acct_name}")

            details = get_account_details(credential, sub_id, rg, acct_name)

            print(f"      Fetching activity logs...")
            activity_logs = get_activity_logs(monitor_client, acct_id, start_time)

            caller_apps: list[str] = []
            resource_providers: list[str] = []
            for evt in activity_logs:
                if evt.get("app_id"):
                    caller_apps.append(evt["app_id"])
                if evt.get("resource_provider_name"):
                    resource_providers.append(evt["resource_provider_name"])

            all_accounts.append({
                "account_name":      acct_name,
                "resource_id":       acct_id,
                "location":          location,
                "resource_group":    rg,
                "subscription_id":   sub_id,
                "subscription_name": sub_name,
                "details":           details,
                "activity_logs":     activity_logs,
                "caller_app_ids":    list(set(caller_apps)),
                "resource_providers": list(set(resource_providers)),
            })

    output_data = {
        "collectionTime": datetime.now(timezone.utc).isoformat(),
        "summary": {
            "subscriptions_scanned": len(subscriptions),
            "accounts_collected":    len(all_accounts),
        },
        "accounts": all_accounts,
    }

    with output_file.open("w", encoding="utf-8") as fh:
        json.dump(output_data, fh, indent=2, ensure_ascii=False, default=str)

    print(f"\nCompleted.")
    print(f"  Total Cosmos DB accounts: {len(all_accounts)}")
    print(f"  Output saved to:          {output_file}")

    print("\nGenerating BOM report...")
    generate_bom.main(target_file=output_file)


if __name__ == "__main__":
    main()
