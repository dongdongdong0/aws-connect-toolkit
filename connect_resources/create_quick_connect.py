import boto3
import os

# ====== CONFIG ======
INSTANCE_ID = os.environ["INSTANCE_ID"]
REGION = os.environ.get("REGION", "ca-central-1")

connect = boto3.client("connect", region_name=REGION)

# ====== YOUR DATA ======
quick_connects = [
    ("HD_GN_FR", "Quick Connect for HD & ISQ FR for technical assistance", "HD_GN_FR"),
    ("HD_PC_FR", "Quick Connect for HD & ISQ FR for technical assistance", "HD_PC_FR"),
    ("HD_NB_FR", "Quick Connect for HD_NB_FR", "HD_NB_FR"),
]

# ====== HELPERS ======
def get_all_queues():
    queues = {}
    paginator = connect.get_paginator("list_queues")

    for page in paginator.paginate(InstanceId=INSTANCE_ID):
        for q in page.get("QueueSummaryList", []):

            if q.get("QueueType") != "STANDARD":
                continue

            name = q.get("Name")
            if name:
                queues[name] = q["Id"]

    return queues


def get_default_transfer_flow_id():
    paginator = connect.get_paginator("list_contact_flows")

    for page in paginator.paginate(InstanceId=INSTANCE_ID):
        for flow in page["ContactFlowSummaryList"]:
            name = flow["Name"]
            if name.strip().lower() == "default queue transfer":
                print(f"✅ MATCHED FLOW: {name}")
                return flow["Id"]

    raise Exception("Default transfer flow not found")


# ====== LAMBDA ENTRY ======
def lambda_handler(event, context):
    results = []

    flow_id = get_default_transfer_flow_id()
    print(f"Using Default Transfer Flow ID: {flow_id}")


    queues = get_all_queues()
    print(f"Loaded {len(queues)} queues")

    for qc_name, qc_desc, queue_name in quick_connects:
        try:
            queue_id = queues.get(queue_name)

            if not queue_id:
                raise Exception(f"Queue not found: {queue_name}")

            connect.create_quick_connect(
                InstanceId=INSTANCE_ID,
                Name=qc_name,
                Description=qc_desc,
                QuickConnectConfig={
                    "QuickConnectType": "QUEUE",
                    "QueueConfig": {
                        "QueueId": queue_id,
                        "ContactFlowId": flow_id
                    }
                }
            )

            print(f"✅ Created: {qc_name}")
            results.append({"name": qc_name, "status": "created"})

        except Exception as e:
            print(f"❌ Failed: {qc_name} -> {str(e)}")
            results.append({"name": qc_name, "status": "failed", "error": str(e)})

    return results