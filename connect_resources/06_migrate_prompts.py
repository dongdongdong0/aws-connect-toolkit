#!/usr/bin/env python3
"""
06_migrate_prompts.py — Copy prompt audio files to another Connect instance.

Connect stores prompt audio internally and describe_prompt does not return an
S3Uri for it, so the audio cannot be referenced directly. This script moves each
prompt in four steps:

  1. get_prompt_file on the source instance → presigned download URL
  2. download the audio to a temp directory
  3. upload it to an S3 bucket you own
  4. create_prompt on the target instance pointing at that S3 object

The staged S3 objects are deleted at the end — the bucket is scratch space, not
the prompt's permanent home. Connect copies the audio into its own storage.

Requires an S3 bucket in the same region, and s3:PutObject / s3:GetObject /
s3:DeleteObject on the prefix. The Connect service role needs read access to
the bucket to pull the file during create_prompt.

No dependencies — can run at any point in the migration.

Usage:
  python3 06_migrate_prompts.py -s <SOURCE_ID> -t <TARGET_ID> -r us-west-2 \
      --s3-bucket my-scratch-bucket [--s3-prefix prompt-migration/] [--dry-run]
"""

import argparse
import os
import tempfile
import time
import urllib.request

import boto3


def parse_args():
    p = argparse.ArgumentParser(description="Migrate prompts between Connect instances")
    p.add_argument("-s", "--source-instance-id", required=True, help="Source Connect instance ID")
    p.add_argument("-t", "--target-instance-id", required=True, help="Target Connect instance ID")
    p.add_argument("-r", "--region", required=True, help="AWS region (e.g. us-west-2)")
    p.add_argument("--s3-bucket", required=True, help="S3 bucket used to stage the audio files")
    p.add_argument("--s3-prefix", default="prompt-migration/", help="S3 key prefix for staging")
    p.add_argument("--keep-s3", action="store_true",
                   help="Leave the staged S3 objects in place instead of deleting them")
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


def safe_filename(name):
    """Make a prompt name safe to use as an S3 key / local filename."""
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in name)[:200]


def main():
    args = parse_args()
    connect = boto3.client("connect", region_name=args.region)
    s3 = boto3.client("s3", region_name=args.region)

    if args.dry_run:
        print("=== DRY RUN — no changes will be made ===")

    print(f"\nSource: {args.source_instance_id}")
    print(f"Target: {args.target_instance_id}")
    print(f"Staging: s3://{args.s3_bucket}/{args.s3_prefix}\n")

    source = paginate(connect.list_prompts, args.source_instance_id, "PromptSummaryList")
    target = paginate(connect.list_prompts, args.target_instance_id, "PromptSummaryList")
    target_names = {p["Name"] for p in target}

    print(f"Source: {len(source)}, Target: {len(target)}")

    missing = [p for p in source if p["Name"] not in target_names]
    if not missing:
        print("\n✓ All prompts already on target.")
        return

    print(f"Missing on target: {len(missing)}")

    created = failed = 0

    with tempfile.TemporaryDirectory() as tmpdir:
        for p in missing:
            name = p["Name"]
            print(f"\n  → {name}")

            if args.dry_run:
                print("    Would migrate")
                created += 1
                continue

            # 1. Presigned download URL
            try:
                resp = connect.get_prompt_file(
                    InstanceId=args.source_instance_id,
                    PromptId=p["Id"],
                )
                url = resp.get("PromptPresignedUrl", "")
                if not url:
                    print("    ✗ No presigned URL returned")
                    failed += 1
                    continue
            except Exception as e:
                print(f"    ✗ get_prompt_file failed: {e}")
                failed += 1
                continue

            # 2. Download
            local_file = os.path.join(tmpdir, f"{p['Id']}.wav")
            try:
                urllib.request.urlretrieve(url, local_file)
                print(f"    Downloaded ({os.path.getsize(local_file) / 1024:.0f} KB)")
            except Exception as e:
                print(f"    ✗ Download failed: {e}")
                failed += 1
                continue

            # 3. Stage in S3
            s3_key = f"{args.s3_prefix}{safe_filename(name)}.wav"
            try:
                s3.upload_file(local_file, args.s3_bucket, s3_key)
            except Exception as e:
                print(f"    ✗ S3 upload failed: {e}")
                failed += 1
                continue

            # 4. Register on target
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
                created += 1
                time.sleep(0.4)
            except Exception as e:
                print(f"    ✗ create_prompt failed: {e}")
                failed += 1

            try:
                os.remove(local_file)
            except Exception:
                pass

    # Clean up the staging area
    if not args.dry_run and not args.keep_s3:
        print(f"\nCleaning up s3://{args.s3_bucket}/{args.s3_prefix}")
        try:
            objs = s3.list_objects_v2(Bucket=args.s3_bucket, Prefix=args.s3_prefix)
            for obj in objs.get("Contents", []):
                s3.delete_object(Bucket=args.s3_bucket, Key=obj["Key"])
            print("✓ Cleaned up")
        except Exception as e:
            print(f"⚠ Cleanup failed (not critical): {e}")

    print(f"\n{'━' * 60}")
    print(f"Created: {created}, Failed: {failed}")
    print("━" * 60)


if __name__ == "__main__":
    main()