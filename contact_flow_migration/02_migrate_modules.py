#!/usr/bin/env python3
"""
11_migrate_modules.py — Migrate contact flow modules from a source Amazon Connect
instance to a target instance.

Purpose:
--------
Creates the modules missing on the target, matched by name, so that later scripts
can rewrite their content.

Key Behavior:
-------------
- Creates missing modules on target (matched by name)
- If creation fails because a referenced resource is not found, retries with
  placeholder content so that the module still exists and gets an ID
- --update overwrites existing target modules with the source content

Why the placeholder fallback exists:
------------------------------------
Unlike create_contact_flow, create_contact_flow_module has no SAVED status, so
Connect validates the content on create. A module referencing a prompt or queue
that is not on the target yet fails with an error like

    PromptEntity@0 not found    /    SkillEntity@1f not found

The retry creates the module with a minimal valid body instead. That is enough,
because the point of this step is to get a module ID on the target — the real
content is written later by the content update script, which rewrites every
resource reference at once.

So a module created with a placeholder is not a failure. It is the normal path
for any module whose dependencies are not migrated yet.

About --update:
---------------
--update only replaces the instance ID in the content. It does NOT remap queue,
prompt, Lambda, Lex or AI agent IDs, which differ between instances. For a real
content migration use the content update script, which builds a full
{source ID: target ID} mapping by resource name. --update is only useful when
the two instances already share every referenced resource ID.

Ordering:
---------
This script and the flow migration script can run in either order. Flows are
created with Status="SAVED", which skips validation, and modules fall back to a
placeholder, so neither needs the other to exist first.

--dry-run lists what would be created without calling create_contact_flow_module.
The script exits with status 1 if any module could not be created at all.

Usage:
  python3 11_migrate_modules.py -s <SOURCE_ID> -t <TARGET_ID> -r us-west-2 --dry-run
  python3 11_migrate_modules.py -s <SOURCE_ID> -t <TARGET_ID> -r us-west-2
"""

import argparse
import json
import sys
import time

import boto3

# Minimal valid module content — just an EndFlowModuleExecution action
PLACEHOLDER_CONTENT = json.dumps({
    "Version": "2019-10-30",
    "StartAction": "placeholder-start",
    "Actions": [
        {
            "Identifier": "placeholder-start",
            "Type": "EndFlowModuleExecution",
            "Parameters": {},
            "Transitions": {}
        }
    ]
})


def parse_args():
    p = argparse.ArgumentParser(
        description="Migrate contact flow modules between Connect instances"
    )
    p.add_argument("-s", "--source-instance-id", required=True, help="Source Connect instance ID")
    p.add_argument("-t", "--target-instance-id", required=True, help="Target Connect instance ID")
    p.add_argument("-r", "--region", required=True, help="AWS region (e.g. us-west-2)")
    p.add_argument("--update", action="store_true",
                   help="Also overwrite existing target modules with source content "
                        "(instance ID only — see the note in this file's header)")
    p.add_argument("--dry-run", action="store_true",
                   help="Preview without creating or updating anything")
    return p.parse_args()


def list_modules(connect, instance_id):
    modules = []
    kwargs = {"InstanceId": instance_id, "MaxResults": 100}
    while True:
        resp = connect.list_contact_flow_modules(**kwargs)
        modules.extend(resp.get("ContactFlowModulesSummaryList", []))
        token = resp.get("NextToken")
        if not token:
            break
        kwargs["NextToken"] = token
    return modules


def get_module_content(connect, instance_id, module_id):
    resp = connect.describe_contact_flow_module(
        InstanceId=instance_id,
        ContactFlowModuleId=module_id,
    )
    return resp.get("ContactFlowModule", {})


def rewrite_content(content_str, source_id, target_id):
    return content_str.replace(source_id, target_id)


