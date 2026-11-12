#!/usr/bin/env python3
"""
migrate_cases.py
================
Migrate Amazon Connect Cases resources between two Connect instances:

    1. Fields       (custom fields + SingleSelect options)
    2. Layouts      (field references remapped to target field IDs)
    3. Templates    (requiredFields + layoutConfiguration remapped)

Both instances must already have Cases enabled (a Cases domain must exist).
Resources are matched by NAME, so re-running is safe — anything already
present in the target is skipped and mapped, never duplicated.

-------------------------------------------------------------------------
PARAMETERS
-------------------------------------------------------------------------
  --source-instance   REQUIRED  Source Connect instance ID or full ARN
  --target-instance   REQUIRED  Target Connect instance ID or full ARN
  --region            OPTIONAL  AWS region (default: inferred from the ARN,
                                else AWS_REGION env var, else ca-central-1)
  --output            OPTIONAL  Path for the ID mapping JSON
                                (default: cases_id_mapping.json)
  --dry-run           OPTIONAL  Preview only; makes no changes

Instance values accept either form:
    <SOURCE_INSTANCE_ID>
    arn:aws:connect:<REGION>:<ACCOUNT_ID>:instance/<SOURCE_INSTANCE_ID>

-------------------------------------------------------------------------
USAGE
-------------------------------------------------------------------------
  # Preview first
  python3 migrate_cases.py \
      --source-instance <SOURCE_INSTANCE_ID> \
      --target-instance <TARGET_INSTANCE_ID> \
      --region ca-central-1 \
      --dry-run

  # Then run for real
  python3 migrate_cases.py \
      --source-instance <SOURCE_INSTANCE_ID> \
      --target-instance <TARGET_INSTANCE_ID> \
      --region ca-central-1

  # ARNs work too, and region is picked up from them
  python3 migrate_cases.py \
      --source-instance arn:aws:connect:<REGION>:<ACCOUNT_ID>:instance/<SOURCE_INSTANCE_ID> \
      --target-instance arn:aws:connect:<REGION>:<ACCOUNT_ID>:instance/<TARGET_INSTANCE_ID>

-------------------------------------------------------------------------
IAM PERMISSIONS REQUIRED
-------------------------------------------------------------------------
  connect:ListIntegrationAssociations
  cases:ListDomains
  cases:ListFields         cases:BatchGetField        cases:CreateField
  cases:ListFieldOptions   cases:BatchPutFieldOptions
  cases:ListLayouts        cases:GetLayout            cases:CreateLayout
  cases:ListTemplates      cases:GetTemplate          cases:CreateTemplate

-------------------------------------------------------------------------
OUTPUT
-------------------------------------------------------------------------
Writes a JSON file holding source->target ID maps for fields and layouts.
Keep it: other migrations (flows that reference case fields, etc.) need it.
"""

import argparse
import json
import os
import sys
import time

import boto3
from botocore.exceptions import ClientError

DEFAULT_REGION = "ca-central-1"


# ══════════════════════════════════════════════════════════════════
# ARGUMENT HANDLING
# ══════════════════════════════════════════════════════════════════

def normalize_instance_id(value):
    """Accept either a bare instance ID or a full Connect instance ARN."""
    if not value:
        return value
    value = value.strip().rstrip("/")
    if value.startswith("arn:"):
        return value.split("/")[-1]
    return value


def region_from_arn(value):
    """Pull the region out of a Connect ARN, or return None."""
    if value and value.strip().startswith("arn:"):
        parts = value.strip().split(":")
        if len(parts) > 3 and parts[3]:
            return parts[3]
    return None


def parse_args():
    parser = argparse.ArgumentParser(
        description="Migrate Connect Cases fields, layouts, and templates between instances.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Example:\n"
            "  python3 migrate_cases.py \\\n"
            "      --source-instance <SOURCE_INSTANCE_ID> \\\n"
            "      --target-instance <TARGET_INSTANCE_ID> \\\n"
            "      --region ca-central-1 --dry-run\n"
        ),
    )
    parser.add_argument("--source-instance", required=True,
                        help="Source Connect instance ID or full ARN")
    parser.add_argument("--target-instance", required=True,
                        help="Target Connect instance ID or full ARN")
    parser.add_argument("--region", default=None,
                        help=f"AWS region (default: from ARN, else AWS_REGION, else {DEFAULT_REGION})")
    parser.add_argument("--output", default="cases_id_mapping.json",
                        help="Path for the ID mapping JSON (default: cases_id_mapping.json)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview what would be created without making changes")

    args = parser.parse_args()

    # Resolve region: explicit flag > ARN > env var > default
    if not args.region:
        args.region = (
            region_from_arn(args.source_instance)
            or region_from_arn(args.target_instance)
            or os.environ.get("AWS_REGION")
            or os.environ.get("AWS_DEFAULT_REGION")
            or DEFAULT_REGION
        )

    args.source_instance = normalize_instance_id(args.source_instance)
    args.target_instance = normalize_instance_id(args.target_instance)

    if args.source_instance == args.target_instance:
        parser.error("Source and target instance are the same.")

    return args


