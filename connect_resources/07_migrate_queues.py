#!/usr/bin/env python3
"""
07_migrate_queues.py — Copy queues to another Connect instance.

Creates the queues missing on the target, matched by name. Each queue points at
an Hours of Operation, whose ID differs between instances, so the script resolves
it by name: source HoO ID → name → target HoO ID. A queue whose Hours of
Operation does not exist on the target is skipped rather than created wrong.

Run 04_migrate_hours_of_operation.py first.

Two things are deliberately not copied:
  - AGENT-type queues. Connect creates one per user automatically; they have no
    Name field in list_queues, which is also why names are read with .get().
  - OutboundCallerConfig. It references phone numbers and flows that do not
    exist on the target, and including it makes create_queue fail. Set the
    outbound caller ID manually afterwards if the queue needs one.

Usage:
  python3 07_migrate_queues.py -s <SOURCE_ID> -t <TARGET_ID> -r us-west-2 [--dry-run]
"""

import argparse
import time

import boto3


def parse_args():
    p = argparse.ArgumentParser(description="Migrate queues between Connect instances")
    p.add_argument("-s", "--source-instance-id", required=True, help="Source Connect instance ID")
    p.add_argument("-t", "--target-instance-id", required=True, help="Target Connect instance ID")
    p.add_argument("-r", "--region", required=True, help="AWS region (e.g. us-west-2)")
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

    source_raw = paginate(connect.list_queues, args.source_instance_id, "QueueSummaryList")
    target_raw = paginate(connect.list_queues, args.target_instance_id, "QueueSummaryList")

    # AGENT-type queues are per-user and have no Name — filter them out
    source = [q for q in source_raw if q.get("Name") and q.get("QueueType") != "AGENT"]
    target = [q for q in target_raw if q.get("Name")]
    target_names = {q["Name"] for q in target}

    # Hours of Operation IDs differ between instances — resolve by name
    source_hoo = paginate(connect.list_hours_of_operations, args.source_instance_id,
                          "HoursOfOperationSummaryList")
    target_hoo = paginate(connect.list_hours_of_operations, args.target_instance_id,
                          "HoursOfOperationSummaryList")
    source_hoo_map = {h["Id"]: h["Name"] for h in source_hoo}
    target_hoo_map = {h["Name"]: h["Id"] for h in target_hoo}

    print(f"Source: {len(source)} standard + {len(source_raw) - len(source)} agent (skipped)")
    print(f"Target: {len(target)}")

    missing = [q for q in source if q["Name"] not in target_names]
    if not missing:
        print("\n✓ All queues already on target.")
        return

    print(f"Missing on target: {len(missing)}")

    created = failed = 0

    for q in missing:
        name = q["Name"]
        print(f"\n  → {name}")

        try:
            detail = connect.describe_queue(
                InstanceId=args.source_instance_id,
                QueueId=q["Id"],
            )["Queue"]
        except Exception as e:
            print(f"    ✗ Could not read source: {e}")
            failed += 1
            continue

        source_hoo_id = detail.get("HoursOfOperationId", "")
        hoo_name = source_hoo_map.get(source_hoo_id, "")
        target_hoo_id = target_hoo_map.get(hoo_name, "")

        if not target_hoo_id and source_hoo_id:
            print(f"    ⚠ Hours of Operation '{hoo_name}' not found on target — skipping")
            failed += 1
            continue

        if args.dry_run:
            print(f"    Would create (HoO: {hoo_name})")
            created += 1
            continue

        try:
            kwargs = {
                "InstanceId": args.target_instance_id,
                "Name": name,
                "HoursOfOperationId": target_hoo_id,
            }
            if detail.get("Description"):
                kwargs["Description"] = detail["Description"]
            if detail.get("MaxContacts"):
                kwargs["MaxContacts"] = detail["MaxContacts"]
            if detail.get("Tags"):
                kwargs["Tags"] = detail["Tags"]

            # OutboundCallerConfig intentionally omitted — see module docstring

            resp = connect.create_queue(**kwargs)
            print(f"    ✓ Created (ID: {resp['QueueId']})")
            created += 1
            time.sleep(0.4)
        except Exception as e:
            print(f"    ✗ Failed: {e}")
            failed += 1

    print(f"\n{'━' * 60}")
    print(f"Created: {created}, Failed: {failed}")
    print("━" * 60)

    if failed:
        print("\nMost failures here are a missing Hours of Operation on the target.")
        print("Run 04_migrate_hours_of_operation.py, then re-run this script.")


if __name__ == "__main__":
    main()