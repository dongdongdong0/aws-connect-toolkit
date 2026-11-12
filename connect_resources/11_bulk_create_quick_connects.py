#!/usr/bin/env python3
"""
bulk_create_quick_connects.py — Create QUEUE-type quick connects in bulk on one
Amazon Connect instance, from a list you supply.

This is not an instance-to-instance migration. It creates quick connects from a
spec, which is what you want when the list comes from a spreadsheet or a naming
convention rather than from another instance.

How it works:
  1. Lists the instance's STANDARD queues and builds a {queue name: queue ID} map,
     because create_quick_connect needs the ID but your list has names.
  2. Finds the instance's built-in "Default queue transfer" flow. Every QUEUE-type
     quick connect needs a contact flow to hand the call off to the queue, and all
     of them share this same flow.
  3. For each entry, resolves the queue by name and creates the quick connect.
     Entries that already exist are skipped, so the script is safe to re-run.

Input — three ways. If no flag is given it falls back to the QUICK_CONNECTS
list defined near the top of this file, so you can keep the spec in the script
instead of carrying a separate CSV around:

  (no flag)         Uses the QUICK_CONNECTS list below — edit it in place
  --csv FILE        Columns: name, queue, description (description optional)
  --queues a,b,c    Shorthand for when the quick connect takes the queue's own name

Usage:
  # use the QUICK_CONNECTS list in this file
  python3 bulk_create_quick_connects.py -i <INSTANCE_ID> -r ca-central-1 --dry-run

  # read the list from a CSV instead
  python3 bulk_create_quick_connects.py -i <INSTANCE_ID> -r ca-central-1 \\
      --csv quick_connects.csv

  # queue names only; each quick connect takes its queue's name
  python3 bulk_create_quick_connects.py -i <INSTANCE_ID> -r ca-central-1 \\
      --queues HD_GN_FR,HD_PC_FR,HD_NB_FR

Exits non-zero if any entry failed, so it can be used in a pipeline.
"""

import argparse
import csv
import sys
import time

import boto3

DEFAULT_TRANSFER_FLOW = "Default queue transfer"

# ════════════════════════════════════════════════════════════
# YOUR LIST — edit this, or override it with --csv / --queues
#
# One tuple per quick connect:
#   (quick connect name, description, queue name on the instance)
#
# The queue name is looked up on the instance to get its ID, so it has to match
# an existing STANDARD queue exactly. The quick connect name is usually the same
# as the queue name, but it does not have to be.
# ════════════════════════════════════════════════════════════
QUICK_CONNECTS = [
    ("HD_GN_FR", "Quick Connect for HD & ISQ FR for technical assistance", "HD_GN_FR"),
    ("HD_PC_FR", "Quick Connect for HD & ISQ FR for technical assistance", "HD_PC_FR"),
    ("HD_NB_FR", "Quick Connect for HD_NB_FR", "HD_NB_FR"),
]


def parse_args():
    p = argparse.ArgumentParser(
        description="Bulk-create QUEUE-type quick connects on a Connect instance"
    )
    p.add_argument("-i", "--instance-id", required=True, help="Connect instance ID")
    p.add_argument("-r", "--region", required=True, help="AWS region (e.g. ca-central-1)")

    # Neither flag is required — without one, the QUICK_CONNECTS list is used
    src = p.add_mutually_exclusive_group()
    src.add_argument("--csv", help="CSV file with columns: name, queue, description")
    src.add_argument("--queues",
                     help="Comma-separated queue names; each quick connect takes the "
                          "queue's own name")

    p.add_argument("--description-template", default="Quick Connect for {queue}",
                   help="Description used with --queues. {queue} is substituted.")
    p.add_argument("--flow-name", default=DEFAULT_TRANSFER_FLOW,
                   help=f"Contact flow to hand off to (default: '{DEFAULT_TRANSFER_FLOW}')")
    p.add_argument("--dry-run", action="store_true", help="Preview without creating anything")
    return p.parse_args()


