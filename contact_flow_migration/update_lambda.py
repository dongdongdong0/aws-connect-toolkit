"""
Bulk update Lambda function references inside Amazon Connect contact flows.

Purpose:
When contact flows are migrated between Amazon Connect instances or environments,
the Lambda function ARN stored in the flow may become outdated or invalid. This script
replaces old Lambda references with the correct target Lambda functions in the current environment.

Flow migration note:
This script is usually used after core flow resources such as Hours of Operation,
Queues, and Disconnect Flows have already been repaired. Those resources should typically
be updated first because invalid references to them can make the whole flow invalid and
prevent publishing in the Amazon Connect UI. If the flow cannot be published, other resource
updates such as Lambda or Lex bot changes may also fail when using the UpdateContactFlowContent API.

What this script does:
1. Read contact flows from the target Amazon Connect instance
2. Filter flows by a name keyword, such as CHAT
3. Query AWS Lambda in the target region to get current function ARNs
4. Use a mapping to resolve old Lambda names to target Lambda names
5. Locate actions of type:
   - InvokeLambdaFunction
6. Replace the runtime Lambda ARN in:
   - Actions[*].Parameters.LambdaFunctionARN
7. Update related metadata fields used by the Amazon Connect UI
8. Save the updated flow content back to Amazon Connect

Notes:
- LambdaFunctionARN is the actual runtime field used by the flow
- Metadata displayName is mainly used by the Connect UI
- LAMBDA_MAPPING is the main mapping standard used to resolve old Lambda functions
  to new target Lambda functions. Update this section before use based on your own environment.
"""

import json
import os
import boto3

# =========================
# Basic Config
# =========================
REGION = os.environ["REGION"]
INSTANCE_ID = os.environ["INSTANCE_ID"]
FLOW_NAME_KEYWORD = os.environ["FLOW_NAME_KEYWORD"]

# Main mapping used to resolve old Lambda names to target Lambda names.
# Replace this with your own mapping before use.
LAMBDA_MAPPING = {
    "SourceLambdaA": "TargetLambdaA",
    "SourceLambdaB": "TargetLambdaB"
}

connect = boto3.client("connect", region_name=REGION)
lambda_client = boto3.client("lambda", region_name=REGION)


# =========================
# Helpers: Lambda
# =========================
def get_lambda_arn_map():
    resp = lambda_client.list_functions(MaxItems=1000)

    lambda_arn_map = {}
    for fn in resp.get("Functions", []):
        fn_name = fn.get("FunctionName")
        fn_arn = fn.get("FunctionArn")
        if fn_name and fn_arn:
            lambda_arn_map[fn_name] = fn_arn

    return lambda_arn_map


def build_target_lambda_info_map(lambda_mapping: dict):
    lambda_arn_map = get_lambda_arn_map()
    result = {}

    for old_name, new_name in lambda_mapping.items():
        if new_name not in lambda_arn_map:
            raise ValueError(f"Target lambda not found: {new_name}")

        result[old_name] = {
            "function_name": new_name,
            "function_arn": lambda_arn_map[new_name]
        }

        print(f"Resolved target lambda: {old_name} -> {result[old_name]}")

    return result


# =========================
# Helpers: Connect
# =========================
def list_flows():
    resp = connect.list_contact_flows(
        InstanceId=INSTANCE_ID,
        ContactFlowTypes=["CONTACT_FLOW"],
        MaxResults=1000
    )
    return resp["ContactFlowSummaryList"]


def get_flow_detail(flow_id: str):
    return connect.describe_contact_flow(
        InstanceId=INSTANCE_ID,
        ContactFlowId=flow_id
    )["ContactFlow"]


# =========================
# Main Logic
# =========================
def lambda_handler(event, context):
    target_lambda_info_map = build_target_lambda_info_map(LAMBDA_MAPPING)
    flows = list_flows()

    updated = []
    skipped = []
    failed = []

    for flow in flows:
        flow_name = flow["Name"]

        # Only process flows containing the target keyword
        if FLOW_NAME_KEYWORD.upper() not in flow_name.upper():
            continue

        try:
            detail = get_flow_detail(flow["Id"])
            content_obj = json.loads(detail["Content"])
            changed = False

            # 1. Update runtime Lambda ARN in Actions
            for action in content_obj.get("Actions", []):
                if action.get("Type") != "InvokeLambdaFunction":
                    continue

                params = action.get("Parameters", {})
                current_arn = params.get("LambdaFunctionARN", "")

                for old_name, new_info in target_lambda_info_map.items():
                    if old_name in current_arn:
                        if current_arn != new_info["function_arn"]:
                            print(
                                f"[{flow_name}] Fixing Action Lambda ARN: "
                                f"{current_arn} -> {new_info['function_arn']}"
                            )
                            params["LambdaFunctionARN"] = new_info["function_arn"]
                            changed = True
                        break

            # 2. Update metadata displayName used by the UI
            action_metadata = content_obj.get("Metadata", {}).get("ActionMetadata", {})

            for _, meta in action_metadata.items():
                parameters = meta.get("parameters", {})
                lambda_meta = parameters.get("LambdaFunctionARN")

                if not isinstance(lambda_meta, dict):
                    continue

                display_name = lambda_meta.get("displayName", "")

                for old_name, new_info in target_lambda_info_map.items():
                    if old_name in display_name:
                        if display_name != new_info["function_name"]:
                            print(
                                f"[{flow_name}] Fixing Metadata Lambda displayName: "
                                f"{display_name} -> {new_info['function_name']}"
                            )
                            lambda_meta["displayName"] = new_info["function_name"]
                            changed = True
                        break

            if not changed:
                skipped.append(flow_name)
                continue

            connect.update_contact_flow_content(
                InstanceId=INSTANCE_ID,
                ContactFlowId=flow["Id"],
                Content=json.dumps(content_obj)
            )

            print(f"[{flow_name}] UPDATED")
            updated.append(flow_name)

        except Exception as e:
            print(f"[{flow_name}] FAILED: {str(e)}")
            failed.append({
                "flow_name": flow_name,
                "error": str(e)
            })

    return {
        "flow_name_keyword": FLOW_NAME_KEYWORD,
        "updated_count": len(updated),
        "updated_flows": updated,
        "skipped_count": len(skipped),
        "skipped_flows": skipped,
        "failed_count": len(failed),
        "failed_flows": failed
    }