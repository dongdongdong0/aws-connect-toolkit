#!/usr/bin/env python3
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

-------------------------------------------------------------------------
PARAMETERS
-------------------------------------------------------------------------
  --instance            REQUIRED  Connect instance ID or full ARN
  --region              OPTIONAL  AWS region (default: inferred from the ARN,
                                  else REGION / AWS_REGION env var, else ca-central-1)
  --config              OPTIONAL  Path to a JSON file holding the number->label list
                                  (default: the NUMBER_CONFIGS list in this file)
  --description-prefix  OPTIONAL  Prefix for the generated description
                                  (default: DESCRIPTION_PREFIX env var, else "PROD_LIVE for ")
  --dry-run             OPTIONAL  Resolve and report every mapping without
                                  writing anything to the instance

Instance values accept either form:
    <INSTANCE_ID>
    arn:aws:connect:<REGION>:<ACCOUNT_ID>:instance/<INSTANCE_ID>

-------------------------------------------------------------------------
CONFIG FILE FORMAT
-------------------------------------------------------------------------
A JSON array of objects, same shape as NUMBER_CONFIGS below:

    [
      {"number": "8881234567", "label": "YK"},
      {"number": "5191234567", "label": "NS"},
      {"number": "5191234568", "label": "YELLOW PHONE"}
    ]

-------------------------------------------------------------------------
USAGE
-------------------------------------------------------------------------
  # Preview first — resolves numbers and flows, writes nothing
  python3 map_phone_numbers.py \
      --instance <INSTANCE_ID> \
      --region ca-central-1 \
      --config numbers.json \
      --dry-run

  # Then run for real
  python3 map_phone_numbers.py \
      --instance <INSTANCE_ID> \
      --region ca-central-1 \
      --config numbers.json

  # ARN works too, and region is picked up from it
  python3 map_phone_numbers.py \
      --instance arn:aws:connect:<REGION>:<ACCOUNT_ID>:instance/<INSTANCE_ID> \
      --config numbers.json

-------------------------------------------------------------------------
IAM PERMISSIONS REQUIRED
-------------------------------------------------------------------------
  connect:ListPhoneNumbersV2
  connect:ListContactFlows
  connect:UpdatePhoneNumberMetadata
  connect:AssociatePhoneNumberContactFlow

-------------------------------------------------------------------------
EXIT CODES
-------------------------------------------------------------------------
  0  all configured numbers mapped successfully
  1  one or more numbers failed (details in the JSON summary)
  2  could not preload phone numbers or contact flows
"""

import argparse
import json
import os
import re
import sys

import boto3
from botocore.exceptions import ClientError

DEFAULT_REGION = "ca-central-1"

# ====== Config ======
# Populated from CLI arguments in main(); the helper functions below read these.
INSTANCE_ID = None
REGION = None
DESCRIPTION_PREFIX = None

connect = None

# ====== Number Configs ======
# Replace this example list with your own number-to-label mapping,
# or pass --config pointing at a JSON file with the same shape.
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


# ====== Argument Handling ======

def normalize_instance_id(value):
    """Accept either a bare instance ID or a full Connect instance ARN."""
    if not value:
        return value
    value = value.strip().rstrip("/")
    if value.startswith("arn:"):
        return value.split("/")[-1]
    return value


def region_from_arn(value):
    """Pull the region out of a Connect ARN, or return None."""
    if value and value.strip().startswith("arn:"):
        parts = value.strip().split(":")
        if len(parts) > 3 and parts[3]:
            return parts[3]
    return None


def load_number_configs(path):
    """Load the number->label list from a JSON file."""
    with open(path) as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError("Config file must contain a JSON array of objects.")
    for i, item in enumerate(data):
        if not isinstance(item, dict) or "number" not in item or "label" not in item:
            raise ValueError(f"Config entry {i} must be an object with 'number' and 'label'.")
    return data


def parse_args():
    parser = argparse.ArgumentParser(
        description="Bulk map Connect phone numbers to contact flows and update descriptions.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Example:\n"
            "  python3 map_phone_numbers.py \\\n"
            "      --instance <INSTANCE_ID> \\\n"
            "      --region ca-central-1 \\\n"
            "      --config numbers.json --dry-run\n"
        ),
    )
    parser.add_argument("--instance", required=True,
                        help="Connect instance ID or full ARN")
    parser.add_argument("--region", default=None,
                        help=f"AWS region (default: from ARN, else REGION/AWS_REGION, else {DEFAULT_REGION})")
    parser.add_argument("--config", default=None,
                        help="JSON file with the number->label list (default: built-in NUMBER_CONFIGS)")
    parser.add_argument("--description-prefix", default=None,
                        help='Description prefix (default: DESCRIPTION_PREFIX env var, else "PROD_LIVE for ")')
    parser.add_argument("--dry-run", action="store_true",
                        help="Resolve mappings and report without writing to the instance")

    args = parser.parse_args()

    if not args.region:
        args.region = (
            region_from_arn(args.instance)
            or os.environ.get("REGION")
            or os.environ.get("AWS_REGION")
            or os.environ.get("AWS_DEFAULT_REGION")
            or DEFAULT_REGION
        )

    if args.description_prefix is None:
        args.description_prefix = os.environ.get("DESCRIPTION_PREFIX", "PROD_LIVE for ")

    args.instance = normalize_instance_id(args.instance)

    return args


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


# ====== Mapping Run ======

def run_mapping(number_configs, dry_run=False):
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

    for item in number_configs:
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

            if not dry_run:
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
            "message": "Dry run completed — nothing was written." if dry_run
                       else "Completed phone number mapping.",
            "success_count": len(success),
            "failed_count": len(failed),
            "success": success,
            "failed": failed
        }, indent=2)
    }


# ====== CLI Entry Point ======

def main():
    global INSTANCE_ID, REGION, DESCRIPTION_PREFIX, connect

    args = parse_args()

    INSTANCE_ID = args.instance
    REGION = args.region
    DESCRIPTION_PREFIX = args.description_prefix

    connect = boto3.client("connect", region_name=REGION)

    number_configs = NUMBER_CONFIGS
    if args.config:
        try:
            number_configs = load_number_configs(args.config)
        except (OSError, ValueError, json.JSONDecodeError) as e:
            print(f"ERROR reading --config {args.config}: {e}", file=sys.stderr)
            sys.exit(2)

    if args.dry_run:
        print("=" * 55)
        print("  DRY RUN — nothing will be written to the instance")
        print("=" * 55)

    print(f"\nRegion:   {REGION}")
    print(f"Instance: {INSTANCE_ID}")
    print(f"Numbers:  {len(number_configs)} configured"
          f"{' (from ' + args.config + ')' if args.config else ' (built-in list)'}")
    print(f"Prefix:   {DESCRIPTION_PREFIX!r}\n")

    result = run_mapping(number_configs, dry_run=args.dry_run)

    print(result["body"])

    if result["statusCode"] != 200:
        sys.exit(2)

    body = json.loads(result["body"])
    sys.exit(1 if body["failed_count"] else 0)


if __name__ == "__main__":
    main()