def main():
    args = parse_args()
    connect = boto3.client("connect", region_name=args.region)

    SOURCE_INSTANCE_ID = args.source_instance_id
    TARGET_INSTANCE_ID = args.target_instance_id

    if args.dry_run:
        print("=== DRY RUN ===\n")
    if args.update:
        print("=== UPDATE MODE: will overwrite existing modules ===\n")

    print("Listing source modules...")
    source_modules = list_modules(connect, SOURCE_INSTANCE_ID)
    print(f"Source: {len(source_modules)} module(s)")

    print("Listing target modules...")
    target_modules = list_modules(connect, TARGET_INSTANCE_ID)
    print(f"Target: {len(target_modules)} module(s)\n")

    target_by_name = {m["Name"]: m for m in target_modules}

    missing = []
    existing = []
    for m in source_modules:
        if m["Name"] in target_by_name:
            existing.append(m)
        else:
            missing.append(m)

    print(f"Missing on target: {len(missing)}")
    print(f"Already on target: {len(existing)}")
    print()

    created = 0
    placeholder = 0
    failed = 0

    # ── Create missing modules ──
    if missing:
        print("━" * 60)
        print("CREATING MISSING MODULES")
        print("━" * 60)

        for m in missing:
            name = m["Name"]
            module_id = m["Id"]
            print(f"\n  → {name}")

            detail = get_module_content(connect, SOURCE_INSTANCE_ID, module_id)
            content = detail.get("Content", "")
            description = detail.get("Description", "")
            tags = detail.get("Tags", {})

            content = rewrite_content(content, SOURCE_INSTANCE_ID, TARGET_INSTANCE_ID)

            if args.dry_run:
                print("    Would create")
                created += 1
                continue

            # Attempt 1: create with real content
            try:
                resp = connect.create_contact_flow_module(
                    InstanceId=TARGET_INSTANCE_ID,
                    Name=name,
                    Description=description,
                    Content=content,
                    Tags=tags,
                )
                print(f"    ✓ Created with full content (ID: {resp.get('Id', '?')})")
                created += 1
                time.sleep(0.3)
                continue
            except Exception as e1:
                print(f"    ⚠ Full content failed: {e1}")

            # Attempt 2: create with placeholder content
            try:
                resp = connect.create_contact_flow_module(
                    InstanceId=TARGET_INSTANCE_ID,
                    Name=name,
                    Description=description or "(placeholder — needs content update)",
                    Content=PLACEHOLDER_CONTENT,
                    Tags=tags,
                )
                print(f"    ✓ Created with PLACEHOLDER (ID: {resp.get('Id', '?')})"
                      f" — content comes from the update script")
                placeholder += 1
                time.sleep(0.3)
            except Exception as e2:
                print(f"    ✗ Placeholder also failed: {e2}")
                failed += 1

        print(f"\nFull content: {created}, Placeholder: {placeholder}, Failed: {failed}")

    # ── Update existing modules ──
    updated = 0
    update_failed = 0

    if args.update and existing:
        print("\n" + "━" * 60)
        print("UPDATING EXISTING MODULES")
        print("━" * 60)

        for m in existing:
            name = m["Name"]
            source_id = m["Id"]
            target_id = target_by_name[name]["Id"]
            print(f"\n  → {name}")

            detail = get_module_content(connect, SOURCE_INSTANCE_ID, source_id)
            content = detail.get("Content", "")
            content = rewrite_content(content, SOURCE_INSTANCE_ID, TARGET_INSTANCE_ID)

            if args.dry_run:
                print(f"    Would update (target ID: {target_id})")
                updated += 1
                continue

            try:
                connect.update_contact_flow_module_content(
                    InstanceId=TARGET_INSTANCE_ID,
                    ContactFlowModuleId=target_id,
                    Content=content,
                )
                print("    ✓ Updated")
                updated += 1
                time.sleep(0.3)
            except Exception as e:
                print(f"    ✗ Failed: {e}")
                update_failed += 1

        print(f"\nUpdated: {updated}, Failed: {update_failed}")

    # ── Summary ──
    print("\n" + "━" * 60)
    print("SUMMARY")
    print("━" * 60)
    print(f"Created with full content: {created}")
    print(f"Created with placeholder:  {placeholder}")
    print(f"Could not create:          {failed}")
    if args.update:
        print(f"Updated existing:          {updated}")
        print(f"Update failed:             {update_failed}")

    if placeholder:
        print(f"\n{placeholder} module(s) hold placeholder content. Run the content")
        print("update script to write their real content once the resources they")
        print("reference exist on the target.")

    if failed or update_failed:
        sys.exit(1)


if __name__ == "__main__":
    main()