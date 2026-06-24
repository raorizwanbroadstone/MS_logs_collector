import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import boto3
from dotenv import load_dotenv
import generate_bom

load_dotenv(Path(__file__).parent.parent.parent / ".env")

AWS_KEY_ID     = os.getenv("AWS_RDS_ACCESS_KEY_ID", "")
AWS_SECRET_KEY = os.getenv("AWS_RDS_SECRET_ACCESS_KEY", "")
REGION         = os.getenv("AWS_DEFAULT_REGION", "us-east-1")

LOOKBACK_HOURS = 24
EVENT_SOURCE   = "rds.amazonaws.com"
OUTPUT_DIR     = Path(__file__).parent / "logs"

RESOURCE_PARAM_KEYS = {
    "dBInstanceIdentifier": "DBInstance",
    "dBClusterIdentifier":  "DBCluster",
    "dBSnapshotIdentifier": "DBSnapshot",
    "dBSubnetGroupName":    "DBSubnetGroup",
    "dBParameterGroupName": "DBParameterGroup",
}

rds_client = boto3.client(
    "rds",
    aws_access_key_id=AWS_KEY_ID,
    aws_secret_access_key=AWS_SECRET_KEY,
    region_name=REGION,
)
cloudtrail_client = boto3.client(
    "cloudtrail",
    aws_access_key_id=AWS_KEY_ID,
    aws_secret_access_key=AWS_SECRET_KEY,
    region_name=REGION,
)


def fetch_events_for_source(source, start_time, end_time):
    events = []
    kwargs = {
        "LookupAttributes": [{"AttributeKey": "EventSource", "AttributeValue": source}],
        "StartTime": start_time,
        "EndTime":   end_time,
        "MaxResults": 50,
    }
    while True:
        resp = cloudtrail_client.lookup_events(**kwargs)
        events.extend(resp.get("Events", []))
        next_token = resp.get("NextToken")
        if not next_token:
            break
        kwargs["NextToken"] = next_token
    return events


def normalize_event(raw):
    event = dict(raw)
    event["EventTime"]          = raw["EventTime"].isoformat()
    ct_event                    = json.loads(raw.get("CloudTrailEvent", "{}"))
    event["requestParameters"]  = ct_event.get("requestParameters", {}) or {}
    event["responseElements"]   = ct_event.get("responseElements", {}) or {}
    event["userIdentity"]       = ct_event.get("userIdentity", {}) or {}
    event["sourceIPAddress"]    = ct_event.get("sourceIPAddress", "")
    event["awsRegion"]          = ct_event.get("awsRegion", "")
    return event


def extract_unique_resources(events):
    seen      = set()
    resources = []
    for event in events:
        params = event.get("requestParameters", {}) or {}
        for param_key, resource_type in RESOURCE_PARAM_KEYS.items():
            name = params.get(param_key)
            if name and (resource_type, name) not in seen:
                seen.add((resource_type, name))
                resources.append({"resource_type": resource_type, "resource_name": name})
    return resources


def enumerate_all_resources():
    resources = []
    seen      = set()

    paginator = rds_client.get_paginator("describe_db_instances")
    for page in paginator.paginate():
        for inst in page.get("DBInstances", []):
            name = inst["DBInstanceIdentifier"]
            if ("DBInstance", name) not in seen:
                seen.add(("DBInstance", name))
                resources.append({"resource_type": "DBInstance", "resource_name": name})

    try:
        paginator = rds_client.get_paginator("describe_db_clusters")
        for page in paginator.paginate():
            for cluster in page.get("DBClusters", []):
                name = cluster["DBClusterIdentifier"]
                if ("DBCluster", name) not in seen:
                    seen.add(("DBCluster", name))
                    resources.append({"resource_type": "DBCluster", "resource_name": name})
    except Exception:
        pass

    return resources


