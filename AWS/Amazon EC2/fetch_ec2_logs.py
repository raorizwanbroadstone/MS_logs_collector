import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import boto3
from dotenv import load_dotenv
import generate_bom

load_dotenv(Path(__file__).parent.parent.parent / ".env")

AWS_REGION     = os.getenv("AWS_DEFAULT_REGION", "us-east-1")
AWS_KEY_ID     = os.getenv("AWS_EC2_ACCESS_KEY_ID", "")
AWS_SECRET_KEY = os.getenv("AWS_EC2_SECRET_ACCESS_KEY", "")

LOOKBACK_HOURS = 24
OUTPUT_DIR     = Path(__file__).parent / "logs"

EC2_EVENT_SOURCES = ["ec2.amazonaws.com"]

RESOURCE_PARAM_KEYS = {
    "instanceId":  "Instance",
    "groupId":     "SecurityGroup",
    "keyName":     "KeyPair",
    "imageId":     "Image",
    "volumeId":    "Volume",
    "snapshotId":  "Snapshot",
}


def _ec2_client():
    return boto3.client(
        "ec2",
        region_name=AWS_REGION,
        aws_access_key_id=AWS_KEY_ID,
        aws_secret_access_key=AWS_SECRET_KEY,
    )


def _cloudtrail_client():
    return boto3.client(
        "cloudtrail",
        region_name=AWS_REGION,
        aws_access_key_id=AWS_KEY_ID,
        aws_secret_access_key=AWS_SECRET_KEY,
    )


def check_ec2_availability() -> bool:
    try:
        _ec2_client().describe_instances(MaxResults=5)
        print("  EC2 is reachable")
        return True
    except Exception as exc:
        msg = str(exc)
        if any(kw in msg.lower() for kw in ("could not connect", "connection refused", "nodename")):
            print(f"  EC2 connectivity issue: {type(exc).__name__}")
            return False
        print(f"  EC2 endpoint reachable ({type(exc).__name__}: {msg[:120]})")
        return True


def fetch_events_for_source(source: str, start_time: datetime, end_time: datetime) -> list[dict]:
    client = _cloudtrail_client()
    events: list[dict] = []
    kwargs: dict = dict(
        LookupAttributes=[{"AttributeKey": "EventSource", "AttributeValue": source}],
        StartTime=start_time,
        EndTime=end_time,
        MaxResults=50,
    )
    page = 0
    while True:
        resp  = client.lookup_events(**kwargs)
        batch = resp.get("Events", [])
        events.extend(batch)
        page += 1
        print(f"    Page {page}: {len(batch)} events")
        token = resp.get("NextToken")
        if not token:
            break
        kwargs["NextToken"] = token
    return events


def normalize_event(raw: dict) -> dict:
    event_time = raw.get("EventTime", datetime.now(timezone.utc))
    out: dict = {
        "EventId":     raw.get("EventId", ""),
        "EventName":   raw.get("EventName", ""),
        "EventTime":   event_time.isoformat() if isinstance(event_time, datetime) else str(event_time),
        "EventSource": raw.get("EventSource", ""),
        "Username":    raw.get("Username", ""),
        "Resources":   raw.get("Resources", []),
    }
    try:
        ct: dict = json.loads(raw.get("CloudTrailEvent", "{}"))
        out["userIdentity"]      = ct.get("userIdentity") or {}
        out["requestParameters"] = ct.get("requestParameters") or {}
        out["responseElements"]  = ct.get("responseElements") or {}
        out["awsRegion"]         = ct.get("awsRegion", "")
        out["sourceIPAddress"]   = ct.get("sourceIPAddress", "")
        out["errorCode"]         = ct.get("errorCode", "")
        out["errorMessage"]      = ct.get("errorMessage", "")
    except (json.JSONDecodeError, TypeError):
        pass
    return out


def extract_unique_resources(events: list[dict]) -> list[dict]:
    seen: set[tuple[str, str]] = set()
    resources: list[dict] = []

    for event in events:
        params = event.get("requestParameters") or {}
        if not isinstance(params, dict):
            continue

        for param_key, resource_type in RESOURCE_PARAM_KEYS.items():
            name = params.get(param_key)
            if name and isinstance(name, str):
                key = (resource_type, name)
                if key not in seen:
                    seen.add(key)
                    resources.append({"resource_type": resource_type, "resource_name": name})

        # StartInstances, StopInstances, TerminateInstances, RebootInstances use instancesSet
        items_set = params.get("instancesSet") or {}
        for item in (items_set.get("items") or []):
            if isinstance(item, dict):
                iid = item.get("instanceId", "")
                if iid and ("Instance", iid) not in seen:
                    seen.add(("Instance", iid))
                    resources.append({"resource_type": "Instance", "resource_name": iid})

    return resources


