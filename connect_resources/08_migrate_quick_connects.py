#!/usr/bin/env python3
"""
08_migrate_quick_connects.py — Copy quick connects to another Connect instance.

Creates the quick connects missing on the target, matched by name. A QUEUE-type
quick connect points at both a queue and a contact flow, and both IDs differ
between instances, so each is resolved by name before the quick connect is
created. If either is missing on the target, the quick connect is skipped.

Run 07_migrate_queues.py and the flow migration first.

Only QUEUE-type quick connects are migrated. USER types reference agents and
PHONE_NUMBER types reference claimed numbers — neither is migrated by these
scripts, so both would fail on create. They are skipped silently; use --verbose
to see which ones were passed over.

Usage:
  python3 08_migrate_quick_connects.py -s <SOURCE_ID> -t <TARGET_ID> -r us-west-2 [--dry-run]
"""

import argparse
import time

import boto3


def parse_args():
    p = argparse.ArgumentParser(description="Migrate quick connects between Connect instances")
    p.add_argument("-s", "--source-instance-id", required=True, help="Source Connect instance ID")
    p.add_argument("-t", "--target-instance-id", required=True, help="Target Connect instance ID")
    p.add_argument("-r", "--region", required=True, help="AWS region (e.g. us-west-2)")
    p.add_argument("--verbose", action="store_true",
                   help="Also list the non-QUEUE quick connects that were skipped")
    p.add_argument("--dry-run", action="store_true", help="Preview without making changes")
    return p.parse_args()


def paginate(method, instance_id, list_key, **extra):
    """Generic paginator for Connect list APIs."""
    items = []
    kwargs = {"InstanceId": instance_id, "MaxResults": 100, **extra}
    while True:
        resp = method(**kwargs)
        items.extend(resp.get(list_key, []))
        token = resp.get("NextToken")
        if not token:
            break
        kwargs["NextToken"] = token
    return items


def main():
    args = parse_args()
    connect = boto3.client("connect", region_name=args.region)

    if args.dry_run:
        print("=== DRY RUN — no changes will be made ===")

    print(f"\nSource: {args.source_instance_id}")
    print(f"Target: {args.target_instance_id}\n")

    source = [q for q in paginate(connect.list_quick_connects, args.source_instance_id,
                                  "QuickConnectSummaryList") if q.get("Name")]
    target = [q for q in paginate(connect.list_quick_connects, args.target_instance_id,
                                  "QuickConnectSummaryList") if q.get("Name")]
    target_names = {q["Name"] for q in target}

    # Queue and flow IDs differ between instances — resolve both by name
    source_queues = paginate(connect.list_queues, args.source_instance_id, "QueueSummaryList")
    target_queues = paginate(connect.list_queues, args.target_instance_id, "QueueSummaryList")
    source_queue_map = {q["Id"]: q["Name"] for q in source_queues if q.get("Name")}
    target_queue_map = {q["Name"]: q["Id"] for q in target_queues if q.get("Name")}

    source_flows = paginate(connect.list_contact_flows, args.source_instance_id,
                            "ContactFlowSummaryList")
    target_flows = paginate(connect.list_contact_flows, args.target_instance_id,
                            "ContactFlowSummaryList")
    source_flow_map = {f["Id"]: f["Name"] for f in source_flows}
    target_flow_map = {f["Name"]: f["Id"] for f in target_flows}

    print(f"Source: {len(source)}, Target: {len(target)}")

    missing = [q for q in source if q["Name"] not in target_names]
    if not missing:
        print("\n✓ All quick connects already on target.")
        return

    print(f"Missing on target: {len(missing)}")

    created = failed = 0
    skipped_types = []

    for qc in missing:
        name = qc["Name"]

        try:
            detail = connect.describe_quick_connect(
                InstanceId=args.source_instance_id,
                QuickConnectId=qc["Id"],
            )["QuickConnect"]
        except Exception as e:
            print(f"\n  → {name}")
            print(f"    ✗ Could not read source: {e}")
            failed += 1
            continue

        config = detail.get("QuickConnectConfig", {})
        qc_type = config.get("QuickConnectType", "")

        # USER and PHONE_NUMBER reference resources these scripts do not migrate
        if qc_type != "QUEUE":
            skipped_types.append((name, qc_type))
            continue

        print(f"\n  → {name}")

        qconf = config.get("QueueConfig", {})
        q_name = source_queue_map.get(qconf.get("QueueId", ""), "")
        f_name = source_flow_map.get(qconf.get("ContactFlowId", ""), "")
        tgt_qid = target_queue_map.get(q_name, "")
        tgt_fid = target_flow_map.get(f_name, "")

        if not tgt_qid or not tgt_fid:
            missing_parts = []
            if not tgt_qid:
                missing_parts.append(f"queue '{q_name}'")
            if not tgt_fid:
                missing_parts.append(f"flow '{f_name}'")
            print(f"    ⚠ Missing on target: {', '.join(missing_parts)} — skipping")
            failed += 1
            continue

        if args.dry_run:
            print(f"    Would create (queue: {q_name}, flow: {f_name})")
            created += 1
            continue

        try:
            kwargs = {
                "InstanceId": args.target_instance_id,
                "Name": name,
                "QuickConnectConfig": {
                    "QuickConnectType": "QUEUE",
                    "QueueConfig": {"QueueId": tgt_qid, "ContactFlowId": tgt_fid},
                },
            }
            if detail.get("Description"):
                kwargs["Description"] = detail["Description"]
            if detail.get("Tags"):
                kwargs["Tags"] = detail["Tags"]

            resp = connect.create_quick_connect(**kwargs)
            print(f"    ✓ Created (ID: {resp['QuickConnectId']})")
            created += 1
            time.sleep(0.4)
        except Exception as e:
            print(f"    ✗ Failed: {e}")
            failed += 1

    if skipped_types:
        print(f"\nSkipped {len(skipped_types)} non-QUEUE quick connect(s)")
        if args.verbose:
            for name, qc_type in skipped_types:
                print(f"  - {name} ({qc_type})")

    print(f"\n{'━' * 60}")
    print(f"Created: {created}, Failed: {failed}, Skipped: {len(skipped_types)}")
    print("━" * 60)

    if failed:
        print("\nFailures here mean a referenced queue or flow is not on the target yet.")
        print("Migrate those first, then re-run this script.")


if __name__ == "__main__":
    main()