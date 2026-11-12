#!/usr/bin/env python3
"""
03_migrate_resources.py — Migrate base resources between two Amazon Connect instances.

Migrates six resource types, matched by name, skipping anything that already
exists on the target:

  1. hoo              Hours of Operation
  2. agent-status     Agent Statuses
  3. prompts          Prompts (audio files, via S3)
  4. queues           Queues (depends on Hours of Operation)
  5. quick-connects   Quick Connects, QUEUE type only (depends on Queues + Flows)
  6. routing-profiles Routing Profiles (depends on Queues)

Phases run in dependency order. Use --only to run a subset.

Usage:
  python3 03_migrate_resources.py -s <SOURCE_ID> -t <TARGET_ID> -r us-west-2 --dry-run
  python3 03_migrate_resources.py -s <SOURCE_ID> -t <TARGET_ID> -r us-west-2 \
      --s3-bucket my-bucket
  python3 03_migrate_resources.py -s <SOURCE_ID> -t <TARGET_ID> -r us-west-2 \
      --only queues,routing-profiles

Notes:
  - --s3-bucket is only needed for the prompts phase. Prompt audio is downloaded
    from the source instance via a presigned URL, staged in S3, then registered
    on the target. The staged files are deleted afterwards.
  - Not migrated: phone numbers, users, OutboundCallerConfig on queues,
    USER / PHONE_NUMBER quick connects, Hours of Operation overrides.
  - Run the flow/module update scripts afterwards to fix ID references.
"""

import argparse
import os
import sys
import tempfile
import time
import urllib.request

import boto3

PHASES = ["hoo", "agent-status", "prompts", "queues", "quick-connects", "routing-profiles"]

stats = {"created": 0, "skipped": 0, "failed": 0}


def parse_args():
    p = argparse.ArgumentParser(description="Migrate base resources between Connect instances")
    p.add_argument("-s", "--source-instance-id", required=True, help="Source Connect instance ID")
    p.add_argument("-t", "--target-instance-id", required=True, help="Target Connect instance ID")
    p.add_argument("-r", "--region", required=True, help="AWS region (e.g. us-west-2)")
    p.add_argument("--s3-bucket", help="S3 bucket for staging prompt audio (prompts phase only)")
    p.add_argument("--s3-prefix", default="prompt-migration/", help="S3 key prefix for staging")
    p.add_argument("--only", help=f"Comma-separated phases to run. Choices: {','.join(PHASES)}")
    p.add_argument("--dry-run", action="store_true", help="Preview without making changes")
    return p.parse_args()


def throttle():
    time.sleep(0.4)


def print_header(title):
    print(f"\n{'━' * 60}")
    print(title)
    print("━" * 60)


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


# ════════════════════════════════════════════════════════════
# HOURS OF OPERATION
# ════════════════════════════════════════════════════════════
def migrate_hours_of_operation(connect, args):
    print_header("HOURS OF OPERATION")

    source = paginate(connect.list_hours_of_operations, args.source_instance_id,
                      "HoursOfOperationSummaryList")
    target = paginate(connect.list_hours_of_operations, args.target_instance_id,
                      "HoursOfOperationSummaryList")
    target_names = {h["Name"] for h in target}

    print(f"Source: {len(source)}, Target: {len(target)}")

    for h in source:
        name = h["Name"]
        if name in target_names:
            continue

        detail = connect.describe_hours_of_operation(
            InstanceId=args.source_instance_id,
            HoursOfOperationId=h["Id"],
        )["HoursOfOperation"]

        print(f"\n  → {name}")
        if args.dry_run:
            print("    Would create")
            stats["created"] += 1
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
            stats["created"] += 1
            throttle()
        except Exception as e:
            print(f"    ✗ Failed: {e}")
            stats["failed"] += 1