def describe_resource(resource_type, resource_name):
    try:
        if resource_type == "DBInstance":
            resp     = rds_client.describe_db_instances(DBInstanceIdentifier=resource_name)
            inst     = resp["DBInstances"][0]
            endpoint = inst.get("Endpoint", {}) or {}
            kms_key  = inst.get("KmsKeyId", "")
            if kms_key:
                kms_key = kms_key.split("/")[-1]
            subnet_group = inst.get("DBSubnetGroup", {}) or {}
            create_time  = inst.get("InstanceCreateTime")
            return {
                "DBInstanceIdentifier":             resource_name,
                "DBInstanceArn":                    inst.get("DBInstanceArn", ""),
                "Engine":                           inst.get("Engine", ""),
                "EngineVersion":                    inst.get("EngineVersion", ""),
                "DBInstanceClass":                  inst.get("DBInstanceClass", ""),
                "DBInstanceStatus":                 inst.get("DBInstanceStatus", ""),
                "MultiAZ":                          inst.get("MultiAZ", False),
                "StorageEncrypted":                 inst.get("StorageEncrypted", False),
                "KMSKeyId":                         kms_key,
                "StorageType":                      inst.get("StorageType", ""),
                "AllocatedStorageGB":               inst.get("AllocatedStorage", 0),
                "MaxAllocatedStorageGB":            inst.get("MaxAllocatedStorage", 0),
                "DeletionProtection":               inst.get("DeletionProtection", False),
                "PubliclyAccessible":               inst.get("PubliclyAccessible", False),
                "IAMDatabaseAuthenticationEnabled": inst.get("IAMDatabaseAuthenticationEnabled", False),
                "Endpoint":                         f"{endpoint.get('Address', '')}:{endpoint.get('Port', '')}",
                "AvailabilityZone":                 inst.get("AvailabilityZone", ""),
                "DBSubnetGroup":                    subnet_group.get("DBSubnetGroupName", ""),
                "VpcId":                            subnet_group.get("VpcId", ""),
                "DBName":                           inst.get("DBName", ""),
                "MasterUsername":                   inst.get("MasterUsername", ""),
                "CACertificateIdentifier":          inst.get("CACertificateIdentifier", ""),
                "BackupRetentionPeriodDays":        inst.get("BackupRetentionPeriod", 0),
                "AutoMinorVersionUpgrade":          inst.get("AutoMinorVersionUpgrade", False),
                "InstanceCreateTime":               create_time.isoformat() if create_time else "",
                "InventoryStatus":                  "Found",
            }

        if resource_type == "DBCluster":
            resp    = rds_client.describe_db_clusters(DBClusterIdentifier=resource_name)
            cluster = resp["DBClusters"][0]
            kms_key = cluster.get("KmsKeyId", "")
            if kms_key:
                kms_key = kms_key.split("/")[-1]
            create_time = cluster.get("ClusterCreateTime")
            return {
                "DBClusterIdentifier":              resource_name,
                "DBClusterArn":                     cluster.get("DBClusterArn", ""),
                "Engine":                           cluster.get("Engine", ""),
                "EngineVersion":                    cluster.get("EngineVersion", ""),
                "EngineMode":                       cluster.get("EngineMode", ""),
                "Status":                           cluster.get("Status", ""),
                "MultiAZ":                          cluster.get("MultiAZ", False),
                "StorageEncrypted":                 cluster.get("StorageEncrypted", False),
                "KMSKeyId":                         kms_key,
                "DeletionProtection":               cluster.get("DeletionProtection", False),
                "Endpoint":                         cluster.get("Endpoint", ""),
                "ReaderEndpoint":                   cluster.get("ReaderEndpoint", ""),
                "ClusterMemberCount":               len(cluster.get("DBClusterMembers", [])),
                "AvailabilityZones":                cluster.get("AvailabilityZones", []),
                "DBSubnetGroup":                    cluster.get("DBSubnetGroup", ""),
                "MasterUsername":                   cluster.get("MasterUsername", ""),
                "BackupRetentionPeriodDays":        cluster.get("BackupRetentionPeriod", 0),
                "IAMDatabaseAuthenticationEnabled": cluster.get("IAMDatabaseAuthenticationEnabled", False),
                "ClusterCreateTime":                create_time.isoformat() if create_time else "",
                "InventoryStatus":                  "Found",
            }

    except Exception as exc:
        return {"InventoryStatus": "NotFound", "Error": str(exc)}

    return {"InventoryStatus": "NotFound"}


