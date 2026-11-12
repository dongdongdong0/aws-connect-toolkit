#!/usr/bin/env python3
"""
migrate_quick_responses.py
==========================
Migrate Quick Responses between two Amazon Connect instances.

Split out of migrate_connect_resources.py — the logic is unchanged, only
the instance IDs and region are now CLI parameters.

Quick Responses are matched by NAME, so re-running is safe: anything
already present in the target is skipped, never duplicated.

-------------------------------------------------------------------------
PARAMETERS
-------------------------------------------------------------------------
  --source-instance   REQUIRED  Source Connect instance ID or full ARN
  --target-instance   REQUIRED  Target Connect instance ID or full ARN
  --region            OPTIONAL  AWS region (default: inferred from the ARN,
                                else AWS_REGION env var, else ca-central-1)
  --dry-run           OPTIONAL  Preview only; makes no changes

Instance values accept either form:
    <SOURCE_INSTANCE_ID>
    arn:aws:connect:<REGION>:<ACCOUNT_ID>:instance/<SOURCE_INSTANCE_ID>

-------------------------------------------------------------------------
USAGE
-------------------------------------------------------------------------
  # Preview first
  python3 migrate_quick_responses.py \
      --source-instance <SOURCE_INSTANCE_ID> \
      --target-instance <TARGET_INSTANCE_ID> \
      --region ca-central-1 \
      --dry-run

  # Then run for real
  python3 migrate_quick_responses.py \
      --source-instance <SOURCE_INSTANCE_ID> \
      --target-instance <TARGET_INSTANCE_ID> \
      --region ca-central-1

-------------------------------------------------------------------------
IAM PERMISSIONS REQUIRED
-------------------------------------------------------------------------
  connect:SearchQuickResponses
  connect:CreateQuickResponse

-------------------------------------------------------------------------
NOTE
-------------------------------------------------------------------------
If the installed boto3 does not expose SearchQuickResponses, the script
reports that and exits cleanly — migrate those from the Connect console.
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
        description="Migrate Quick Responses between Connect instances.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Example:\n"
            "  python3 migrate_quick_responses.py \\\n"
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
# QUICK RESPONSES
# ══════════════════════════════════════════════════════════════════

def migrate_quick_responses(client, src, tgt, dry_run):
    print("\nQuick Responses")
    print("─" * 50)

    try:
        src_resp = client.search_quick_responses(
            InstanceId=src, MaxResults=100, SearchCriteria={}
        )
        src_list = src_resp.get("QuickResponses", [])
    except (ClientError, AttributeError, TypeError) as e:
        print(f"  [WARN] search_quick_responses not available or failed: {e}")
        print("  → Quick Responses may need manual migration from the console.")
        return 0

    try:
        tgt_resp = client.search_quick_responses(
            InstanceId=tgt, MaxResults=100, SearchCriteria={}
        )
        tgt_by_name = {
            qr.get("Name", ""): qr.get("QuickResponseId", "")
            for qr in tgt_resp.get("QuickResponses", [])
        }
    except (ClientError, AttributeError, TypeError):
        tgt_by_name = {}

    print(f"  Source: {len(src_list)},  Target existing: {len(tgt_by_name)}")

    created = 0
    for qr in src_list:
        name = qr.get("Name", "")
        if name in tgt_by_name:
            print(f"  [SKIP]   {name}")
            continue

        print(f"  [CREATE] {name}")
        if not dry_run:
            try:
                kwargs = {
                    "InstanceId": tgt,
                    "Name": name,
                    "ContentType": qr.get("ContentType", "text/plain"),
                }
                if qr.get("Contents"):
                    kwargs["Contents"] = qr["Contents"]
                elif qr.get("Content"):
                    kwargs["Content"] = qr["Content"]
                if qr.get("Description"):
                    kwargs["Description"] = qr["Description"]
                if qr.get("Language"):
                    kwargs["Language"] = qr["Language"]
                if qr.get("ShortcutKey"):
                    kwargs["ShortcutKey"] = qr["ShortcutKey"]
                if qr.get("Channels"):
                    kwargs["Channels"] = qr["Channels"]

                resp = client.create_quick_response(**kwargs)
                print(f"           → {resp.get('QuickResponse', {}).get('QuickResponseId', 'OK')}")
                created += 1
            except ClientError as e:
                print(f"           → ERROR: {e}")
        else:
            created += 1

    print(f"  Result: {created} created")
    return created


# ══════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════

def main():
    args = parse_args()

    if args.dry_run:
        print("=" * 55)
        print("  DRY RUN — no changes will be made")
        print("=" * 55)

    client = boto3.client("connect", region_name=args.region)

    print(f"\nRegion: {args.region}")
    print(f"Source: {args.source_instance}")
    print(f"Target: {args.target_instance}")

    created = migrate_quick_responses(
        client, args.source_instance, args.target_instance, args.dry_run
    )

    print("\n" + "=" * 55)
    print("  DRY RUN COMPLETE" if args.dry_run else "  DONE")
    print(f"  Quick Responses created: {created}")
    print("=" * 55 + "\n")


if __name__ == "__main__":
    main()