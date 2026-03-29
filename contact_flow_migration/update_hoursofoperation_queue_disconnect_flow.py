"""
Repair invalid core resource references in Amazon Connect contact flows after migration.

Purpose:
When contact flows are migrated from one Amazon Connect instance to another,
resource ARNs inside the flow may become invalid even if the resource names remain the same.
This commonly affects:
- Hours of Operation
- Queues
- Disconnect flows (Event Hooks)

These resources should usually be repaired first after flow migration because invalid references
to them can make the entire flow invalid. In the Amazon Connect UI, this typically appears as
the flow being unable to publish. As a result, other resources such as Lex bots or Lambda functions
may also be impossible to update through the UpdateContactFlowContent API until these core references
are fixed first.

What this script does:
1. Read contact flows from the target Amazon Connect instance
2. Filter flows by a name keyword, such as CHAT or EMAIL
3. Read valid Hours of Operation, Queue, and Contact Flow ARNs from the current instance
4. Locate actions that reference:
   - Hours of Operation
   - Queues
   - Disconnect flows
5. Replace invalid ARNs with valid ARNs from the current instance
6. Save the updated flow content back to Amazon Connect

Notes:
- This script only updates the three core resource types listed above
- If your flow still cannot be published because of other invalid resources,
  check the Amazon Connect UI error message and extend this same logic with the relevant API
- This script assumes resource names such as Hours of Operation, Queue, and Disconnect Flow
  have not changed in the target instance
- If resource names have changed, it is recommended to add a mapping layer to resolve
  old names to new names before applying the update
- The main repair logic in this script still applies even when mappings are needed
"""

import json
import os
import boto3

CONNECT_REGION = os.environ["CONNECT_REGION"]
INSTANCE_ID = os.environ["INSTANCE_ID"]
FLOW_NAME_KEYWORD = os.environ["FLOW_NAME_KEYWORD"]

connect = boto3.client("connect", region_name=CONNECT_REGION)


def get_hours_map():
    resp = connect.list_hours_of_operations(
        InstanceId=INSTANCE_ID,
        MaxResults=1000
    )
    return {
        h["Name"]: h["Arn"]
        for h in resp["HoursOfOperationSummaryList"]
        if h.get("Name") and h.get("Arn")
    }


def get_queue_map():
    resp = connect.list_queues(
        InstanceId=INSTANCE_ID,
        MaxResults=1000
    )

    queue_map = {}
    for q in resp["QueueSummaryList"]:
        name = q.get("Name")
        arn = q.get("Arn")
        if name and arn:
            queue_map[name] = arn

    return queue_map


def get_flow_map():
    resp = connect.list_contact_flows(
        InstanceId=INSTANCE_ID,
        ContactFlowTypes=["CONTACT_FLOW"],
        MaxResults=1000
    )

    return {
        f["Name"]: f["Arn"]
        for f in resp["ContactFlowSummaryList"]
        if f.get("Name") and f.get("Arn")
    }


def list_flows():
    resp = connect.list_contact_flows(
        InstanceId=INSTANCE_ID,
        ContactFlowTypes=["CONTACT_FLOW"],
        MaxResults=1000
    )
    return resp["ContactFlowSummaryList"]


def lambda_handler(event, context):
    hours_map = get_hours_map()
    queue_map = get_queue_map()
    flow_map = get_flow_map()
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
            detail = connect.describe_contact_flow(
                InstanceId=INSTANCE_ID,
                ContactFlowId=flow["Id"]
            )["ContactFlow"]

            content = json.loads(detail["Content"])
            metadata_map = content.get("Metadata", {}).get("ActionMetadata", {})

            changed = False

            for action in content.get("Actions", []):
                action_type = action.get("Type")
                action_id = action.get("Identifier")
                metadata = metadata_map.get(action_id, {})

                # HOURS OF OPERATION
                if action_type == "CheckHoursOfOperation":
                    hours_name = metadata.get("Hours", {}).get("text")

                    if hours_name in hours_map:
                        new_arn = hours_map[hours_name]
                        current_arn = action.get("Parameters", {}).get("HoursOfOperationId")

                        if new_arn != current_arn:
                            print(f"[{flow_name}] Fix HOURS: {hours_name}")
                            action["Parameters"]["HoursOfOperationId"] = new_arn
                            changed = True

                # QUEUE
                if action_type in ["TransferContactToQueue", "UpdateContactTargetQueue"]:
                    queue_name = metadata.get("queue", {}).get("text")

                    if queue_name in queue_map:
                        new_arn = queue_map[queue_name]
                        current_arn = action.get("Parameters", {}).get("QueueId")

                        if new_arn != current_arn:
                            print(f"[{flow_name}] Fix QUEUE: {queue_name}")
                            action["Parameters"]["QueueId"] = new_arn
                            changed = True

                # DISCONNECT FLOW
                if action_type == "UpdateContactEventHooks":
                    hooks = action.get("Parameters", {}).get("EventHooks", {})
                    referenced_flow_name = metadata.get("contactFlow", {}).get("text")

                    if referenced_flow_name in flow_map:
                        new_arn = flow_map[referenced_flow_name]
                        current_arn = hooks.get("CustomerRemaining")

                        if new_arn != current_arn:
                            print(f"[{flow_name}] Fix DISCONNECT FLOW: {referenced_flow_name}")
                            hooks["CustomerRemaining"] = new_arn
                            changed = True

            if not changed:
                skipped.append(flow_name)
                continue

            connect.update_contact_flow_content(
                InstanceId=INSTANCE_ID,
                ContactFlowId=flow["Id"],
                Content=json.dumps(content)
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