# ════════════════════════════════════════════════════════════
# AGENT STATUSES
# ════════════════════════════════════════════════════════════
def migrate_agent_statuses(connect, args):
    print_header("AGENT STATUSES")

    source = paginate(connect.list_agent_statuses, args.source_instance_id,
                      "AgentStatusSummaryList")
    target = paginate(connect.list_agent_statuses, args.target_instance_id,
                      "AgentStatusSummaryList")
    target_names = {s["Name"] for s in target}

    print(f"Source: {len(source)}, Target: {len(target)}")

    for s in source:
        name = s["Name"]
        if name in target_names:
            continue

        detail = connect.describe_agent_status(
            InstanceId=args.source_instance_id,
            AgentStatusId=s["Id"],
        )["AgentStatus"]

        print(f"\n  → {name} (Type: {detail.get('Type', '?')})")

        # ROUTABLE is a system status and cannot be created
        if detail.get("Type") in ("ROUTABLE",):
            print("    Skipping system type")
            stats["skipped"] += 1
            continue

        if args.dry_run:
            print("    Would create")
            stats["created"] += 1
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
            stats["created"] += 1
            throttle()
        except Exception as e:
            print(f"    ✗ Failed: {e}")
            stats["failed"] += 1


# ════════════════════════════════════════════════════════════
# PROMPTS
# ════════════════════════════════════════════════════════════
def safe_filename(name):
    """Make a prompt name safe to use as an S3 key / local filename."""
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in name)[:200]


def migrate_prompts(connect, s3, args):
    print_header("PROMPTS")

    if not args.s3_bucket and not args.dry_run:
        print("✗ --s3-bucket is required for the prompts phase. Skipping.")
        stats["failed"] += 1
        return

    source = paginate(connect.list_prompts, args.source_instance_id, "PromptSummaryList")
    target = paginate(connect.list_prompts, args.target_instance_id, "PromptSummaryList")
    target_names = {p["Name"] for p in target}

    print(f"Source: {len(source)}, Target: {len(target)}")

    missing = [p for p in source if p["Name"] not in target_names]
    if not missing:
        print("✓ All prompts already on target.")
        return

    print(f"Missing on target: {len(missing)}")

    with tempfile.TemporaryDirectory() as tmpdir:
        for p in missing:
            name = p["Name"]
            print(f"\n  → {name}")

            if args.dry_run:
                print("    Would migrate")
                stats["created"] += 1
                continue

            # 1. Presigned download URL from the source instance
            try:
                resp = connect.get_prompt_file(
                    InstanceId=args.source_instance_id,
                    PromptId=p["Id"],
                )
                url = resp.get("PromptPresignedUrl", "")
                if not url:
                    print("    ✗ No presigned URL returned")
                    stats["failed"] += 1
                    continue
            except Exception as e:
                print(f"    ✗ get_prompt_file failed: {e}")
                stats["failed"] += 1
                continue

            # 2. Download the audio
            local_file = os.path.join(tmpdir, f"{p['Id']}.wav")
            try:
                urllib.request.urlretrieve(url, local_file)
                print(f"    Downloaded ({os.path.getsize(local_file) / 1024:.0f} KB)")
            except Exception as e:
                print(f"    ✗ Download failed: {e}")
                stats["failed"] += 1
                continue

            # 3. Stage in S3
            s3_key = f"{args.s3_prefix}{safe_filename(name)}.wav"
            try:
                s3.upload_file(local_file, args.s3_bucket, s3_key)
            except Exception as e:
                print(f"    ✗ S3 upload failed: {e}")
                stats["failed"] += 1
                continue

            # 4. Register on the target instance
            try:
                try:
                    description = connect.describe_prompt(
                        InstanceId=args.source_instance_id,
                        PromptId=p["Id"],
                    )["Prompt"].get("Description", "")
                except Exception:
                    description = ""

                kwargs = {
                    "InstanceId": args.target_instance_id,
                    "Name": name,
                    "S3Uri": f"s3://{args.s3_bucket}/{s3_key}",
                }
                if description:
                    kwargs["Description"] = description

                resp = connect.create_prompt(**kwargs)
                print(f"    ✓ Created (ID: {resp['PromptId']})")
                stats["created"] += 1
                throttle()
            except Exception as e:
                print(f"    ✗ create_prompt failed: {e}")
                stats["failed"] += 1

            try:
                os.remove(local_file)
            except Exception:
                pass

    # Clean up the staged S3 objects
    if not args.dry_run and args.s3_bucket:
        print(f"\n  Cleaning up s3://{args.s3_bucket}/{args.s3_prefix}")
        try:
            objs = s3.list_objects_v2(Bucket=args.s3_bucket, Prefix=args.s3_prefix)
            for obj in objs.get("Contents", []):
                s3.delete_object(Bucket=args.s3_bucket, Key=obj["Key"])
            print("  ✓ Cleaned up")
        except Exception as e:
            print(f"  ⚠ Cleanup failed (not critical): {e}")