def describe_instance(client, instance_id: str) -> dict:
    result = {
        "resource_type":        "Instance",
        "resource_name":        instance_id,
        "arn":                  "",
        "instance_type":        "",
        "state":                "",
        "ami_id":               "",
        "key_name":             "",
        "vpc_id":               "",
        "subnet_id":            "",
        "private_ip":           "",
        "public_ip":            "",
        "platform":             "",
        "architecture":         "",
        "monitoring":           "",
        "launch_time":          "",
        "iam_instance_profile": "",
        "security_groups":      [],
        "volumes":              [],
        "access_denied":        False,
        "not_found":            False,
    }
    try:
        resp         = client.describe_instances(InstanceIds=[instance_id])
        reservations = resp.get("Reservations") or []
        if not reservations:
            result["not_found"] = True
            return result
        reservation = reservations[0]
        owner_id    = reservation.get("OwnerId", "")
        instances   = reservation.get("Instances") or []
        if not instances:
            result["not_found"] = True
            return result
        instance = instances[0]

        result["arn"]           = f"arn:aws:ec2:{AWS_REGION}:{owner_id}:instance/{instance_id}"
        result["instance_type"] = instance.get("InstanceType", "")
        result["state"]         = (instance.get("State") or {}).get("Name", "")
        result["ami_id"]        = instance.get("ImageId", "")
        result["key_name"]      = instance.get("KeyName", "")
        result["vpc_id"]        = instance.get("VpcId", "")
        result["subnet_id"]     = instance.get("SubnetId", "")
        result["private_ip"]    = instance.get("PrivateIpAddress", "")
        result["public_ip"]     = instance.get("PublicIpAddress", "")
        result["platform"]      = instance.get("Platform", "linux")
        result["architecture"]  = instance.get("Architecture", "")
        result["monitoring"]    = (instance.get("Monitoring") or {}).get("State", "")
        lt = instance.get("LaunchTime")
        result["launch_time"]   = lt.isoformat() if lt else ""
        profile = instance.get("IamInstanceProfile") or {}
        result["iam_instance_profile"] = profile.get("Arn", "")
        result["security_groups"] = [
            sg.get("GroupId", "") for sg in (instance.get("SecurityGroups") or []) if sg.get("GroupId")
        ]
        result["volumes"] = [
            m.get("Ebs", {}).get("VolumeId", "")
            for m in (instance.get("BlockDeviceMappings") or [])
            if m.get("Ebs") and m["Ebs"].get("VolumeId")
        ]
    except Exception as exc:
        msg = str(exc)
        if "UnauthorizedOperation" in msg or "AccessDenied" in msg:
            result["access_denied"] = True
            print(f"    Cannot describe Instance {instance_id}: {type(exc).__name__}")
        elif "InvalidInstanceID.NotFound" in msg:
            result["not_found"] = True
            print(f"    Not found: Instance/{instance_id}")
        else:
            print(f"    Error describing Instance/{instance_id}: {exc}")
    return result


def describe_security_group(client, group_id: str) -> dict:
    result = {
        "resource_type":      "SecurityGroup",
        "resource_name":      group_id,
        "arn":                "",
        "group_name":         "",
        "description":        "",
        "vpc_id":             "",
        "ingress_rule_count": 0,
        "egress_rule_count":  0,
        "access_denied":      False,
        "not_found":          False,
    }
    try:
        resp   = client.describe_security_groups(GroupIds=[group_id])
        groups = resp.get("SecurityGroups") or []
        if not groups:
            result["not_found"] = True
            return result
        sg       = groups[0]
        owner_id = sg.get("OwnerId", "")
        result["arn"]                = f"arn:aws:ec2:{AWS_REGION}:{owner_id}:security-group/{group_id}"
        result["group_name"]         = sg.get("GroupName", "")
        result["description"]        = sg.get("Description", "")
        result["vpc_id"]             = sg.get("VpcId", "")
        result["ingress_rule_count"] = len(sg.get("IpPermissions") or [])
        result["egress_rule_count"]  = len(sg.get("IpPermissionsEgress") or [])
    except Exception as exc:
        msg = str(exc)
        if "UnauthorizedOperation" in msg or "AccessDenied" in msg:
            result["access_denied"] = True
            print(f"    Cannot describe SecurityGroup {group_id}: {type(exc).__name__}")
        elif "InvalidGroup.NotFound" in msg:
            result["not_found"] = True
            print(f"    Not found: SecurityGroup/{group_id}")
        else:
            print(f"    Error describing SecurityGroup/{group_id}: {exc}")
    return result