def load_entries(args):
    """Return a list of (quick connect name, description, queue name) tuples.

    Source order: --csv, then --queues, then the QUICK_CONNECTS list in this file.
    """
    entries = []

    if args.csv:
        try:
            with open(args.csv, newline="", encoding="utf-8-sig") as fh:
                reader = csv.DictReader(fh)
                if not reader.fieldnames:
                    print(f"✗ {args.csv} is empty")
                    sys.exit(1)

                cols = {c.strip().lower() for c in reader.fieldnames}
                for required in ("name", "queue"):
                    if required not in cols:
                        print(f"✗ {args.csv} is missing a '{required}' column")
                        print(f"  Found: {', '.join(reader.fieldnames)}")
                        sys.exit(1)

                for row_num, row in enumerate(reader, start=2):
                    row = {(k or "").strip().lower(): (v or "").strip()
                           for k, v in row.items()}
                    name = row.get("name", "")
                    queue = row.get("queue", "")
                    if not name or not queue:
                        print(f"  ⚠ Row {row_num}: missing name or queue — skipped")
                        continue
                    entries.append((name, row.get("description", ""), queue))
        except FileNotFoundError:
            print(f"✗ File not found: {args.csv}")
            sys.exit(1)
    elif args.queues:
        for queue in [q.strip() for q in args.queues.split(",") if q.strip()]:
            description = args.description_template.format(queue=queue)
            entries.append((queue, description, queue))
    else:
        # No flag given — fall back to the list defined in this file
        print("Using the QUICK_CONNECTS list defined in this script")
        entries = list(QUICK_CONNECTS)

    if not entries:
        print("✗ No usable entries found")
        if not args.csv and not args.queues:
            print("  The QUICK_CONNECTS list in this script is empty. Edit it, or")
            print("  pass --csv / --queues.")
        sys.exit(1)

    return entries


def get_standard_queues(connect, instance_id):
    """Map {queue name: queue ID} for STANDARD queues only.

    AGENT-type queues are created automatically per user and have no Name field,
    so an explicit STANDARD allowlist is safer than excluding known types.
    """
    queues = {}
    paginator = connect.get_paginator("list_queues")
    for page in paginator.paginate(InstanceId=instance_id):
        for q in page.get("QueueSummaryList", []):
            if q.get("QueueType") != "STANDARD":
                continue
            name = q.get("Name")
            if name:
                queues[name] = q["Id"]
    return queues


def get_flow_id(connect, instance_id, flow_name):
    """Find a contact flow by name, ignoring case and surrounding whitespace."""
    wanted = flow_name.strip().lower()
    paginator = connect.get_paginator("list_contact_flows")
    for page in paginator.paginate(InstanceId=instance_id):
        for flow in page.get("ContactFlowSummaryList", []):
            if flow.get("Name", "").strip().lower() == wanted:
                return flow["Id"], flow["Name"]
    return None, None


def get_existing_quick_connects(connect, instance_id):
    """Names of quick connects already on the instance, so re-runs skip them."""
    names = set()
    paginator = connect.get_paginator("list_quick_connects")
    for page in paginator.paginate(InstanceId=instance_id):
        for qc in page.get("QuickConnectSummaryList", []):
            if qc.get("Name"):
                names.add(qc["Name"])
    return names


def main():
    args = parse_args()
    connect = boto3.client("connect", region_name=args.region)

    if args.dry_run:
        print("=== DRY RUN — nothing will be created ===")

    print(f"\nInstance: {args.instance_id}")
    print(f"Region:   {args.region}\n")

    entries = load_entries(args)
    print(f"Entries to process: {len(entries)}")

    # Resolve the hand-off flow once — every quick connect uses the same one
    flow_id, matched_name = get_flow_id(connect, args.instance_id, args.flow_name)
    if not flow_id:
        print(f"\n✗ Contact flow not found: '{args.flow_name}'")
        print("  Check the name, or pass --flow-name with the one you want.")
        sys.exit(1)
    print(f"Hand-off flow: {matched_name} ({flow_id})")

    queues = get_standard_queues(connect, args.instance_id)
    print(f"Standard queues on instance: {len(queues)}")

    existing = get_existing_quick_connects(connect, args.instance_id)
    print(f"Existing quick connects: {len(existing)}")

    created = skipped = failed = 0

    for qc_name, qc_desc, queue_name in entries:
        print(f"\n  → {qc_name}")

        if qc_name in existing:
            print("    Already exists — skipping")
            skipped += 1
            continue

        queue_id = queues.get(queue_name)
        if not queue_id:
            print(f"    ✗ Queue not found: {queue_name}")
            failed += 1
            continue

        if args.dry_run:
            print(f"    Would create → queue '{queue_name}'")
            created += 1
            continue

        try:
            kwargs = {
                "InstanceId": args.instance_id,
                "Name": qc_name,
                "QuickConnectConfig": {
                    "QuickConnectType": "QUEUE",
                    "QueueConfig": {"QueueId": queue_id, "ContactFlowId": flow_id},
                },
            }
            if qc_desc:
                kwargs["Description"] = qc_desc

            resp = connect.create_quick_connect(**kwargs)
            print(f"    ✓ Created (ID: {resp['QuickConnectId']})")
            created += 1
            time.sleep(0.4)
        except Exception as e:
            print(f"    ✗ Failed: {e}")
            failed += 1

    print(f"\n{'━' * 60}")
    print(f"Created: {created}, Skipped: {skipped}, Failed: {failed}")
    print("━" * 60)

    if failed:
        print("\nFailures are usually a queue name that does not exist on this instance,")
        print("or a typo in the CSV. Fix those rows and re-run — existing quick")
        print("connects are skipped, so nothing is created twice.")
        sys.exit(1)


if __name__ == "__main__":
    main()