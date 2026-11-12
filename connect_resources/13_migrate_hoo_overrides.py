#!/usr/bin/env python3
"""
migrate_hoo_overrides.py
========================
Migrate Hours of Operation OVERRIDES (holiday / special-date exceptions)
between two Amazon Connect instances.

This does NOT create the Hours of Operation profiles themselves — those
must already exist in the target with the SAME NAME. This script matches
each HoO by name, then copies its overrides across.

Overrides are matched by NAME, so re-running is safe: anything already in
the target is skipped, never duplicated.

IMPORTANT — RecurrenceConfig
    Connect rejects overrides whose effective date ranges overlap. A
    recurring override (e.g. "every 3rd Monday in February") carries a
    long EffectiveFrom..EffectiveTill span plus a RecurrenceConfig that
    narrows it to the actual dates. If RecurrenceConfig is dropped, the
    long span is treated as one continuous block and every later override
    fails with "EffectiveFrom and EffectiveTill fields overlap".
    This script copies RecurrenceConfig and OverrideType, so that is handled.

-------------------------------------------------------------------------
PARAMETERS
-------------------------------------------------------------------------
  --source-instance   REQUIRED  Source Connect instance ID or full ARN
  --target-instance   REQUIRED  Target Connect instance ID or full ARN
  --region            OPTIONAL  AWS region (default: inferred from the ARN,
                                else AWS_REGION env var, else ca-central-1)
  --hoo-name          OPTIONAL  Only migrate overrides for this one HoO
                                (repeatable; default: all matching HoOs)
  --dry-run           OPTIONAL  Preview only; makes no changes

Instance values accept either form:
    <SOURCE_INSTANCE_ID>
    arn:aws:connect:<REGION>:<ACCOUNT_ID>:instance/<SOURCE_INSTANCE_ID>

-------------------------------------------------------------------------
USAGE
-------------------------------------------------------------------------
  # Preview first
  python3 migrate_hoo_overrides.py \
      --source-instance <SOURCE_INSTANCE_ID> \
      --target-instance <TARGET_INSTANCE_ID> \
      --region ca-central-1 \
      --dry-run

  # Then run for real
  python3 migrate_hoo_overrides.py \
      --source-instance <SOURCE_INSTANCE_ID> \
      --target-instance <TARGET_INSTANCE_ID> \
      --region ca-central-1

  # Just one Hours of Operation profile
  python3 migrate_hoo_overrides.py \
      --source-instance <SOURCE_INSTANCE_ID> --target-instance <TARGET_INSTANCE_ID> \
      --hoo-name <HOO_NAME>

-------------------------------------------------------------------------
IAM PERMISSIONS REQUIRED
-------------------------------------------------------------------------
  connect:ListHoursOfOperations
  connect:ListHoursOfOperationOverrides
  connect:CreateHoursOfOperationOverride

-------------------------------------------------------------------------
PREREQUISITE
-------------------------------------------------------------------------
Run the Hours of Operation migration first (or create them by hand) so the
target instance has matching HoO names. Any source HoO with no name match in
the target is reported and skipped.
"""

import argparse
import os

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
        description="Migrate Hours of Operation overrides between Connect instances.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Example:\n"
            "  python3 migrate_hoo_overrides.py \\\n"
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
    parser.add_argument("--hoo-name", action="append", default=None,
                        metavar="NAME",
                        help="Only migrate this Hours of Operation (repeatable)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview what would be created without making changes")

    args = parser.parse_args()

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
# HELPERS
# ══════════════════════════════════════════════════════════════════

def list_hoo_by_name(client, instance_id):
    """Return {hoo_name: hoo_id} for an instance."""
    resp = client.list_hours_of_operations(InstanceId=instance_id, MaxResults=100)
    return {h["Name"]: h["Id"] for h in resp.get("HoursOfOperationSummaryList", [])}


