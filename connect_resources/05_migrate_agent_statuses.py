#!/usr/bin/env python3
"""
05_migrate_agent_statuses.py — Copy Agent Statuses to another Connect instance.

Reads every Agent Status from the source instance and creates the ones missing
on the target, matched by name. ROUTABLE is a built-in system status that every
instance already has and cannot be created, so it is skipped.

No dependencies — can run at any point in the migration.

Usage:
  python3 05_migrate_agent_statuses.py -s <SOURCE_ID> -t <TARGET_ID> -r us-west-2 [--dry-run]
"""

import argparse
import time

import boto3

# Statuses Connect creates itself; create_agent_status rejects these
SYSTEM_TYPES = ("ROUTABLE",)


def parse_args():
    p = argparse.ArgumentParser(description="Migrate Agent Statuses between Connect instances")
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

    source = paginate(connect.list_agent_statuses, args.source_instance_id,
                      "AgentStatusSummaryList")
    target = paginate(connect.list_agent_statuses, args.target_instance_id,
                      "AgentStatusSummaryList")
    target_names = {s["Name"] for s in target}

    print(f"Source: {len(source)}, Target: {len(target)}")

    missing = [s for s in source if s["Name"] not in target_names]
    if not missing:
        print("\n✓ All Agent Statuses already on target.")
        return

    print(f"Missing on target: {len(missing)}")

    created = skipped = failed = 0

    for s in missing:
        name = s["Name"]

        try:
            detail = connect.describe_agent_status(
                InstanceId=args.source_instance_id,
                AgentStatusId=s["Id"],
            )["AgentStatus"]
        except Exception as e:
            print(f"\n  → {name}")
            print(f"    ✗ Could not read source: {e}")
            failed += 1
            continue

        status_type = detail.get("Type", "?")
        print(f"\n  → {name} (Type: {status_type})")

        if status_type in SYSTEM_TYPES:
            print("    Skipping system type")
            skipped += 1
            continue

        if args.dry_run:
            print("    Would create")
            created += 1
            continue

        try:
            kwargs = {
                "InstanceId": args.target_instance_id,
                "Name": name,
                "State": detail.get("State", "ENABLED"),
            }
            if detail.get("Description"):
                kwargs["Description"] = detail["Description"]
            if detail.get("DisplayOrder"):
                kwargs["DisplayOrder"] = detail["DisplayOrder"]
            if detail.get("Tags"):
                kwargs["Tags"] = detail["Tags"]

            resp = connect.create_agent_status(**kwargs)
            print(f"    ✓ Created (ID: {resp['AgentStatusId']})")
            created += 1
            time.sleep(0.4)
        except Exception as e:
            print(f"    ✗ Failed: {e}")
            failed += 1

    print(f"\n{'━' * 60}")
    print(f"Created: {created}, Skipped: {skipped}, Failed: {failed}")
    print("━" * 60)


if __name__ == "__main__":
    main()