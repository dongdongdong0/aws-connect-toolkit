"""
Bulk map Amazon Connect phone numbers to contact flows and update descriptions.

Purpose:
This script is used after AWS completes phone number porting and the numbers have
already been added into the Amazon Connect instance. At that point, each phone number
still needs to be:
1. associated with the correct contact flow
2. updated with a standardized description

Doing this manually in the UI can be repetitive and error-prone, especially when
handling many numbers at once. This script automates the process using a predefined
configuration list and a consistent naming standard.

How it works:
1. Read all phone numbers currently available in the target Amazon Connect instance
2. Read all contact flows in the same instance
3. For each configured phone number:
   - normalize the number format
   - build the expected flow name using a naming convention
   - build the target description
   - update phone number metadata
   - associate the phone number with the target flow

Naming convention:
This script assumes the contact flow naming standard follows this pattern:

    CS_<LABEL>_Voice

Examples:
- YK -> CS_YK_Voice
- YELLOW PHONE -> CS_YELLOW_PHONE_Voice
- HD & ISQ FR -> CS_HD_ISQ_FR_Voice

This naming rule is environment-specific. If you want to use this script in another
environment, update the flow naming logic in `build_flow_name()` to match your own standard.

Scope and limitation:
- This script is most useful when number mapping work follows a standardized file or naming rule
- It does not solve every number-porting scenario
- It works best when the target flow name can be derived consistently from a label
- Even if your naming standard is not fully uniform, this script can still reduce manual work
  and lower the chance of mapping errors
"""

import json
import os
import re
import boto3
from botocore.exceptions import ClientError

# ====== Config ======
INSTANCE_ID = os.environ["INSTANCE_ID"]
REGION = os.environ["REGION"]
DESCRIPTION_PREFIX = os.environ.get("DESCRIPTION_PREFIX", "PROD_LIVE for ")

connect = boto3.client("connect", region_name=REGION)

# ====== Number Configs ======
# Replace this example list with your own number-to-label mapping.
#
# Notes:
# 1. Phone numbers can be provided in a simplified digit format
# 2. If a number is given as 10 digits, the script will prepend leading "1"
#    to match instance format such as +1XXXXXXXXXX
# 3. Labels are used to derive both description text and expected flow names
NUMBER_CONFIGS = [
    {"number": "8881234567", "label": "YK"},
    {"number": "5191234567", "label": "NS"},
    {"number": "5191234568", "label": "YELLOW PHONE"},
    {"number": "5191234569", "label": "CLIENT SUPPORT"},
]


# ====== Helper Functions ======

def normalize_digits(phone: str) -> str:
    return re.sub(r"\D", "", phone or "")


def build_flow_name(label: str) -> str:
    """
    Build flow name from label using the current naming standard.

    Examples:
    - YK -> CS_YK_Voice
    - YELLOW PHONE -> CS_YELLOW_PHONE_Voice
    - HD & ISQ FR -> CS_HD_ISQ_FR_Voice

    Update this function if your flow naming convention is different.
    """
    normalized = re.sub(r"[^A-Za-z0-9]+", "_", label.strip().upper())
    normalized = re.sub(r"_+", "_", normalized).strip("_")
    return f"CS_{normalized}_Voice"


def build_description(label: str) -> str:
    return f"{DESCRIPTION_PREFIX}{label}"


def get_phone_map():
    """
    Returns a lookup map like:
    {
        "18671234567": {
            "id": "...",
            "arn": "...",
            "raw": "+18671234567"
        }
    }
    """
    phone_map = {}
    next_token = None

    while True:
        kwargs = {
            "InstanceId": INSTANCE_ID,
            "MaxResults": 100
        }
        if next_token:
            kwargs["NextToken"] = next_token

        resp = connect.list_phone_numbers_v2(**kwargs)

        for item in resp.get("ListPhoneNumbersSummaryList", []):
            raw = item.get("PhoneNumber", "")
            digits = normalize_digits(raw)
            if digits:
                phone_map[digits] = {
                    "id": item.get("PhoneNumberId") or item.get("Id"),
                    "arn": item.get("PhoneNumberArn") or item.get("Arn"),
                    "raw": raw
                }

        next_token = resp.get("NextToken")
        if not next_token:
            break

    return phone_map


def get_flow_map():
    """
    Returns a lookup map like:
    {
        "CS_YK_Voice": "flow-id",
        ...
    }
    """
    flow_map = {}
    next_token = None

    while True:
        kwargs = {
            "InstanceId": INSTANCE_ID,
            "ContactFlowTypes": ["CONTACT_FLOW"],
            "MaxResults": 100
        }
        if next_token:
            kwargs["NextToken"] = next_token

        resp = connect.list_contact_flows(**kwargs)

        for flow in resp.get("ContactFlowSummaryList", []):
            name = flow.get("Name")
            flow_id = flow.get("Id")
            if name and flow_id:
                flow_map[name] = flow_id

        next_token = resp.get("NextToken")
        if not next_token:
            break

    return flow_map


# ====== Lambda Handler ======

def lambda_handler(event, context):
    failed = []
    success = []

    try:
        phone_map = get_phone_map()
        flow_map = get_flow_map()
    except Exception as e:
        return {
            "statusCode": 500,
            "body": json.dumps({
                "message": "Failed to preload phone numbers or flows.",
                "error": str(e)
            }, indent=2)
        }

    for item in NUMBER_CONFIGS:
        number = item["number"]
        label = item["label"]

        digits = normalize_digits(number)

        # Match instance phone format such as +1XXXXXXXXXX
        # If input is 10 digits, prepend leading "1"
        if len(digits) == 10:
            digits = "1" + digits

        flow_name = build_flow_name(label)
        description = build_description(label)

        try:
            phone_info = phone_map.get(digits)
            if not phone_info:
                failed.append({
                    "number": number,
                    "label": label,
                    "reason": f"Phone number not found in instance. Expected digits: {digits}"
                })
                continue

            flow_id = flow_map.get(flow_name)
            if not flow_id:
                failed.append({
                    "number": number,
                    "label": label,
                    "reason": f"Flow not found: {flow_name}"
                })
                continue

            # Update description
            connect.update_phone_number_metadata(
                PhoneNumberId=phone_info["id"],
                PhoneNumberDescription=description
            )

            # Associate phone number to flow
            connect.associate_phone_number_contact_flow(
                PhoneNumberId=phone_info["id"],
                InstanceId=INSTANCE_ID,
                ContactFlowId=flow_id
            )

            success.append({
                "input_number": number,
                "instance_number": phone_info["raw"],
                "label": label,
                "description": description,
                "flow_name": flow_name
            })

        except ClientError as e:
            failed.append({
                "number": number,
                "label": label,
                "flow_name": flow_name,
                "description": description,
                "reason": str(e)
            })
        except Exception as e:
            failed.append({
                "number": number,
                "label": label,
                "flow_name": flow_name,
                "description": description,
                "reason": str(e)
            })

    return {
        "statusCode": 200,
        "body": json.dumps({
            "message": "Completed phone number mapping.",
            "success_count": len(success),
            "failed_count": len(failed),
            "success": success,
            "failed": failed
        }, indent=2)
    }