import os
import boto3

# Initialize AWS Connect client using environment variable for region
connect = boto3.client(
    "connect",
    region_name=os.environ["REGION"]
)

# Source and target instance IDs must be provided as environment variables
SOURCE_INSTANCE_ID = os.environ["SOURCE_INSTANCE_ID"]
TARGET_INSTANCE_ID = os.environ["TARGET_INSTANCE_ID"]


def lambda_handler(event, context):
    """
    Purpose:
    --------
    This Lambda function is used to migrate contact flows from a source Amazon Connect instance
    to a target instance.

    Key Behavior:
    -------------
    - Only migrates flows of type CONTACT_FLOW
    - Skips flows that already exist in the target instance (based on name)
    - Creates flows in the target instance in SAVED status (NOT published)

    Important Notes:
    ----------------
    1. All migrated flows will be in "SAVED" state:
       - They are NOT published
       - They cannot be used in production until published manually or via API

    2. This script does NOT handle dependency updates:
       - Lambda functions
       - Lex bots
       - Queues
       - Hours of Operation
       - Disconnect flows

       These dependencies MUST be updated separately before publishing.

    3. If dependencies are invalid:
       - The flow cannot be published in UI
       - UpdateContactFlowContent API will fail

    Recommended Workflow:
    ---------------------
    Step 1: Run this script to migrate flows (creates SAVED flows)
    Step 2: Run dependency update scripts (Lambda / Lex / Queue / HOA / Disconnect Flow)
    Step 3: Publish flows after validation
    """

    # Get all CONTACT_FLOW type flows from source instance
    source_flows = connect.list_contact_flows(
        InstanceId=SOURCE_INSTANCE_ID,
        ContactFlowTypes=["CONTACT_FLOW"],
        MaxResults=1000
    )["ContactFlowSummaryList"]

    # Get all CONTACT_FLOW type flows from target instance
    target_flows = connect.list_contact_flows(
        InstanceId=TARGET_INSTANCE_ID,
        ContactFlowTypes=["CONTACT_FLOW"],
        MaxResults=1000
    )["ContactFlowSummaryList"]

    # Build a set of existing flow names in target to avoid duplicates
    target_names = {f["Name"] for f in target_flows}

    created = []
    skipped = []

    for flow in source_flows:

        name = flow["Name"]

        # Skip if flow already exists in target instance
        if name in target_names:
            skipped.append(name)
            continue

        # Get full flow definition from source
        detail = connect.describe_contact_flow(
            InstanceId=SOURCE_INSTANCE_ID,
            ContactFlowId=flow["Id"]
        )["ContactFlow"]

        # Create flow in target instance (IMPORTANT: status = SAVED)
        connect.create_contact_flow(
            InstanceId=TARGET_INSTANCE_ID,
            Name=detail["Name"],
            Type=detail["Type"],
            Description=detail.get("Description", ""),
            Content=detail["Content"],
            Status="SAVED"  # Ensures flow is created but not published
        )

        created.append(name)

    return {
        "created_count": len(created),
        "created": created,
        "skipped_count": len(skipped),
        "skipped": skipped
    }