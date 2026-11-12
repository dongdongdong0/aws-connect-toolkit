#!/usr/bin/env python3
"""
13_update_flows.py — Rewrite contact flow content on the target instance so every
resource reference points at the target's own IDs.

What this does:
---------------
This is a full content rewrite, not a patch. For each flow it reads the source
flow's content (which is the correct, authoritative version), replaces every
source resource ID with the matching target ID, and writes the whole body back
with update_contact_flow_content.

It is called "update" because that is the API name. The operation is a complete
overwrite of the flow body.

How the ID mapping is built:
----------------------------
A flow's content JSON holds the raw IDs and ARNs of everything it references,
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

Because the mapping is built from every resource type at once, anything a flow
references gets remapped — there is no per-block or per-resource-type handling
to maintain.

Not in the mapping, and not needed:
-----------------------------------
Lambda function ARNs and Lex bot alias ARNs do not contain the instance ID, so
they are identical on both instances and need no rewriting. They do have to be
ASSOCIATED with the target instance — see the Lambda and Lex sync scripts.

Scope — pick one:
-----------------
  (nothing)           Every flow that exists on both instances by name
  --filter TEXT       Only flows whose name contains TEXT (case-insensitive)
  --name "EXACT"      Only the flow with this exact name

Flows whose name starts with "Default " are skipped unless --name asks for one
by name, because those are Connect's own system flows.

The pre-check:
--------------
Before writing, the script scans the rewritten content for IDs that exist on the
source but have no match on the target. If it finds any, the flow is skipped and
the missing resources are named. Without this the API fails with an opaque
InvalidContactFlowException that does not say which resource is at fault.

This makes the script safe to run repeatedly, and repeat runs are expected: a
flow that references another flow can only be written once that other flow has
been written, so each pass unlocks more flows. Run it until nothing is skipped.

A flow in SAVED state can be referenced by other flows — it does not need to be
published first.

Use --no-precheck to write anyway and let the API decide.

Usage:
  python3 13_update_flows.py -s <SOURCE_ID> -t <TARGET_ID> -r us-west-2 --dry-run
  python3 13_update_flows.py -s <SOURCE_ID> -t <TARGET_ID> -r us-west-2 --filter PRODUCTION
  python3 13_update_flows.py -s <SOURCE_ID> -t <TARGET_ID> -r us-west-2 \
      --name "CS_ResidentSupport_ GetAccountNumber (RSV2)"
"""

import argparse
import sys
import time

import boto3

# Connect's own system flows — not overwritten unless asked for by exact name
SYSTEM_FLOW_PREFIXES = ("Default ",)


def parse_args():
    p = argparse.ArgumentParser(
        description="Rewrite contact flow content on the target instance with target resource IDs"
    )
    p.add_argument("-s", "--source-instance-id", required=True, help="Source Connect instance ID")
    p.add_argument("-t", "--target-instance-id", required=True, help="Target Connect instance ID")
    p.add_argument("-r", "--region", required=True, help="AWS region (e.g. us-west-2)")

    p.add_argument("--source-assistant-id",
                   help="Source Q in Connect assistant ID (optional; enables AI agent mapping)")
    p.add_argument("--target-assistant-id",
                   help="Target Q in Connect assistant ID (optional; enables AI agent mapping)")

    scope = p.add_mutually_exclusive_group()
    scope.add_argument("--name", help="Update only the flow with this exact name")
    scope.add_argument("--filter", dest="name_filter",
                       help="Update only flows whose name contains this text "
                            "(case-insensitive), e.g. PRODUCTION")

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
                       used by the pre-check to explain why a flow was skipped
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

    # ── Step 2: pick the flows to update ──
    source_flows = paginate(connect.list_contact_flows, args.source_instance_id,
                            "ContactFlowSummaryList")
    target_flows = paginate(connect.list_contact_flows, args.target_instance_id,
                            "ContactFlowSummaryList")
    target_by_name = {f["Name"]: f for f in target_flows if f.get("Name")}

    # A flow can only be rewritten if it exists on the target
    selected = [f for f in source_flows
                if f.get("Name") and f["Name"] in target_by_name]

    if args.name:
        selected = [f for f in selected if f["Name"] == args.name]
        if not selected:
            print(f"\n✗ No flow named '{args.name}' exists on both instances")
            sys.exit(1)
    else:
        # Leave Connect's own system flows alone unless asked for by exact name
        selected = [f for f in selected
                    if not f["Name"].startswith(SYSTEM_FLOW_PREFIXES)]
        if args.name_filter:
            needle = args.name_filter.lower()
            selected = [f for f in selected if needle in f["Name"].lower()]

    scope = (f"name == '{args.name}'" if args.name
             else f"name contains '{args.name_filter}'" if args.name_filter
             else "all flows present on both instances")

    print(f"\n{'━' * 60}")
    print(f"FLOWS TO UPDATE: {len(selected)}  ({scope})")
    print("━" * 60)

    if not selected:
        print("Nothing to do.")
        return

    # ── Step 3: rewrite each flow ──
    updated = skipped = failed = 0

    for src_flow in selected:
        name = src_flow["Name"]
        tgt_flow = target_by_name[name]
        print(f"\n  → {name}")

        try:
            detail = connect.describe_contact_flow(
                InstanceId=args.source_instance_id,
                ContactFlowId=src_flow["Id"],
            )["ContactFlow"]
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
            print(f"    Would update (target ID: {tgt_flow['Id']})")
            updated += 1
            continue

        try:
            connect.update_contact_flow_content(
                InstanceId=args.target_instance_id,
                ContactFlowId=tgt_flow["Id"],
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
        print("\nRe-run this script. Flows that reference other flows can only be")
        print("written once those flows have been written, so each pass unlocks more.")
        print("If a flow is still skipped after a second run, the resource it names")
        print("genuinely does not exist on the target yet.")

    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()