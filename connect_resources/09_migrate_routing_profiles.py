#!/usr/bin/env python3
"""
09_migrate_routing_profiles.py — Copy routing profiles to another Connect instance.

Creates the routing profiles missing on the target, matched by name. Each profile
carries two kinds of queue reference, and both are resolved by name because the
IDs differ between instances:

  - DefaultOutboundQueueId, which is required. If that queue is not on the
    target, the profile is skipped rather than created with the wrong queue.
  - The queue assignments (priority / delay per channel), which come from a
    separate list_routing_profile_queues call rather than describe_routing_profile.
    Any assignment whose queue is missing on the target is dropped, and the
    profile is still created with the rest.

Run 07_migrate_queues.py first.

Agent assignments are not migrated — users are out of scope for these scripts.

Usage:
  python3 09_migrate_routing_profiles.py -s <SOURCE_ID> -t <TARGET_ID> -r us-west-2 [--dry-run]
"""

import argparse
import time

import boto3


def parse_args():
    p = argparse.ArgumentParser(description="Migrate routing profiles between Connect instances")
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

    source = paginate(connect.list_routing_profiles, args.source_instance_id,
                      "RoutingProfileSummaryList")
    target = paginate(connect.list_routing_profiles, args.target_instance_id,
                      "RoutingProfileSummaryList")
    target_names = {r["Name"] for r in target}

    # Queue IDs differ between instances — resolve by name
    source_queues = paginate(connect.list_queues, args.source_instance_id, "QueueSummaryList")
    target_queues = paginate(connect.list_queues, args.target_instance_id, "QueueSummaryList")
    source_queue_map = {q["Id"]: q["Name"] for q in source_queues if q.get("Name")}
    target_queue_map = {q["Name"]: q["Id"] for q in target_queues if q.get("Name")}

    print(f"Source: {len(source)}, Target: {len(target)}")

    missing = [r for r in source if r["Name"] not in target_names]
    if not missing:
        print("\n✓ All routing profiles already on target.")
        return

    print(f"Missing on target: {len(missing)}")

    created = failed = 0

    for rp in missing:
        name = rp["Name"]
        print(f"\n  → {name}")

        try:
            detail = connect.describe_routing_profile(
                InstanceId=args.source_instance_id,
                RoutingProfileId=rp["Id"],
            )["RoutingProfile"]
        except Exception as e:
            print(f"    ✗ Could not read source: {e}")
            failed += 1
            continue

        # Default outbound queue is required
        default_q_name = source_queue_map.get(detail.get("DefaultOutboundQueueId", ""), "")
        tgt_default_q = target_queue_map.get(default_q_name, "")

        if not tgt_default_q:
            print(f"    ⚠ Default queue '{default_q_name}' not found on target — skipping")
            failed += 1
            continue

        # Queue assignments live in a separate API call
        queue_configs = []
        dropped = []
        try:
            src_qc = paginate(
                connect.list_routing_profile_queues, args.source_instance_id,
                "RoutingProfileQueueConfigSummaryList",
                RoutingProfileId=rp["Id"],
            )
            for qc in src_qc:
                q_name = source_queue_map.get(qc.get("QueueId", ""), "")
                tgt_qid = target_queue_map.get(q_name, "")
                if tgt_qid:
                    queue_configs.append({
                        "QueueReference": {
                            "QueueId": tgt_qid,
                            "Channel": qc.get("Channel", "VOICE"),
                        },
                        "Priority": qc.get("Priority", 1),
                        "Delay": qc.get("Delay", 0),
                    })
                else:
                    dropped.append(q_name or qc.get("QueueId", "?"))
        except Exception as e:
            print(f"    ⚠ Could not read queue assignments: {e}")

        if dropped:
            print(f"    ⚠ Dropping {len(dropped)} assignment(s) for queues not on target:")
            for d in sorted(set(dropped)):
                print(f"      - {d}")

        if args.dry_run:
            print(f"    Would create (default: {default_q_name}, "
                  f"{len(queue_configs)} queue config(s))")
            created += 1
            continue

        try:
            kwargs = {
                "InstanceId": args.target_instance_id,
                "Name": name,
                "DefaultOutboundQueueId": tgt_default_q,
                "MediaConcurrencies": detail.get(
                    "MediaConcurrencies", [{"Channel": "VOICE", "Concurrency": 1}]
                ),
            }
            if detail.get("Description"):
                kwargs["Description"] = detail["Description"]
            if detail.get("Tags"):
                kwargs["Tags"] = detail["Tags"]
            if queue_configs:
                kwargs["QueueConfigs"] = queue_configs

            resp = connect.create_routing_profile(**kwargs)
            print(f"    ✓ Created (ID: {resp['RoutingProfileId']})")
            created += 1
            time.sleep(0.4)
        except Exception as e:
            print(f"    ✗ Failed: {e}")
            failed += 1

    print(f"\n{'━' * 60}")
    print(f"Created: {created}, Failed: {failed}")
    print("━" * 60)

    if failed:
        print("\nFailures here are usually a missing queue on the target.")
        print("Run 07_migrate_queues.py, then re-run this script.")


if __name__ == "__main__":
    main()