#!/usr/bin/env python3
"""
01_sync_lambda.py — Sync Lambda functions between two Amazon Connect instances.

What it does:
  1. Lists all Lambda functions associated with the source Connect instance.
  2. Compares against the target instance to find missing ones.
  3. Associates missing Lambda functions to the target instance.
  4. Checks each Lambda's resource-based policy — if the target instance
     is not allowed to invoke the function, adds the permission automatically.

Prerequisites:
  - Both instances must be in the same AWS account and region.
  - IAM permissions: connect:ListLambdaFunctions, connect:AssociateLambdaFunction,
    lambda:GetPolicy, lambda:AddPermission.

Usage:
  python3 01_sync_lambda.py --source-instance-id <ID> --target-instance-id <ID> --region <REGION> [--dry-run]

Notes:
  - Lambda functions that no longer exist in AWS will fail with ResourceNotFoundException.
    These are stale associations on the source instance and can be safely ignored.
  - Lambda ARNs do not contain the instance ID, so no ARN rewriting is needed.
    This script only handles the association and invoke permission.
"""

import argparse
import boto3
import json
import sys
import time


def parse_args():
    parser = argparse.ArgumentParser(description="Sync Lambda functions between Connect instances")
    parser.add_argument("--source-instance-id", required=True, help="Source Connect instance ID")
    parser.add_argument("--target-instance-id", required=True, help="Target Connect instance ID")
    parser.add_argument("--region", required=True, help="AWS region (e.g. us-west-2)")
    parser.add_argument("--dry-run", action="store_true", help="Preview changes without applying")
    return parser.parse_args()


def list_lambda_functions(connect, instance_id):
    """List all Lambda ARNs associated with a Connect instance."""
    arns = []
    kwargs = {"InstanceId": instance_id, "MaxResults": 25}
    while True:
        resp = connect.list_lambda_functions(**kwargs)
        arns.extend(resp.get("LambdaFunctions", []))
        token = resp.get("NextToken")
        if not token:
            break
        kwargs["NextToken"] = token
    return arns


def check_lambda_policy(lambda_client, function_arn, target_instance_arn):
    """Check if the Lambda resource policy allows the target Connect instance to invoke it."""
    try:
        resp = lambda_client.get_policy(FunctionName=function_arn)
        policy = json.loads(resp["Policy"])
        for stmt in policy.get("Statement", []):
            condition = stmt.get("Condition", {})
            arn_like = condition.get("ArnLike", {})
            source_arn = arn_like.get("AWS:SourceArn", arn_like.get("aws:SourceArn", ""))
            if target_instance_arn in source_arn or source_arn.endswith("*"):
                return True
        return False
    except lambda_client.exceptions.ResourceNotFoundException:
        return False


def add_lambda_permission(lambda_client, function_arn, target_instance_arn, target_instance_id):
    """Add invoke permission for the target Connect instance on a Lambda."""
    sid = f"connect-{target_instance_id[:8]}"
    try:
        lambda_client.add_permission(
            FunctionName=function_arn,
            StatementId=sid,
            Action="lambda:InvokeFunction",
            Principal="connect.amazonaws.com",
            SourceArn=target_instance_arn,
        )
        return True
    except lambda_client.exceptions.ResourceConflictException:
        return True  # Statement already exists
    except Exception as e:
        print(f"    ⚠ Failed to add permission: {e}")
        return False


def main():
    args = parse_args()
    connect = boto3.client("connect", region_name=args.region)
    lambda_client = boto3.client("lambda", region_name=args.region)

    if args.dry_run:
        print("=== DRY RUN — no changes will be made ===\n")

    # Derive target instance ARN for policy checks
    sts = boto3.client("sts")
    account_id = sts.get_caller_identity()["Account"]
    target_instance_arn = f"arn:aws:connect:{args.region}:{account_id}:instance/{args.target_instance_id}"

    # List Lambda functions on both instances
    source_lambdas = list_lambda_functions(connect, args.source_instance_id)
    target_lambdas = list_lambda_functions(connect, args.target_instance_id)
    target_set = set(target_lambdas)

    print(f"Source instance: {len(source_lambdas)} Lambda(s)")
    print(f"Target instance: {len(target_lambdas)} Lambda(s)")

    missing = [arn for arn in source_lambdas if arn not in target_set]

    if not missing:
        print("\n✓ All source Lambdas already attached to target.")
        return

    print(f"\nMissing on target: {len(missing)}\n")

    created = 0
    failed = 0

    for arn in missing:
        short = arn.split("function:")[-1]
        print(f"  → {short}")

        # Check / fix resource policy
        has_perm = check_lambda_policy(lambda_client, arn, target_instance_arn)
        if not has_perm:
            if args.dry_run:
                print(f"    Policy: target instance NOT allowed — would add")
            else:
                print(f"    Policy: target instance NOT allowed — adding...")
                add_lambda_permission(lambda_client, arn, target_instance_arn, args.target_instance_id)
        else:
            print(f"    Policy: OK")

        # Associate
        if args.dry_run:
            print(f"    Would associate to target")
            created += 1
        else:
            try:
                connect.associate_lambda_function(
                    InstanceId=args.target_instance_id,
                    FunctionArn=arn,
                )
                print(f"    ✓ Associated")
                created += 1
                time.sleep(0.3)
            except Exception as e:
                print(f"    ✗ Failed: {e}")
                failed += 1

    print(f"\n{'━' * 60}")
    print(f"Associated: {created}, Failed: {failed}")
    print('━' * 60)


if __name__ == "__main__":
    main()