#!/bin/bash

# ============================================================
# Amazon Connect Data Pipeline Deployment Helper
#
# Purpose:
# This script automates the deployment of the Amazon Connect
# data pipeline CloudFormation template, including required IAM
# role permission updates (inline policies).
#
# What this script does:
# 1. Collects required parameters (CLI or interactive input)
# 2. Looks up existing resource ARNs (IAM roles, Lambda if needed)
# 3. Dynamically builds inline IAM policies for:
#    - Firehose role (Kinesis read, S3 write, optional Lambda invoke)
#    - Glue crawler role (S3 read)
# 4. Applies IAM policy patches using aws iam put-role-policy
# 5. Deploys the CloudFormation stack with all required parameters
# 6. Outputs stack deployment results
#
# Why this script exists:
# Deploying a Connect data pipeline usually requires:
# - Creating multiple AWS resources (Kinesis, Firehose, S3, Glue)
# - Ensuring IAM roles have correct permissions
#
# Missing IAM permissions is one of the most common failure points.
# This script eliminates manual IAM setup by automatically patching
# required permissions before deployment.
#
# Optional Transformation:
# - If enabled, the script will:
#   - Look up the Lambda ARN
#   - Add invoke permissions to Firehose role
#   - Pass Lambda ARN into CloudFormation template
#
# - This is particularly useful for CTR data pipelines where
#   preprocessing (normalization / enrichment) is required before S3
#
# Requirements:
# - AWS CLI configured with sufficient permissions
# - Existing IAM roles for Firehose and Glue crawler
# - CloudFormation template file (default: data_pipeline.yaml)
#
# Notes:
# - This script modifies IAM roles by attaching inline policies
# - It is recommended for controlled environments or reusable setups
# - For production environments, consider converting to managed policies
#
# ============================================================
set -euo pipefail

########################################
# Helpers
########################################
log() { echo "[INFO] $1"; }
warn() { echo "[WARN] $1"; }
err() { echo "[ERROR] $1" >&2; }

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || {
    err "Required command not found: $1"
    exit 1
  }
}

confirm() {
  read -r -p "$1 [y/N]: " ans
  [[ "${ans:-}" =~ ^[Yy]$ ]]
}

########################################
# Defaults
########################################
REGION="${AWS_REGION:-us-east-1}"
TEMPLATE_FILE="data_pipeline.yaml"

########################################
# Parse CLI parameters
########################################
while [[ $# -gt 0 ]]; do
  case $1 in
    --region) REGION="$2"; shift 2 ;;
    --bucket) S3_BUCKET_NAME="$2"; shift 2 ;;
    --stream) KINESIS_STREAM_NAME="$2"; shift 2 ;;
    --firehose-stream) FIREHOSE_DELIVERY_STREAM_NAME="$2"; shift 2 ;;
    --database) GLUE_DATABASE_NAME="$2"; shift 2 ;;
    --crawler) GLUE_CRAWLER_NAME="$2"; shift 2 ;;
    --pipeline) PIPELINE_NAME="$2"; shift 2 ;;
    --stack) STACK_NAME="$2"; shift 2 ;;
    --firehose-role) FIREHOSE_ROLE_NAME="$2"; shift 2 ;;
    --crawler-role) GLUE_CRAWLER_ROLE_NAME="$2"; shift 2 ;;
    --enable-transformation) ENABLE_TRANSFORMATION="$2"; shift 2 ;;
    --lambda) LAMBDA_FUNCTION_NAME="$2"; shift 2 ;;
    --buffer-interval) FIREHOSE_BUFFER_INTERVAL="$2"; shift 2 ;;
    --buffer-size) FIREHOSE_BUFFER_SIZE="$2"; shift 2 ;;
    --template-file) TEMPLATE_FILE="$2"; shift 2 ;;
    *) err "Unknown parameter: $1"; exit 1 ;;
  esac
done

########################################
# Fallback to interactive input
########################################
echo "=== IAM Patch + CFN Deploy Helper ==="