# ════════════════════════════════════════════════════════════
# QUEUES
# ════════════════════════════════════════════════════════════
def migrate_queues(connect, args):
    print_header("QUEUES")

    source_raw = paginate(connect.list_queues, args.source_instance_id, "QueueSummaryList")
    target_raw = paginate(connect.list_queues, args.target_instance_id, "QueueSummaryList")

    # AGENT-type queues have no Name field and are created automatically per user
    source = [q for q in source_raw if q.get("Name") and q.get("QueueType") != "AGENT"]
    target = [q for q in target_raw if q.get("Name")]
    target_names = {q["Name"] for q in target}

    # Hours of Operation must be resolved by name — IDs differ between instances
    source_hoo = paginate(connect.list_hours_of_operations, args.source_instance_id,
                          "HoursOfOperationSummaryList")
    target_hoo = paginate(connect.list_hours_of_operations, args.target_instance_id,
                          "HoursOfOperationSummaryList")
    source_hoo_map = {h["Id"]: h["Name"] for h in source_hoo}
    target_hoo_map = {h["Name"]: h["Id"] for h in target_hoo}

    print(f"Source: {len(source)} standard + {len(source_raw) - len(source)} agent (skipped)")
    print(f"Target: {len(target)}")

    for q in source:
        name = q["Name"]
        if name in target_names:
            continue

        detail = connect.describe_queue(
            InstanceId=args.source_instance_id,
            QueueId=q["Id"],
        )["Queue"]

        print(f"\n  → {name}")

        source_hoo_id = detail.get("HoursOfOperationId", "")
        hoo_name = source_hoo_map.get(source_hoo_id, "")
        target_hoo_id = target_hoo_map.get(hoo_name, "")

        if not target_hoo_id and source_hoo_id:
            print(f"    ⚠ Hours of Operation '{hoo_name}' not found on target — skipping")
            stats["failed"] += 1
            continue

        if args.dry_run:
            print(f"    Would create (HoO: {hoo_name})")
            stats["created"] += 1
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

            # OutboundCallerConfig is intentionally dropped — it references phone
            # numbers and flows that do not exist on the target. Configure manually.

            resp = connect.create_queue(**kwargs)
            print(f"    ✓ Created (ID: {resp['QueueId']})")
            stats["created"] += 1
            throttle()
        except Exception as e:
            print(f"    ✗ Failed: {e}")
            stats["failed"] += 1


# ════════════════════════════════════════════════════════════
# QUICK CONNECTS
# ════════════════════════════════════════════════════════════
def migrate_quick_connects(connect, args):
    print_header("QUICK CONNECTS")

    source = [q for q in paginate(connect.list_quick_connects, args.source_instance_id,
                                  "QuickConnectSummaryList") if q.get("Name")]
    target = [q for q in paginate(connect.list_quick_connects, args.target_instance_id,
                                  "QuickConnectSummaryList") if q.get("Name")]
    target_names = {q["Name"] for q in target}

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

    for qc in source:
        name = qc["Name"]
        if name in target_names:
            continue

        detail = connect.describe_quick_connect(
            InstanceId=args.source_instance_id,
            QuickConnectId=qc["Id"],
        )["QuickConnect"]

        config = detail.get("QuickConnectConfig", {})
        qc_type = config.get("QuickConnectType", "")

        # USER and PHONE_NUMBER types reference resources that are not migrated
        if qc_type != "QUEUE":
            continue

        print(f"\n  → {name}")

        if args.dry_run:
            print("    Would create")
            stats["created"] += 1
            continue

        try:
            qconf = config.get("QueueConfig", {})
            q_name = source_queue_map.get(qconf.get("QueueId", ""), "")
            f_name = source_flow_map.get(qconf.get("ContactFlowId", ""), "")
            tgt_qid = target_queue_map.get(q_name, "")
            tgt_fid = target_flow_map.get(f_name, "")

            if not tgt_qid or not tgt_fid:
                print(f"    ⚠ Queue '{q_name}' or Flow '{f_name}' missing on target")
                stats["failed"] += 1
                continue

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
            stats["created"] += 1
            throttle()
        except Exception as e:
            print(f"    ✗ Failed: {e}")
            stats["failed"] += 1