# ══════════════════════════════════════════════════════════════════
# DOMAIN DISCOVERY
# ══════════════════════════════════════════════════════════════════

def get_domain_for_instance(connect_client, cases_client, instance_id):
    """
    Find the Cases domain linked to a Connect instance via its
    integration association. Falls back to an interactive pick.
    """
    try:
        resp = connect_client.list_integration_associations(
            InstanceId=instance_id,
            IntegrationType="CASES_DOMAIN",
            MaxResults=5,
        )
        for assoc in resp.get("IntegrationAssociationSummaryList", []):
            arn = assoc.get("IntegrationArn", "")
            if "/domain/" in arn:
                return arn.split("/domain/")[-1]
            parts = arn.rsplit("/", 1)
            if len(parts) == 2:
                return parts[-1]
    except ClientError as e:
        print(f"  [WARN] list_integration_associations failed: {e}")

    print(f"  [WARN] Could not auto-detect domain for instance {instance_id}")
    try:
        domains = cases_client.list_domains(maxResults=10).get("domains", [])
        if not domains:
            print("  [ERROR] No Cases domains found in this account/region.")
            return None
        print("  Available domains:")
        for i, d in enumerate(domains):
            print(f"    [{i}] {d['name']}  (id: {d['domainId']})")
        choice = input(f"  Select domain index for instance {instance_id}: ").strip()
        return domains[int(choice)]["domainId"]
    except Exception as ex:
        print(f"  [ERROR] {ex}")
        return None


# ══════════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════════

def paginate(fn, result_key, **kwargs):
    """Generic paginator for Cases APIs that use nextToken."""
    items = []
    next_token = None
    while True:
        call_kwargs = {**kwargs}
        if next_token:
            call_kwargs["nextToken"] = next_token
        resp = fn(**call_kwargs)
        items.extend(resp.get(result_key, []))
        next_token = resp.get("nextToken")
        if not next_token:
            break
    return items


def pick(d, *keys, default=""):
    """Read the first present key — tolerates camelCase / PascalCase drift."""
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return default


# ══════════════════════════════════════════════════════════════════
# FIELDS
# ══════════════════════════════════════════════════════════════════

def get_all_fields(cases, domain_id):
    """Return all fields (system + custom) with full details."""
    summaries = paginate(cases.list_fields, "fields", domainId=domain_id, maxResults=100)

    detailed = []
    for i in range(0, len(summaries), 50):
        batch = summaries[i:i + 50]
        field_ids = [{"id": pick(f, "fieldId", "FieldId")} for f in batch]
        resp = cases.batch_get_field(domainId=domain_id, fields=field_ids)
        detailed.extend(resp.get("fields", []))
        for err in resp.get("errors", []):
            print(f"  [WARN] batch_get_field error: {err}")
    return detailed


def get_field_options(cases, domain_id, field_id):
    """Get all options for a single-select field."""
    return paginate(cases.list_field_options, "options",
                    domainId=domain_id, fieldId=field_id, maxResults=100)


def create_field(cases, domain_id, name, field_type, description=""):
    """Create a custom field. Returns the new field ID."""
    kwargs = {"domainId": domain_id, "name": name, "type": field_type}
    if description:
        kwargs["description"] = description
    return cases.create_field(**kwargs)["fieldId"]


def put_field_options(cases, domain_id, field_id, options):
    """Add options to a single-select field in the target."""
    if not options:
        return
    formatted = [
        {"name": o["name"], "value": o["value"], "active": o.get("active", True)}
        for o in options
    ]
    for i in range(0, len(formatted), 50):
        cases.batch_put_field_options(
            domainId=domain_id, fieldId=field_id, options=formatted[i:i + 50]
        )


def build_system_field_map(source_fields, target_fields):
    """Map source System field IDs to target System field IDs by name."""
    target_sys = {
        pick(f, "name", "Name"): pick(f, "fieldId", "FieldId")
        for f in target_fields
        if pick(f, "namespace", "Namespace") == "System"
    }
    mapping = {}
    for f in source_fields:
        if pick(f, "namespace", "Namespace") == "System":
            name = pick(f, "name", "Name")
            target_id = target_sys.get(name)
            if target_id:
                mapping[pick(f, "fieldId", "FieldId")] = target_id
            else:
                print(f"  [WARN] System field '{name}' not found in target — skipping")
    return mapping