[[ -n "${STACK_NAME:-}" ]] || read -r -p "Stack name: " STACK_NAME
[[ -n "${PIPELINE_NAME:-}" ]] || read -r -p "Pipeline name: " PIPELINE_NAME
[[ -n "${S3_BUCKET_NAME:-}" ]] || read -r -p "S3 bucket name: " S3_BUCKET_NAME
[[ -n "${KINESIS_STREAM_NAME:-}" ]] || read -r -p "Kinesis stream name: " KINESIS_STREAM_NAME
[[ -n "${FIREHOSE_DELIVERY_STREAM_NAME:-}" ]] || read -r -p "Firehose delivery stream name: " FIREHOSE_DELIVERY_STREAM_NAME
[[ -n "${GLUE_DATABASE_NAME:-}" ]] || read -r -p "Glue database name: " GLUE_DATABASE_NAME
[[ -n "${GLUE_CRAWLER_NAME:-}" ]] || read -r -p "Glue crawler name: " GLUE_CRAWLER_NAME
[[ -n "${FIREHOSE_ROLE_NAME:-}" ]] || read -r -p "Firehose role name: " FIREHOSE_ROLE_NAME
[[ -n "${GLUE_CRAWLER_ROLE_NAME:-}" ]] || read -r -p "Glue crawler role name: " GLUE_CRAWLER_ROLE_NAME
[[ -n "${ENABLE_TRANSFORMATION:-}" ]] || read -r -p "Enable transformation? (true/false) [false]: " ENABLE_TRANSFORMATION
ENABLE_TRANSFORMATION="${ENABLE_TRANSFORMATION:-false}"

if [[ "$ENABLE_TRANSFORMATION" == "true" && -z "${LAMBDA_FUNCTION_NAME:-}" ]]; then
  read -r -p "Lambda function name: " LAMBDA_FUNCTION_NAME
fi

FIREHOSE_BUFFER_INTERVAL="${FIREHOSE_BUFFER_INTERVAL:-300}"
FIREHOSE_BUFFER_SIZE="${FIREHOSE_BUFFER_SIZE:-5}"

########################################
# Requirements
########################################
require_cmd aws

if [[ ! -f "$TEMPLATE_FILE" ]]; then
  err "Template file not found: $TEMPLATE_FILE"
  exit 1
fi

########################################
# Lookups
########################################
log "Looking up account id..."
ACCOUNT_ID="$(aws sts get-caller-identity --query 'Account' --output text)"

log "Looking up Firehose role ARN..."
FIREHOSE_ROLE_ARN="$(aws iam get-role \
  --role-name "$FIREHOSE_ROLE_NAME" \
  --query 'Role.Arn' \
  --output text)"

log "Looking up Glue crawler role ARN..."
GLUE_CRAWLER_ROLE_ARN="$(aws iam get-role \
  --role-name "$GLUE_CRAWLER_ROLE_NAME" \
  --query 'Role.Arn' \
  --output text)"

LAMBDA_FUNCTION_ARN=""
if [[ "$ENABLE_TRANSFORMATION" == "true" ]]; then
  log "Looking up Lambda ARN..."
  LAMBDA_FUNCTION_ARN="$(aws lambda get-function \
    --region "$REGION" \
    --function-name "$LAMBDA_FUNCTION_NAME" \
    --query 'Configuration.FunctionArn' \
    --output text)"
fi

########################################
# Policy names
########################################
FIREHOSE_PATCH_POLICY_NAME="DataPipelineFirehosePatch-${PIPELINE_NAME}"
CRAWLER_PATCH_POLICY_NAME="DataPipelineCrawlerPatch-${PIPELINE_NAME}"


########################################
# Build policies
########################################
FIREHOSE_POLICY_JSON="$(cat <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "KinesisReadForPipeline",
      "Effect": "Allow",
      "Action": [
        "kinesis:DescribeStream",
        "kinesis:GetRecords",
        "kinesis:GetShardIterator",
        "kinesis:ListShards"
      ],
      "Resource": "arn:aws:kinesis:${REGION}:${ACCOUNT_ID}:stream/${KINESIS_STREAM_NAME}"
    },
    {
      "Sid": "S3WriteForPipeline",
      "Effect": "Allow",
      "Action": [
        "s3:PutObject",
        "s3:ListBucket",
        "s3:GetBucketLocation"
      ],
      "Resource": [
        "arn:aws:s3:::${S3_BUCKET_NAME}",
        "arn:aws:s3:::${S3_BUCKET_NAME}/*"
      ]
    }$(if [[ "$ENABLE_TRANSFORMATION" == "true" ]]; then
      cat <<EOF2
,
    {
      "Sid": "LambdaInvokeForPipeline",
      "Effect": "Allow",
      "Action": [
        "lambda:InvokeFunction",
        "lambda:GetFunctionConfiguration"
      ],
      "Resource": [
        "${LAMBDA_FUNCTION_ARN}",
        "${LAMBDA_FUNCTION_ARN}:*"
      ]
    }
EOF2
    fi)
  ]
}
EOF
)"

