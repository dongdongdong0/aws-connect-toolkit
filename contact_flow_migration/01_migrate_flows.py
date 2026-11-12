#!/usr/bin/env python3
"""
10_migrate_flows.py — Migrate contact flows from a source Amazon Connect instance
to a target instance.

Purpose:
--------
Copies contact flows, with their full content, from the source instance to the
target instance.

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

   SAVED status is what makes this script work at all: Connect does not validate
   referenced resources for a saved flow, so a flow can be created before its
   Lambda functions, queues and prompts exist on the target.

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

--dry-run lists the flows that would be created without calling
describe_contact_flow or create_contact_flow. The script exits with status 1 if
any flow failed, so it can be used in a pipeline.

Usage:
  python3 10_migrate_flows.py -s <SOURCE_ID> -t <TARGET_ID> -r us-west-2 --dry-run
"""

import argparse
import sys

import boto3


def parse_args():
    p = argparse.ArgumentParser(description="Migrate contact flows between Connect instances")
    p.add_argument("-s", "--source-instance-id", required=True, help="Source Connect instance ID")
    p.add_argument("-t", "--target-instance-id", required=True, help="Target Connect instance ID")
    p.add_argument("-r", "--region", required=True, help="AWS region (e.g. us-west-2)")
    p.add_argument("--dry-run", action="store_true",
                   help="List the flows that would be created without creating anything")
    return p.parse_args()


def main():
    args = parse_args()

    # Both instances are in the same region, so one client serves both
    connect = boto3.client("connect", region_name=args.region)

    SOURCE_INSTANCE_ID = args.source_instance_id
    TARGET_INSTANCE_ID = args.target_instance_id

    if args.dry_run:
        print("=== DRY RUN — no flows will be created ===")

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

    print(f"Source flows count: {len(source_flows)}")
    print(f"Target flows count: {len(target_flows)}")

    created = []
    skipped = []
    failed = []

    for flow in source_flows:

        name = flow["Name"]

        # Skip if flow already exists in target instance
        if name in target_names:
            skipped.append(name)
            continue

        if args.dry_run:
            created.append(name)
            print(f"Would create: {name} | {flow['Id']}")
            continue

        try:
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
            print(f"Created: {name}")

        except Exception as e:
            failed.append(name)
            print(f"Failed: {name} | {flow['Id']} | {str(e)}")

    print(f"\nCreated: {len(created)}")
    print(f"Skipped: {len(skipped)}")
    print(f"Failed:  {len(failed)}")

    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()