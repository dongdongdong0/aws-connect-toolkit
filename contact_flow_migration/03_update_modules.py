#!/usr/bin/env python3
"""
12_update_modules.py — Rewrite contact flow module content on the target instance
so every resource reference points at the target's own IDs.

What this does:
---------------
This is a full content rewrite, not a patch. For each module it reads the source
module's content (which is the correct, authoritative version), replaces every
source resource ID with the matching target ID, and writes the whole body back
with update_contact_flow_module_content.

It is called "update" because that is the API name. The operation is a complete
overwrite of the module body.

How the ID mapping is built:
----------------------------
A module's content JSON holds the raw IDs and ARNs of everything it references,
and those IDs differ between instances. So the script lists every resource type
on BOTH instances, matches them by name, and builds one flat table:

    {source ID: target ID, source ARN: target ARN, ...}

Resource types covered: Queues, Prompts, Hours of Operation, Contact Flows,
Contact Flow Modules, Quick Connects, Routing Profiles, plus the Q in Connect
assistant and its AI agents when --source-assistant-id / --target-assistant-id
are given.

Then the whole content string is rewritten: the instance ID first, then every
mapping key. Keys are applied longest-first so that a shorter ID can never
partially overwrite a longer string that contains it.

Not in the mapping, and not needed:
-----------------------------------
Lambda function ARNs and Lex bot alias ARNs do not contain the instance ID, so
they are identical on both instances and need no rewriting. They do have to be
ASSOCIATED with the target instance — see the Lambda and Lex sync scripts.

The pre-check:
--------------
Before writing, the script scans the rewritten content for IDs that exist on the
source but have no match on the target. If it finds any, the module is skipped
and the missing resources are named. Without this you get an opaque API error
such as "PromptEntity@0 not found" with no indication of which prompt.

Skipped modules usually mean a dependency has not been migrated yet. Migrate it,
then run this script again — each run can unlock more modules. Use --no-precheck
to write anyway and let the API decide.

Usage:
  python3 12_update_modules.py -s <SOURCE_ID> -t <TARGET_ID> -r us-west-2 --dry-run
  python3 12_update_modules.py -s <SOURCE_ID> -t <TARGET_ID> -r us-west-2 \
      --source-assistant-id <SRC_ASSISTANT> --target-assistant-id <TGT_ASSISTANT>
  python3 12_update_modules.py -s <SOURCE_ID> -t <TARGET_ID> -r us-west-2 \
      --name "My Module Name"
"""

import argparse
import sys
import time

import boto3


def parse_args():
    p = argparse.ArgumentParser(
        description="Rewrite module content on the target instance with target resource IDs"
    )
    p.add_argument("-s", "--source-instance-id", required=True, help="Source Connect instance ID")
    p.add_argument("-t", "--target-instance-id", required=True, help="Target Connect instance ID")
    p.add_argument("-r", "--region", required=True, help="AWS region (e.g. us-west-2)")

    p.add_argument("--source-assistant-id",
                   help="Source Q in Connect assistant ID (optional; enables AI agent mapping)")
    p.add_argument("--target-assistant-id",
                   help="Target Q in Connect assistant ID (optional; enables AI agent mapping)")

    scope = p.add_mutually_exclusive_group()
    scope.add_argument("--name", help="Update only the module with this exact name")
    scope.add_argument("--filter", dest="name_filter",
                       help="Update only modules whose name contains this text "
                            "(case-insensitive)")

    p.add_argument("--no-precheck", action="store_true",
                   help="Write even when unmapped references remain, and let the API decide")
    p.add_argument("--dry-run", action="store_true", help="Preview without writing anything")
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