def describe_key_pair(client, key_name: str) -> dict:
    result = {
        "resource_type": "KeyPair",
        "resource_name": key_name,
        "key_pair_id":   "",
        "key_type":      "",
        "create_time":   "",
        "access_denied": False,
        "not_found":     False,
    }
    try:
        resp  = client.describe_key_pairs(KeyNames=[key_name])
        pairs = resp.get("KeyPairs") or []
        if not pairs:
            result["not_found"] = True
            return result
        kp = pairs[0]
        result["key_pair_id"] = kp.get("KeyPairId", "")
        result["key_type"]    = kp.get("KeyType", "")
        ct = kp.get("CreateTime")
        result["create_time"] = ct.isoformat() if ct else ""
    except Exception as exc:
        msg = str(exc)
        if "UnauthorizedOperation" in msg or "AccessDenied" in msg:
            result["access_denied"] = True
            print(f"    Cannot describe KeyPair {key_name}: {type(exc).__name__}")
        elif "InvalidKeyPair.NotFound" in msg:
            result["not_found"] = True
            print(f"    Not found: KeyPair/{key_name}")
        else:
            print(f"    Error describing KeyPair/{key_name}: {exc}")
    return result


def describe_volume(client, volume_id: str) -> dict:
    result = {
        "resource_type":     "Volume",
        "resource_name":     volume_id,
        "volume_type":       "",
        "size_gb":           0,
        "state":             "",
        "availability_zone": "",
        "encrypted":         False,
        "iops":              0,
        "throughput":        0,
        "create_time":       "",
        "attached_to":       [],
        "access_denied":     False,
        "not_found":         False,
    }
    try:
        resp    = client.describe_volumes(VolumeIds=[volume_id])
        volumes = resp.get("Volumes") or []
        if not volumes:
            result["not_found"] = True
            return result
        vol = volumes[0]
        result["volume_type"]       = vol.get("VolumeType", "")
        result["size_gb"]           = vol.get("Size", 0)
        result["state"]             = vol.get("State", "")
        result["availability_zone"] = vol.get("AvailabilityZone", "")
        result["encrypted"]         = vol.get("Encrypted", False)
        result["iops"]              = vol.get("Iops", 0)
        result["throughput"]        = vol.get("Throughput", 0)
        ct = vol.get("CreateTime")
        result["create_time"] = ct.isoformat() if ct else ""
        result["attached_to"] = [
            a.get("InstanceId", "") for a in (vol.get("Attachments") or []) if a.get("InstanceId")
        ]
    except Exception as exc:
        msg = str(exc)
        if "UnauthorizedOperation" in msg or "AccessDenied" in msg:
            result["access_denied"] = True
            print(f"    Cannot describe Volume {volume_id}: {type(exc).__name__}")
        elif "InvalidVolume.NotFound" in msg:
            result["not_found"] = True
            print(f"    Not found: Volume/{volume_id}")
        else:
            print(f"    Error describing Volume/{volume_id}: {exc}")
    return result


def describe_resource(resource_type: str, resource_name: str) -> dict:
    client = _ec2_client()
    if resource_type == "Instance":
        return describe_instance(client, resource_name)
    if resource_type == "SecurityGroup":
        return describe_security_group(client, resource_name)
    if resource_type == "KeyPair":
        return describe_key_pair(client, resource_name)
    if resource_type == "Volume":
        return describe_volume(client, resource_name)
    return {"resource_type": resource_type, "resource_name": resource_name,
            "access_denied": False, "not_found": False}


def enumerate_all_resources() -> list[dict]:
    client    = _ec2_client()
    resources: list[dict] = []

    try:
        paginator = client.get_paginator("describe_instances")
        count = 0
        for page in paginator.paginate():
            for reservation in (page.get("Reservations") or []):
                for instance in (reservation.get("Instances") or []):
                    iid = instance.get("InstanceId", "")
                    if iid:
                        resources.append({"resource_type": "Instance", "resource_name": iid})
                        count += 1
        print(f"  {count} instances")
    except Exception as exc:
        print(f"  Error enumerating instances: {exc}")

    try:
        paginator = client.get_paginator("describe_security_groups")
        count = 0
        for page in paginator.paginate():
            for sg in (page.get("SecurityGroups") or []):
                gid = sg.get("GroupId", "")
                if gid:
                    resources.append({"resource_type": "SecurityGroup", "resource_name": gid})
                    count += 1
        print(f"  {count} security groups")
    except Exception as exc:
        print(f"  Error enumerating security groups: {exc}")

    try:
        resp  = client.describe_key_pairs()
        pairs = resp.get("KeyPairs") or []
        for kp in pairs:
            name = kp.get("KeyName", "")
            if name:
                resources.append({"resource_type": "KeyPair", "resource_name": name})
        print(f"  {len(pairs)} key pairs")
    except Exception as exc:
        print(f"  Error enumerating key pairs: {exc}")

    try:
        paginator = client.get_paginator("describe_volumes")
        count = 0
        for page in paginator.paginate():
            for vol in (page.get("Volumes") or []):
                vid = vol.get("VolumeId", "")
                if vid:
                    resources.append({"resource_type": "Volume", "resource_name": vid})
                    count += 1
        print(f"  {count} volumes")
    except Exception as exc:
        print(f"  Error enumerating volumes: {exc}")

    return resources


