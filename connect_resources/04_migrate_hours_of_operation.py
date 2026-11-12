#!/usr/bin/env python3
"""
04_migrate_hours_of_operation.py — Copy Hours of Operation to another Connect instance.

Reads every Hours of Operation from the source instance and creates the ones
missing on the target, matched by name. Timezone and weekly config are copied
as-is. Run this before migrating queues, which reference Hours of Operation.

Does not migrate overrides (holiday / one-off date exceptions).

Usage:
  python3 04_migrate_hours_of_operation.py -s <SOURCE_ID> -t <TARGET_ID> -r us-west-2 [--dry-run]
"""

import argparse
import time

import boto3


def parse_args():
    p = argparse.ArgumentParser(description="Migrate Hours of Operation between Connect instances")
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

    source = paginate(connect.list_hours_of_operations, args.source_instance_id,
                      "HoursOfOperationSummaryList")
    target = paginate(connect.list_hours_of_operations, args.target_instance_id,
                      "HoursOfOperationSummaryList")
    target_names = {h["Name"] for h in target}

    print(f"Source: {len(source)}, Target: {len(target)}")

    missing = [h for h in source if h["Name"] not in target_names]
    if not missing:
        print("\n✓ All Hours of Operation already on target.")
        return

    print(f"Missing on target: {len(missing)}")

    created = failed = 0

    for h in missing:
        name = h["Name"]
        print(f"\n  → {name}")

        try:
            detail = connect.describe_hours_of_operation(
                InstanceId=args.source_instance_id,
                HoursOfOperationId=h["Id"],
            )["HoursOfOperation"]
        except Exception as e:
            print(f"    ✗ Could not read source: {e}")
            failed += 1
            continue

        if args.dry_run:
            print(f"    Would create (TZ: {detail.get('TimeZone', '?')}, "
                  f"{len(detail.get('Config', []))} day config(s))")
            created += 1
            continue

        try:
            kwargs = {
                "InstanceId": args.target_instance_id,
                "Name": name,
                "TimeZone": detail["TimeZone"],
                "Config": detail["Config"],
            }
            if detail.get("Description"):
                kwargs["Description"] = detail["Description"]
            if detail.get("Tags"):
                kwargs["Tags"] = detail["Tags"]

            resp = connect.create_hours_of_operation(**kwargs)
            print(f"    ✓ Created (ID: {resp['HoursOfOperationId']})")
            created += 1
            time.sleep(0.4)
        except Exception as e:
            print(f"    ✗ Failed: {e}")
            failed += 1

    print(f"\n{'━' * 60}")
    print(f"Created: {created}, Failed: {failed}")
    print("━" * 60)


if __name__ == "__main__":
    main()