def build_mappings(connect, qconnect, args, account_id):
    """Build the ID mapping table by matching resources on both instances by name.

    Returns:
      mapping:         {source ID or ARN: target ID or ARN} for everything matched
      source_only_ids: {source ID: (resource type, name)} for everything unmatched,
                       used by the pre-check to explain why a module was skipped
    """
    mapping = {}
    source_only_ids = {}

    # ── Q in Connect assistant and its AI agents ──
    if args.source_assistant_id and args.target_assistant_id:
        mapping[args.source_assistant_id] = args.target_assistant_id
        mapping[f"arn:aws:wisdom:{args.region}:{account_id}:assistant/{args.source_assistant_id}"] = \
            f"arn:aws:wisdom:{args.region}:{account_id}:assistant/{args.target_assistant_id}"
        print("  Q in Connect assistant: mapped")

        # AI agents are listed per origin. SYSTEM agents are account-level and often
        # share IDs across assistants; CUSTOMER agents are per-assistant and differ.
        ai_matched = 0
        for origin in ["SYSTEM", "CUSTOMER"]:
            try:
                src_agents = qconnect.list_ai_agents(
                    assistantId=args.source_assistant_id, origin=origin
                ).get("aiAgentSummaries", [])
                tgt_agents = qconnect.list_ai_agents(
                    assistantId=args.target_assistant_id, origin=origin
                ).get("aiAgentSummaries", [])

                tgt_by_name = {a["name"]: a for a in tgt_agents}

                for src_a in src_agents:
                    tgt_a = tgt_by_name.get(src_a["name"])
                    if tgt_a:
                        if src_a["aiAgentId"] != tgt_a["aiAgentId"]:
                            mapping[src_a["aiAgentId"]] = tgt_a["aiAgentId"]
                        src_arn = src_a.get("aiAgentArn", "")
                        tgt_arn = tgt_a.get("aiAgentArn", "")
                        if src_arn and tgt_arn and src_arn != tgt_arn:
                            mapping[src_arn] = tgt_arn
                        ai_matched += 1
                    else:
                        source_only_ids[src_a["aiAgentId"]] = ("AI Agent", src_a["name"])
            except Exception as e:
                print(f"  ⚠ AI agents ({origin}): {e}")

        print(f"  AI agents: {ai_matched} matched")
    else:
        print("  Q in Connect: skipped (no assistant IDs given)")

    # ── Connect resources ──
    resource_types = [
        ("Queues", connect.list_queues, "QueueSummaryList"),
        ("Prompts", connect.list_prompts, "PromptSummaryList"),
        ("Hours of Operation", connect.list_hours_of_operations, "HoursOfOperationSummaryList"),
        ("Contact Flows", connect.list_contact_flows, "ContactFlowSummaryList"),
        ("Modules", connect.list_contact_flow_modules, "ContactFlowModulesSummaryList"),
        ("Quick Connects", connect.list_quick_connects, "QuickConnectSummaryList"),
        ("Routing Profiles", connect.list_routing_profiles, "RoutingProfileSummaryList"),
    ]

    for label, method, list_key in resource_types:
        print(f"  {label}...", end=" ", flush=True)

        source_items = paginate(method, args.source_instance_id, list_key)
        target_items = paginate(method, args.target_instance_id, list_key)

        # AGENT-type queues have no Name field, hence .get("Name")
        src_by_name = {i["Name"]: i for i in source_items if i.get("Name")}
        tgt_by_name = {i["Name"]: i for i in target_items if i.get("Name")}

        matched = unmatched = 0
        for name, src in src_by_name.items():
            tgt = tgt_by_name.get(name)
            if tgt:
                if src["Id"] != tgt["Id"]:
                    mapping[src["Id"]] = tgt["Id"]
                src_arn = src.get("Arn", "")
                tgt_arn = tgt.get("Arn", "")
                if src_arn and tgt_arn and src_arn != tgt_arn:
                    mapping[src_arn] = tgt_arn
                matched += 1
            else:
                source_only_ids[src["Id"]] = (label, name)
                unmatched += 1

        print(f"{matched} matched, {unmatched} unmatched")

    return mapping, source_only_ids


def apply_mapping(content_str, mapping, source_instance_id, target_instance_id):
    """Replace the instance ID and every mapped resource ID/ARN in the content.

    Keys are applied longest-first: if a short ID happens to be a substring of a
    longer ARN that is also being replaced, doing the long one first prevents the
    short replacement from corrupting it.
    """
    result = content_str.replace(source_instance_id, target_instance_id)

    replacements = 0
    for src_id in sorted(mapping.keys(), key=len, reverse=True):
        if src_id in result:
            result = result.replace(src_id, mapping[src_id])
            replacements += 1

    return result, replacements