def migrate_fields(cases, source_domain, target_domain, dry_run):
    print("\n[2/4] Migrating fields...")

    source_fields = get_all_fields(cases, source_domain)
    target_fields = get_all_fields(cases, target_domain)

    print(f"  Source: {len(source_fields)} fields total")
    print(f"  Target: {len(target_fields)} fields total")

    field_id_map = build_system_field_map(source_fields, target_fields)
    print(f"  System fields mapped: {len(field_id_map)}")

    target_custom_by_name = {}
    for f in target_fields:
        if pick(f, "namespace", "Namespace") == "Custom":
            fn = pick(f, "name", "Name")
            fid = pick(f, "fieldId", "FieldId")
            if fn and fid:
                target_custom_by_name[fn] = fid

    source_custom = [
        f for f in source_fields
        if pick(f, "namespace", "Namespace") == "Custom"
    ]

    created = 0
    skipped = 0

    for field in source_custom:
        fname = pick(field, "name", "Name")
        ftype = pick(field, "type", "Type")
        fdesc = pick(field, "description", "Description")
        src_fid = pick(field, "fieldId", "FieldId")

        if fname in target_custom_by_name:
            field_id_map[src_fid] = target_custom_by_name[fname]
            print(f"  [SKIP] '{fname}' — already exists in target")
            skipped += 1
            continue

        print(f"  [CREATE] '{fname}' (type={ftype})")

        if dry_run:
            field_id_map[src_fid] = f"<dry-run-{fname}>"
            if ftype == "SingleSelect":
                opts = get_field_options(cases, source_domain, src_fid)
                print(f"           → would migrate {len(opts)} options")
            created += 1
            continue

        try:
            new_id = create_field(cases, target_domain, fname, ftype, fdesc)
            field_id_map[src_fid] = new_id
            print(f"           → new fieldId: {new_id}")
            created += 1
        except cases.exceptions.ConflictException:
            # Exists in target but wasn't in our listing — look it up and map it
            print("           → already exists, looking up ID...")
            for tf in get_all_fields(cases, target_domain):
                if pick(tf, "name", "Name") == fname:
                    existing_id = pick(tf, "fieldId", "FieldId")
                    field_id_map[src_fid] = existing_id
                    target_custom_by_name[fname] = existing_id
                    print(f"           → mapped to existing: {existing_id}")
                    break
            skipped += 1
            continue

        if ftype == "SingleSelect":
            options = get_field_options(cases, source_domain, src_fid)
            if options:
                print(f"           → migrating {len(options)} options")
                put_field_options(cases, target_domain, field_id_map[src_fid], options)

        time.sleep(0.2)

    print(f"\n  Fields: {created} created, {skipped} skipped (already exist)")
    return field_id_map


# ══════════════════════════════════════════════════════════════════
# LAYOUTS
# ══════════════════════════════════════════════════════════════════

def get_all_layouts(cases, domain_id):
    """List all layouts with full details."""
    summaries = paginate(cases.list_layouts, "layouts", domainId=domain_id, maxResults=100)
    detailed = []
    for s in summaries:
        resp = cases.get_layout(domainId=domain_id, layoutId=s["layoutId"])
        detailed.append({
            "layoutId": resp["layoutId"],
            "name": resp["name"],
            "content": resp["content"],
            "tags": resp.get("tags", {}),
        })
    return detailed


def remap_layout_content(content, field_id_map):
    """Recursively walk layout content and remap field ID references."""
    if isinstance(content, dict):
        new = {}
        for k, v in content.items():
            if k == "fieldId" and isinstance(v, str):
                new[k] = field_id_map.get(v, v)
            elif k == "id" and isinstance(v, str) and v in field_id_map:
                new[k] = field_id_map[v]
            else:
                new[k] = remap_layout_content(v, field_id_map)
        return new
    if isinstance(content, list):
        return [remap_layout_content(i, field_id_map) for i in content]
    return content


def migrate_layouts(cases, source_domain, target_domain, field_id_map, dry_run):
    print("\n[3/4] Migrating layouts...")

    source_layouts = get_all_layouts(cases, source_domain)
    target_layouts = get_all_layouts(cases, target_domain)
    target_by_name = {l["name"]: l["layoutId"] for l in target_layouts}

    layout_id_map = {}
    created = 0

    for layout in source_layouts:
        lname = layout["name"]

        if lname in target_by_name:
            layout_id_map[layout["layoutId"]] = target_by_name[lname]
            print(f"  [SKIP] Layout '{lname}' — already exists")
            continue

        remapped = remap_layout_content(layout["content"], field_id_map)
        print(f"  [CREATE] Layout '{lname}'")

        if dry_run:
            layout_id_map[layout["layoutId"]] = f"<dry-run-{lname}>"
            created += 1
            continue

        try:
            resp = cases.create_layout(
                domainId=target_domain, name=lname, content=remapped
            )
            layout_id_map[layout["layoutId"]] = resp["layoutId"]
            print(f"           → new layoutId: {resp['layoutId']}")
            created += 1
        except ClientError as e:
            print(f"  [ERROR] Creating layout '{lname}': {e}")

    print(f"\n  Layouts: {created} created")
    return layout_id_map