# ════════════════════════════════════════════════════════════
# ROUTING PROFILES
# ════════════════════════════════════════════════════════════
def migrate_routing_profiles(connect, args):
    print_header("ROUTING PROFILES")

    source = paginate(connect.list_routing_profiles, args.source_instance_id,
                      "RoutingProfileSummaryList")
    target = paginate(connect.list_routing_profiles, args.target_instance_id,
                      "RoutingProfileSummaryList")
    target_names = {r["Name"] for r in target}

    source_queues = paginate(connect.list_queues, args.source_instance_id, "QueueSummaryList")
    target_queues = paginate(connect.list_queues, args.target_instance_id, "QueueSummaryList")
    source_queue_map = {q["Id"]: q["Name"] for q in source_queues if q.get("Name")}
    target_queue_map = {q["Name"]: q["Id"] for q in target_queues if q.get("Name")}

    print(f"Source: {len(source)}, Target: {len(target)}")

    for rp in source:
        name = rp["Name"]
        if name in target_names:
            continue

        detail = connect.describe_routing_profile(
            InstanceId=args.source_instance_id,
            RoutingProfileId=rp["Id"],
        )["RoutingProfile"]

        print(f"\n  → {name}")

        # Default outbound queue is required and must exist on the target
        default_q_name = source_queue_map.get(detail.get("DefaultOutboundQueueId", ""), "")
        tgt_default_q = target_queue_map.get(default_q_name, "")

        if not tgt_default_q:
            print(f"    ⚠ Default queue '{default_q_name}' not found on target — skipping")
            stats["failed"] += 1
            continue

        # Queue assignments come from a separate list call
        queue_configs = []
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
        except Exception as e:
            print(f"    ⚠ Could not read queue assignments: {e}")

        if args.dry_run:
            print(f"    Would create (default: {default_q_name}, "
                  f"{len(queue_configs)} queue config(s))")
            stats["created"] += 1
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
            stats["created"] += 1
            throttle()
        except Exception as e:
            print(f"    ✗ Failed: {e}")
            stats["failed"] += 1


# ════════════════════════════════════════════════════════════
# MAIN
# ════════════════════════════════════════════════════════════
def main():
    args = parse_args()

    if args.only:
        selected = [p.strip() for p in args.only.split(",") if p.strip()]
        invalid = [p for p in selected if p not in PHASES]
        if invalid:
            print(f"✗ Unknown phase(s): {', '.join(invalid)}")
            print(f"  Valid phases: {', '.join(PHASES)}")
            sys.exit(1)
    else:
        selected = PHASES

    connect = boto3.client("connect", region_name=args.region)
    s3 = boto3.client("s3", region_name=args.region)

    if args.dry_run:
        print("=" * 60)
        print("DRY RUN — no changes will be made")
        print("=" * 60)

    print(f"\nSource: {args.source_instance_id}")
    print(f"Target: {args.target_instance_id}")
    print(f"Region: {args.region}")
    print(f"Phases: {', '.join(selected)}")

    # Dependency order matters: HoO before Queues, Queues before Quick Connects
    # and Routing Profiles.
    if "hoo" in selected:
        migrate_hours_of_operation(connect, args)
    if "agent-status" in selected:
        migrate_agent_statuses(connect, args)
    if "prompts" in selected:
        migrate_prompts(connect, s3, args)
    if "queues" in selected:
        migrate_queues(connect, args)
    if "quick-connects" in selected:
        migrate_quick_connects(connect, args)
    if "routing-profiles" in selected:
        migrate_routing_profiles(connect, args)

    print_header("SUMMARY")
    print(f"Created: {stats['created']}")
    print(f"Skipped: {stats['skipped']}")
    print(f"Failed:  {stats['failed']}")

    if stats["failed"]:
        print("\n⚠ Review failures above. Common causes:")
        print("  - A dependent resource is missing on the target")
        print("  - Prompt audio could not be staged in S3")

    print("\nNext: run the module/flow scripts to create shells and rewrite ID references.")


if __name__ == "__main__":
    main()