CRAWLER_POLICY_JSON="$(cat <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "S3ReadForCrawler",
      "Effect": "Allow",
      "Action": [
        "s3:GetObject",
        "s3:ListBucket"
      ],
      "Resource": [
        "arn:aws:s3:::${S3_BUCKET_NAME}",
        "arn:aws:s3:::${S3_BUCKET_NAME}/*"
      ]
    }
  ]
}
EOF
)"

########################################
# Preview
########################################
echo
echo "========== PREVIEW =========="
echo "Region:                     $REGION"
echo "Account ID:                 $ACCOUNT_ID"
echo "Template file:              $TEMPLATE_FILE"
echo "Stack name:                 $STACK_NAME"
echo
echo "PipelineName:               $PIPELINE_NAME"
echo "S3BucketName:               $S3_BUCKET_NAME"
echo "KinesisStreamName:          $KINESIS_STREAM_NAME"
echo "FirehoseDeliveryStreamName: $FIREHOSE_DELIVERY_STREAM_NAME"
echo "GlueDatabaseName:           $GLUE_DATABASE_NAME"
echo "GlueCrawlerName:            $GLUE_CRAWLER_NAME"
echo
echo "Firehose Role Name:         $FIREHOSE_ROLE_NAME"
echo "Firehose Role ARN:          $FIREHOSE_ROLE_ARN"
echo "Glue Role Name:             $GLUE_CRAWLER_ROLE_NAME"
echo "Glue Role ARN:              $GLUE_CRAWLER_ROLE_ARN"
echo
echo "Enable Transformation:      $ENABLE_TRANSFORMATION"
echo "Lambda Function Name:       ${LAMBDA_FUNCTION_NAME:-<empty>}"
echo "Lambda Function ARN:        ${LAMBDA_FUNCTION_ARN:-<empty>}"
echo
echo "FirehoseBufferInterval:     $FIREHOSE_BUFFER_INTERVAL"
echo "FirehoseBufferSize:         $FIREHOSE_BUFFER_SIZE"
echo "============================="
echo

if ! confirm "Apply IAM patch and deploy stack now?"; then
  warn "Cancelled."
  exit 0
fi

########################################
# Apply policies
########################################
log "Applying Firehose inline policy..."
aws iam put-role-policy \
  --role-name "$FIREHOSE_ROLE_NAME" \
  --policy-name "$FIREHOSE_PATCH_POLICY_NAME" \
  --policy-document "$FIREHOSE_POLICY_JSON"

log "Applying Glue crawler inline policy..."
aws iam put-role-policy \
  --role-name "$GLUE_CRAWLER_ROLE_NAME" \
  --policy-name "$CRAWLER_PATCH_POLICY_NAME" \
  --policy-document "$CRAWLER_POLICY_JSON"

########################################
# Deploy CloudFormation
########################################
log "Deploying CloudFormation stack..."
aws cloudformation deploy \
  --region "$REGION" \
  --template-file "$TEMPLATE_FILE" \
  --stack-name "$STACK_NAME" \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides \
    PipelineName="$PIPELINE_NAME" \
    S3BucketName="$S3_BUCKET_NAME" \
    KinesisStreamName="$KINESIS_STREAM_NAME" \
    FirehoseDeliveryStreamName="$FIREHOSE_DELIVERY_STREAM_NAME" \
    GlueDatabaseName="$GLUE_DATABASE_NAME" \
    GlueCrawlerName="$GLUE_CRAWLER_NAME" \
    FirehoseRoleArn="$FIREHOSE_ROLE_ARN" \
    GlueCrawlerRoleArn="$GLUE_CRAWLER_ROLE_ARN" \
    EnableTransformation="$ENABLE_TRANSFORMATION" \
    TransformationLambdaArn="$LAMBDA_FUNCTION_ARN" \
    FirehoseBufferInterval="$FIREHOSE_BUFFER_INTERVAL" \
    FirehoseBufferSize="$FIREHOSE_BUFFER_SIZE"

########################################
# Show outputs
########################################
log "Deployment complete. Stack outputs:"
aws cloudformation describe-stacks \
  --region "$REGION" \
  --stack-name "$STACK_NAME" \
  --query 'Stacks[0].Outputs' \
  --output table