# ══════════════════════════════════════════════════════════════════
# TEMPLATES
# ══════════════════════════════════════════════════════════════════

def get_all_templates(cases, domain_id):
    """List all templates with full details."""
    summaries = paginate(cases.list_templates, "templates", domainId=domain_id, maxResults=100)
    detailed = []
    for s in summaries:
        try:
            detailed.append(cases.get_template(domainId=domain_id, templateId=s["templateId"]))
        except ClientError as e:
            if "ResourceNotFoundException" in str(e):
                continue
            raise
    return detailed


def migrate_templates(cases, source_domain, target_domain, field_id_map, layout_id_map, dry_run):
    print("\n[4/4] Migrating templates...")

    source_templates = get_all_templates(cases, source_domain)
    target_templates = get_all_templates(cases, target_domain)
    target_by_name = {t["name"]: t["templateId"] for t in target_templates}

    created = 0

    for tmpl in source_templates:
        tname = tmpl["name"]

        if tmpl.get("deleted", False):
            print(f"  [SKIP] Template '{tname}' — deleted in source")
            continue
        if tname in target_by_name:
            print(f"  [SKIP] Template '{tname}' — already exists")
            continue

        required = [
            {"fieldId": field_id_map.get(rf["fieldId"], rf["fieldId"])}
            for rf in tmpl.get("requiredFields", [])
        ]

        layout_config = tmpl.get("layoutConfiguration", {})
        if layout_config and "defaultLayout" in layout_config:
            old_layout = layout_config["defaultLayout"]
            layout_config = {"defaultLayout": layout_id_map.get(old_layout, old_layout)}

        kwargs = {
            "domainId": target_domain,
            "name": tname,
            "status": tmpl.get("status", "Active"),
        }
        if tmpl.get("description"):
            kwargs["description"] = tmpl["description"]
        if required:
            kwargs["requiredFields"] = required
        if layout_config:
            kwargs["layoutConfiguration"] = layout_config

        print(f"  [CREATE] Template '{tname}' (requiredFields={len(required)})")

        if dry_run:
            created += 1
            continue

        try:
            resp = cases.create_template(**kwargs)
            print(f"           → new templateId: {resp['templateId']}")
            created += 1
        except ClientError as e:
            print(f"  [ERROR] Creating template '{tname}': {e}")

    print(f"\n  Templates: {created} created")
    return created


# ══════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════

def main():
    args = parse_args()

    if args.dry_run:
        print("=" * 60)
        print("  DRY RUN — no changes will be made")
        print("=" * 60)

    connect = boto3.client("connect", region_name=args.region)
    cases = boto3.client("connectcases", region_name=args.region)

    print(f"\nRegion: {args.region}")
    print(f"Source instance: {args.source_instance}")
    print(f"Target instance: {args.target_instance}")

    # Step 1 — domains
    print("\n[1/4] Discovering Cases domains...")
    source_domain = get_domain_for_instance(connect, cases, args.source_instance)
    target_domain = get_domain_for_instance(connect, cases, args.target_instance)

    if not source_domain or not target_domain:
        print("\nERROR: Cannot proceed without both domain IDs.")
        sys.exit(1)

    print(f"  Source domain: {source_domain}")
    print(f"  Target domain: {target_domain}")

    if source_domain == target_domain:
        print("  ERROR: Source and target domains are the same. Aborting.")
        sys.exit(1)

    # Steps 2-4
    field_id_map = migrate_fields(cases, source_domain, target_domain, args.dry_run)
    layout_id_map = migrate_layouts(cases, source_domain, target_domain, field_id_map, args.dry_run)
    templates_created = migrate_templates(
        cases, source_domain, target_domain, field_id_map, layout_id_map, args.dry_run
    )

    # Summary
    print("\n" + "=" * 60)
    print("  DRY RUN COMPLETE — no resources were created" if args.dry_run else "  MIGRATION COMPLETE")
    print(f"  Fields mapped/created : {len(field_id_map)}")
    print(f"  Layouts mapped/created: {len(layout_id_map)}")
    print(f"  Templates created     : {templates_created}")
    print("=" * 60)

    mapping = {
        "region": args.region,
        "source_instance": args.source_instance,
        "target_instance": args.target_instance,
        "source_domain": source_domain,
        "target_domain": target_domain,
        "field_id_map": field_id_map,
        "layout_id_map": layout_id_map,
    }
    with open(args.output, "w") as f:
        json.dump(mapping, f, indent=2)

    print(f"\n  ID mapping saved to: {args.output}")
    print("  (keep this — useful for debugging or migrating other resources)\n")


if __name__ == "__main__":
    main()