def build_resource_inventory_event(resource, event_time):
    rtype    = resource["resource_type"]
    rname    = resource["resource_name"]
    param_key = "dBInstanceIdentifier" if rtype == "DBInstance" else "dBClusterIdentifier"
    return {
        "EventId":     f"rds-inventory-{rname}",
        "EventName":   "RDSResourceInventory",
        "EventSource": "rds-local-enumeration",
        "EventTime":   event_time,
        "Resources":   [{"ResourceName": rname, "ResourceType": f"AWS::RDS::{rtype}"}],
        "requestParameters": {param_key: rname},
        "userIdentity": {},
        "inventory":   resource.get("inventory", {}),
    }


def main():
    if not AWS_KEY_ID or not AWS_SECRET_KEY:
        print("Missing credentials. Set AWS_RDS_ACCESS_KEY_ID and AWS_RDS_SECRET_ACCESS_KEY in .env")
        return

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    timestamp   = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H-%M-%S")
    output_file = OUTPUT_DIR / f"rds_logs_{timestamp}.json"

    end_time   = datetime.now(timezone.utc)
    start_time = end_time - timedelta(hours=LOOKBACK_HOURS)

    print(f"Region : {REGION}")
    print(f"Window : {start_time.strftime('%Y-%m-%dT%H:%M:%SZ')} -> {end_time.strftime('%Y-%m-%dT%H:%M:%SZ')}\n")

    print("Verifying RDS access...")
    rds_client.describe_db_instances(MaxRecords=20)
    print("  Connected.\n")

    print(f"Fetching CloudTrail events for {EVENT_SOURCE}...")
    raw_events = fetch_events_for_source(EVENT_SOURCE, start_time, end_time)
    print(f"  {len(raw_events)} events fetched")
    events = [normalize_event(e) for e in raw_events]

    cloudtrail_resources = extract_unique_resources(events)
    print(f"  {len(cloudtrail_resources)} unique resources referenced in CloudTrail")

    print("\nEnumerating all current RDS resources...")
    enumerated = enumerate_all_resources()
    print(f"  {len(enumerated)} resources enumerated ({sum(1 for r in enumerated if r['resource_type']=='DBInstance')} instances, {sum(1 for r in enumerated if r['resource_type']=='DBCluster')} clusters)")

    merged: dict[tuple, dict] = {}
    for r in enumerated:
        merged[(r["resource_type"], r["resource_name"])] = r
    for r in cloudtrail_resources:
        key = (r["resource_type"], r["resource_name"])
        if key not in merged:
            merged[key] = r
    all_resources = list(merged.values())
    print(f"  {len(all_resources)} total after merge with CloudTrail references\n")

    event_time       = datetime.now(timezone.utc).isoformat()
    inventory_events = []
    for resource in all_resources:
        if resource["resource_type"] not in ("DBInstance", "DBCluster"):
            continue
        print(f"  Describing {resource['resource_type']}: {resource['resource_name']}")
        resource["inventory"] = describe_resource(resource["resource_type"], resource["resource_name"])
        inventory_events.append(build_resource_inventory_event(resource, event_time))

    all_output = events + inventory_events
    with output_file.open("w", encoding="utf-8") as fh:
        json.dump(all_output, fh, indent=2, ensure_ascii=False, default=str)

    print(f"\nCompleted.")
    print(f"  CloudTrail events  : {len(events)}")
    print(f"  Inventory events   : {len(inventory_events)}")
    print(f"  Total written      : {len(all_output)}")
    print(f"  Output saved to    : {output_file}")

    print("\nGenerating BOM report...")
    generate_bom.main(target_file=output_file)


if __name__ == "__main__":
    main()