def check_unmapped(content_str, source_only_ids):
    """Return [(resource type, name, id)] for source-only IDs still in the content."""
    found = []
    for src_id, (rtype, name) in source_only_ids.items():
        if src_id in content_str:
            found.append((rtype, name, src_id))
    return found


def main():
    args = parse_args()

    if bool(args.source_assistant_id) != bool(args.target_assistant_id):
        print("✗ --source-assistant-id and --target-assistant-id must be given together")
        sys.exit(1)

    connect = boto3.client("connect", region_name=args.region)
    qconnect = boto3.client("qconnect", region_name=args.region)
    account_id = boto3.client("sts").get_caller_identity()["Account"]

    if args.dry_run:
        print("=" * 60)
        print("DRY RUN — nothing will be written")
        print("=" * 60)

    print(f"\nSource: {args.source_instance_id}")
    print(f"Target: {args.target_instance_id}\n")

    # ── Step 1: build the mapping table ──
    print("Building ID mapping table...")
    mapping, source_only_ids = build_mappings(connect, qconnect, args, account_id)
    print(f"\nTotal mappings: {len(mapping)}")
    print(f"Source-only IDs (potential blockers): {len(source_only_ids)}")

    # ── Step 2: pick the modules to update ──
    source_modules = paginate(connect.list_contact_flow_modules, args.source_instance_id,
                              "ContactFlowModulesSummaryList")
    target_modules = paginate(connect.list_contact_flow_modules, args.target_instance_id,
                              "ContactFlowModulesSummaryList")
    target_by_name = {m["Name"]: m for m in target_modules if m.get("Name")}

    selected = [m for m in source_modules
                if m.get("Name") and m["Name"] in target_by_name]

    if args.name:
        selected = [m for m in selected if m["Name"] == args.name]
        if not selected:
            print(f"\n✗ No module named '{args.name}' exists on both instances")
            sys.exit(1)
    elif args.name_filter:
        needle = args.name_filter.lower()
        selected = [m for m in selected if needle in m["Name"].lower()]

    print(f"\n{'━' * 60}")
    print(f"MODULES TO UPDATE: {len(selected)}")
    print("━" * 60)

    if not selected:
        print("Nothing to do.")
        return

    # ── Step 3: rewrite each module ──
    updated = skipped = failed = 0

    for src_mod in selected:
        name = src_mod["Name"]
        tgt_mod = target_by_name[name]
        print(f"\n  → {name}")

        try:
            detail = connect.describe_contact_flow_module(
                InstanceId=args.source_instance_id,
                ContactFlowModuleId=src_mod["Id"],
            )["ContactFlowModule"]
            content = detail.get("Content", "")
        except Exception as e:
            print(f"    ✗ Could not read source: {e}")
            failed += 1
            continue

        if not content:
            print("    Skipped (empty content)")
            skipped += 1
            continue

        new_content, replacements = apply_mapping(
            content, mapping, args.source_instance_id, args.target_instance_id
        )
        print(f"    {replacements} ID replacement(s)")

        if not args.no_precheck:
            unmapped = check_unmapped(new_content, source_only_ids)
            if unmapped:
                print(f"    ⏭ SKIPPED — {len(unmapped)} unmapped reference(s):")
                for rtype, rname, _ in unmapped:
                    print(f"      - {rtype}: {rname}")
                skipped += 1
                continue

        if args.dry_run:
            print(f"    Would update (target ID: {tgt_mod['Id']})")
            updated += 1
            continue

        try:
            connect.update_contact_flow_module_content(
                InstanceId=args.target_instance_id,
                ContactFlowModuleId=tgt_mod["Id"],
                Content=new_content,
            )
            print("    ✓ Updated")
            updated += 1
            time.sleep(0.3)
        except Exception as e:
            print(f"    ✗ Failed: {e}")
            failed += 1

    # ── Summary ──
    print(f"\n{'━' * 60}")
    print("SUMMARY")
    print("━" * 60)
    print(f"Updated: {updated}")
    print(f"Skipped: {skipped}")
    print(f"Failed:  {failed}")

    if skipped:
        print("\nSkipped modules reference resources that are not on the target yet.")
        print("Migrate those, then run this script again.")

    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()