def list_overrides(client, instance_id, hoo_id):
    """Return the override list for one Hours of Operation."""
    resp = client.list_hours_of_operation_overrides(
        InstanceId=instance_id,
        HoursOfOperationId=hoo_id,
        MaxResults=100,
    )
    return resp.get("HoursOfOperationOverrideList", [])


# ══════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════

def main():
    args = parse_args()
    client = boto3.client("connect", region_name=args.region)

    if args.dry_run:
        print("=" * 55)
        print("  DRY RUN — no changes will be made")
        print("=" * 55)

    print(f"\nRegion: {args.region}")
    print(f"Source instance: {args.source_instance}")
    print(f"Target instance: {args.target_instance}")

    src_hoos = list_hoo_by_name(client, args.source_instance)
    tgt_hoos = list_hoo_by_name(client, args.target_instance)

    # Optional name filter
    if args.hoo_name:
        wanted = set(args.hoo_name)
        missing = wanted - set(src_hoos)
        for m in sorted(missing):
            print(f"\n[WARN] HoO '{m}' not found in source — ignoring filter entry")
        src_hoos = {n: i for n, i in src_hoos.items() if n in wanted}

    print(f"\nSource HoO: {len(src_hoos)},  Target HoO: {len(tgt_hoos)}")

    total_created = 0
    total_skipped = 0
    total_failed = 0

    for hoo_name, src_hoo_id in src_hoos.items():
        tgt_hoo_id = tgt_hoos.get(hoo_name)
        if not tgt_hoo_id:
            print(f"\n[SKIP] HoO '{hoo_name}' — not found in target")
            continue

        try:
            src_overrides = list_overrides(client, args.source_instance, src_hoo_id)
        except ClientError as e:
            print(f"\n[ERROR] listing overrides for '{hoo_name}': {e}")
            continue

        if not src_overrides:
            print(f"\n[OK]   HoO '{hoo_name}' — no overrides")
            continue

        try:
            tgt_override_names = {
                o.get("Name", "")
                for o in list_overrides(client, args.target_instance, tgt_hoo_id)
            }
        except ClientError:
            tgt_override_names = set()

        print(f"\n── HoO '{hoo_name}' — {len(src_overrides)} override(s) ──")

        for ovr in src_overrides:
            ovr_name = ovr.get("Name", ovr.get("HoursOfOperationOverrideId", "unknown"))
            effective_from = ovr.get("EffectiveFrom", "")
            effective_till = ovr.get("EffectiveTill", "")

            if ovr_name in tgt_override_names:
                print(f"  [SKIP]   '{ovr_name}' — already exists in target")
                total_skipped += 1
                continue

            recurring = " [recurring]" if ovr.get("RecurrenceConfig") else ""
            print(f"  [CREATE] '{ovr_name}'  ({effective_from} → {effective_till}){recurring}")

            if args.dry_run:
                total_created += 1
                continue

            kwargs = {
                "InstanceId": args.target_instance,
                "HoursOfOperationId": tgt_hoo_id,
                "Name": ovr_name,
                "Config": ovr.get("Config", []),
                "EffectiveFrom": effective_from,
                "EffectiveTill": effective_till,
            }
            if ovr.get("Description"):
                kwargs["Description"] = ovr["Description"]
            # Both of these matter — see the RecurrenceConfig note in the header
            if ovr.get("RecurrenceConfig"):
                kwargs["RecurrenceConfig"] = ovr["RecurrenceConfig"]
            if ovr.get("OverrideType"):
                kwargs["OverrideType"] = ovr["OverrideType"]

            try:
                client.create_hours_of_operation_override(**kwargs)
                print("           → done")
                total_created += 1
            except ClientError as e:
                print(f"           → ERROR: {e}")
                total_failed += 1

    print(f"\n{'=' * 55}")
    print(f"  {'DRY RUN COMPLETE' if args.dry_run else 'DONE'}")
    print(f"  Overrides created: {total_created},  skipped: {total_skipped},  failed: {total_failed}")
    print(f"{'=' * 55}\n")


if __name__ == "__main__":
    main()