#!/usr/bin/env python3
"""
02_sync_lex_v2.py — Sync Amazon Lex V2 bot associations between two Connect instances.

What it does:
  1. Lists all Lex V2 bot aliases associated with the source Connect instance.
  2. Compares against the target instance (matched by bot alias ARN).
  3. Associates the missing bot aliases to the target instance.

Prerequisites:
  - Both Connect instances must be in the same AWS account and region.
  - The Lex V2 bots themselves are NOT copied — this script only creates the
    association. The bot alias ARNs must already be reachable from the target
    instance, which is the case when both instances share an account + region.
  - IAM permissions: connect:ListBots, connect:AssociateBot,
    and lex:DescribeBotAlias / lex:ListBotAliases on the referenced bots.

Usage:
  python3 02_sync_lex_v2.py \
      --source-instance-id <SOURCE_ID> \
      --target-instance-id <TARGET_ID> \
      --region us-west-2 \
      [--dry-run] [--debug]

Notes:
  - list_bots() nests the alias ARN differently depending on the Lex version:
    V2 returns {"LexV2Bot": {"AliasArn": "..."}} rather than a flat key.
    get_alias_arn() below handles every shape the API has been seen to return,
    so this script does not break on a KeyError when the response changes.
  - Lex bot alias ARNs do not contain the Connect instance ID, so nothing in
    the ARN needs rewriting — only the association has to be created.
  - Use --debug to dump the raw API response for the first bot, which is the
    quickest way to diagnose an unexpected response structure.
  - This script handles Lex V2 only. Lex V1 (classic) is deprecated; if you
    still have V1 bots, call list_bots with LexVersion="V1" and associate with
    the LexBot={"Name": ..., "LexRegion": ...} parameter instead.
"""

import argparse
import json
import sys
import time

import boto3


def parse_args():
    parser = argparse.ArgumentParser(
        description="Sync Lex V2 bot associations between two Amazon Connect instances"
    )
    parser.add_argument("--source-instance-id", required=True,
                        help="Source Connect instance ID (bots are read from here)")
    parser.add_argument("--target-instance-id", required=True,
                        help="Target Connect instance ID (bots are associated here)")
    parser.add_argument("--region", required=True,
                        help="AWS region of both instances (e.g. us-west-2)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview what would be associated without making changes")
    parser.add_argument("--debug", action="store_true",
                        help="Print the raw API response for the first bot found")
    return parser.parse_args()


def list_v2_bots(connect, instance_id):
    """Return every Lex V2 bot association on an instance (handles pagination)."""
    bots = []
    kwargs = {"InstanceId": instance_id, "LexVersion": "V2", "MaxResults": 25}
    while True:
        resp = connect.list_bots(**kwargs)
        bots.extend(resp.get("LexBots", []))
        token = resp.get("NextToken")
        if not token:
            break
        kwargs["NextToken"] = token
    return bots


def get_alias_arn(bot):
    """
    Extract the bot alias ARN from a list_bots() entry.

    The Connect API nests this differently across versions and SDK releases:
      - V2 (current):  {"LexV2Bot": {"AliasArn": "arn:aws:lex:..."}}
      - flat variant:  {"AliasArn": "arn:aws:lex:..."}
      - older variant: {"LexBotAliasArn": "arn:aws:lex:..."}
    Checking all three keeps the script from raising a KeyError.
    """
    if "LexV2Bot" in bot:
        return bot["LexV2Bot"].get("AliasArn", "")
    return bot.get("AliasArn", bot.get("LexBotAliasArn", ""))


def short_name(arn):
    """Human-readable label for a bot alias ARN, for log output."""
    if not arn:
        return "<no ARN>"
    # arn:aws:lex:<region>:<account>:bot-alias/<botId>/<aliasId>
    return arn.split(":")[-1] if ":" in arn else arn


def main():
    args = parse_args()
    connect = boto3.client("connect", region_name=args.region)

    if args.dry_run:
        print("=== DRY RUN — no changes will be made ===\n")

    # ── List bots on both instances ──
    print("Listing source Lex V2 bots...")
    try:
        source_bots = list_v2_bots(connect, args.source_instance_id)
    except Exception as e:
        print(f"✗ Failed to list bots on source instance: {e}")
        sys.exit(1)
    print(f"Source: {len(source_bots)} bot(s)")

    if args.debug and source_bots:
        print("\nRaw structure of first source bot:")
        print(json.dumps(source_bots[0], indent=2, default=str))
        print()

    print("Listing target Lex V2 bots...")
    try:
        target_bots = list_v2_bots(connect, args.target_instance_id)
    except Exception as e:
        print(f"✗ Failed to list bots on target instance: {e}")
        sys.exit(1)
    print(f"Target: {len(target_bots)} bot(s)\n")

    # ── Diff by alias ARN ──
    target_arns = {get_alias_arn(b) for b in target_bots}
    missing = [b for b in source_bots if get_alias_arn(b) not in target_arns]

    if not missing:
        print("✓ All source Lex V2 bots are already associated with the target.")
        return

    print(f"Missing on target: {len(missing)}\n")

    # ── Associate missing bots ──
    associated = 0
    failed = 0

    for bot in missing:
        arn = get_alias_arn(bot)
        print(f"  → {short_name(arn)}")

        if not arn:
            print("    ✗ Skipped: no alias ARN in the API response (use --debug to inspect)")
            failed += 1
            continue

        if args.dry_run:
            print(f"    Would associate: {arn}")
            associated += 1
            continue

        try:
            connect.associate_bot(
                InstanceId=args.target_instance_id,
                LexV2Bot={"AliasArn": arn},
            )
            print("    ✓ Associated")
            associated += 1
            time.sleep(0.3)  # stay under the Connect API rate limit
        except connect.exceptions.ResourceConflictException:
            print("    ✓ Already associated")
            associated += 1
        except Exception as e:
            print(f"    ✗ Failed: {e}")
            failed += 1

    # ── Summary ──
    print(f"\n{'━' * 60}")
    print(f"Associated: {associated}, Failed: {failed}")
    print("━" * 60)

    if failed:
        print("\nCommon causes of failure:")
        print("  - The Lex bot alias does not exist, or is in another region.")
        print("  - The bot's resource policy does not allow this Connect instance.")
        print("  - The Connect instance has hit its Lex bot association quota.")


if __name__ == "__main__":
    main()