def build_inventory_event(resource: dict, event_time: datetime) -> dict:
    resource_type = resource["resource_type"]
    resource_name = resource["resource_name"]
    return {
        "EventId":           f"inventory-{resource_type}-{resource_name}",
        "EventName":         "EC2ResourceInventory",
        "EventSource":       "ec2-local-enumeration",
        "EventTime":         event_time.isoformat(),
        "Username":          "",
        "Resources":         [],
        "userIdentity":      {},
        "requestParameters": {"resourceType": resource_type, "resourceName": resource_name},
        "responseElements":  {},
        "awsRegion":         AWS_REGION,
        "sourceIPAddress":   "",
        "errorCode":         "AccessDenied" if resource.get("access_denied") else "",
        "errorMessage":      "",
        "inventory":         resource,
    }


def main() -> None:
    if not all([AWS_KEY_ID, AWS_SECRET_KEY]):
        print("Missing credentials. Set AWS_EC2_ACCESS_KEY_ID / AWS_EC2_SECRET_ACCESS_KEY in .env")
        return

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    timestamp   = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H-%M-%S")
    output_file = OUTPUT_DIR / f"ec2_logs_{timestamp}.json"

    end_time   = datetime.now(timezone.utc)
    start_time = end_time - timedelta(hours=LOOKBACK_HOURS)

    print(f"Region : {AWS_REGION}")
    print(f"Window : {start_time.strftime('%Y-%m-%dT%H:%M:%SZ')} -> {end_time.strftime('%Y-%m-%dT%H:%M:%SZ')}\n")

    print("Checking EC2 availability...")
    if not check_ec2_availability():
        return
    print()

    all_events: list[dict] = []
    for source in EC2_EVENT_SOURCES:
        print(f"Fetching CloudTrail events: {source}")
        try:
            raw_events = fetch_events_for_source(source, start_time, end_time)
            normalised = [normalize_event(e) for e in raw_events]
            all_events.extend(normalised)
            print(f"  {len(normalised)} events from {source}\n")
        except Exception as exc:
            print(f"  Error fetching {source}: {exc}\n")

    print("Enumerating current EC2 resources...")
    enumerated = enumerate_all_resources()

    ct_resources = extract_unique_resources(all_events)
    seen: set[tuple[str, str]] = {(r["resource_type"], r["resource_name"]) for r in enumerated}
    for r in ct_resources:
        key = (r["resource_type"], r["resource_name"])
        if key not in seen:
            seen.add(key)
            enumerated.append(r)

    print(f"\nDescribing {len(enumerated)} unique EC2 resources...")
    for resource_ref in enumerated:
        rtype = resource_ref["resource_type"]
        rname = resource_ref["resource_name"]
        print(f"  -> {rtype}/{rname}")
        details = describe_resource(rtype, rname)
        if not details.get("access_denied") and not details.get("not_found"):
            if rtype == "Instance":
                print(f"    state={details.get('state')}, type={details.get('instance_type')}")
            elif rtype == "SecurityGroup":
                print(f"    name={details.get('group_name')}, vpc={details.get('vpc_id')}")
            elif rtype == "Volume":
                print(f"    {details.get('size_gb')}GB {details.get('volume_type')}, state={details.get('state')}")
        all_events.append(build_inventory_event(details, end_time))

    with output_file.open("w", encoding="utf-8") as fh:
        json.dump(all_events, fh, indent=2, ensure_ascii=False)

    print(f"\nCompleted.")
    print(f"  Total events collected: {len(all_events)}")
    print(f"  Output saved to:        {output_file}")

    print("\nGenerating BOM report...")
    generate_bom.main(target_file=output_file)


if __name__ == "__main__":
    main()