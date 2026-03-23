import boto3
import json
import re
import os

# Lambda environment variables
INSTANCE_ID = os.environ["INSTANCE_ID"]
REGION = os.environ["REGION"]
S3_BUCKET = os.environ["S3_BUCKET"]
S3_KEY = os.environ["S3_KEY"]  # e.g. connect-export/hours_template.json

connect = boto3.client("connect", region_name=REGION)
s3 = boto3.client("s3")


def sanitize_logical_id(name: str, used: set) -> str:
    """
    Convert Connect Hours name into a valid CloudFormation Logical ID:
    - Only letters and digits
    - Must start with a letter
    - Unique within template
    """
    base = re.sub(r"[^A-Za-z0-9]+", " ", name).title().replace(" ", "")

    if not base:
        base = "Hours"

    if not re.match(r"^[A-Za-z]", base):
        base = "Hours" + base

    logical = base
    i = 2
    while logical in used:
        logical = f"{base}{i}"
        i += 1

    used.add(logical)
    return logical


def lambda_handler(event, context):
    template = {
        "AWSTemplateFormatVersion": "2010-09-09",
        "Description": "Export Amazon Connect Hours of Operation (with overrides)",
        "Parameters": {
            "ConnectInstanceArn": {
                "Type": "String",
                "Description": "Amazon Connect Instance ARN"
            }
        },
        "Resources": {}
    }

    used_ids = set()

    # 1) List all Hours of Operation (paginated)
    hours_list = []
    paginator = connect.get_paginator("list_hours_of_operations")
    for page in paginator.paginate(InstanceId=INSTANCE_ID):
        hours_list.extend(page.get("HoursOfOperationSummaryList", []))

    # 2) For each hours: describe (to get TimeZone/Config) + list overrides (already includes needed fields)
    for h in hours_list:
        hours_id = h["Id"]  # list_hours_of_operations returns "Id"

        detail = connect.describe_hours_of_operation(
            InstanceId=INSTANCE_ID,
            HoursOfOperationId=hours_id
        )["HoursOfOperation"]

        override_list = []
        override_paginator = connect.get_paginator("list_hours_of_operation_overrides")
        for o_page in override_paginator.paginate(
            InstanceId=INSTANCE_ID,
            HoursOfOperationId=hours_id
        ):
            overrides = o_page.get("HoursOfOperationOverrideList", [])
            for o in overrides:
                override_list.append({
                    "OverrideName": o.get("Name"),
                    "EffectiveFrom": o.get("EffectiveFrom"),
                    "EffectiveTill": o.get("EffectiveTill"),
                    "OverrideConfig": o.get("Config", [])
                })

        logical_id = sanitize_logical_id(detail["Name"], used_ids)

        template["Resources"][logical_id] = {
            "Type": "AWS::Connect::HoursOfOperation",
            "Properties": {
                # Use parameter so the same template can deploy to any instance
                "InstanceArn": {"Ref": "ConnectInstanceArn"},
                "Name": detail["Name"],  # Preserve original Connect name
                "TimeZone": detail["TimeZone"],
                "Config": detail.get("Config", []),
                "HoursOfOperationOverrides": override_list
            }
        }

    # 3) Dump JSON and write to S3
    json_data = json.dumps(template, indent=2, ensure_ascii=False)

    s3.put_object(
        Bucket=S3_BUCKET,
        Key=S3_KEY,
        Body=json_data.encode("utf-8"),
        ContentType="application/json"
    )

    return {
        "status": "success",
        "hours_count": len(hours_list),
        "s3_path": f"s3://{S3_BUCKET}/{S3_KEY}"
    }