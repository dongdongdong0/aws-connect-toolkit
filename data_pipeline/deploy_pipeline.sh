#!/bin/bash

# ============================================================
# Amazon Connect Data Pipeline — End-to-End Deployment
#
# Purpose:
# Orchestrates the deployment of the full Connect data pipeline
# stack set, in the correct order. Each of the first three
# stacks is gated by an "enable" flag so operators can build
# base infrastructure once and re-deploy only the pipeline
# itself on later runs.
#
# Deployment order:
#   1. pipeline_roles.yaml       — Firehose + Glue Crawler IAM roles
#   2. clean_ctr_lambda.yaml     — CTR cleaning Lambda + role
#   3. glue_athena.yaml          — Glue Database + Athena workgroup +
#                                  query-result bucket
#   4. data_pipeline.yaml        — S3 landing bucket, Kinesis,
#                                  Firehose, Glue Crawler
#
# Enable flags (default false for every one):
#   --enable-roles       true|false
#   --enable-lambda      true|false
#   --enable-glue-athena true|false
#
# Stack #4 is always deployed. It looks up the ARNs of the
# resources it needs by NAME, so those resources must already
# exist by the time step 4 runs — either from an earlier
# --enable-* run of this script, or created out-of-band.
#
# Lambda attachment logic (stack #4):
#   Firehose gets the Lambda transformation attached ONLY when
#   BOTH:
#     - ENABLE_LAMBDA == true
#     - LAMBDA_FUNCTION_NAME is non-empty
#   Otherwise the Firehose delivery stream is deployed WITHOUT
#   any Lambda processor and the FirehoseInvokeLambdaPermission
#   is not created.
#
# IAM patching (stack #4):
#   The Firehose role and Glue Crawler role were created without
#   knowledge of the eventual S3 bucket name. This script patches
#   bucket-scoped S3 inline policies onto them just before
#   deploying stack #4.
#
# Requirements:
#   - AWS CLI configured
#   - The four YAML templates in this directory
# ============================================================
set -euo pipefail

########################################
# Helpers
########################################
log()  { echo "[INFO]  $1"; }
warn() { echo "[WARN]  $1"; }
err()  { echo "[ERROR] $1" >&2; }
hr()   { echo "------------------------------------------------------------"; }

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || {
    err "Required command not found: $1"; exit 1;
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

# All three enable flags default to false — nothing is created
# unless explicitly requested. Stack #4 always runs.
ENABLE_ROLES="false"
ENABLE_LAMBDA="false"
ENABLE_GLUE_ATHENA="false"

# Template files (in the current directory)
TEMPLATE_ROLES="pipeline_roles.yaml"
TEMPLATE_LAMBDA="clean_ctr_lambda.yaml"
TEMPLATE_GLUE_ATHENA="glue_athena.yaml"
TEMPLATE_PIPELINE="data_pipeline.yaml"

# Firehose buffer defaults
FIREHOSE_BUFFER_INTERVAL="300"
FIREHOSE_BUFFER_SIZE="5"

# Athena query-result prefix default
QUERY_RESULT_PREFIX="query-results/"

########################################
# Parse CLI parameters
########################################
usage() {
  cat <<USAGE
Usage: $0 [options]

Global:
  --region REGION                   AWS region (default: \$AWS_REGION or us-east-1)
  --pipeline NAME                   Pipeline logical name (required)
  --stack NAME                      CloudFormation stack name for stack #4 (required)

Enable flags (all default false):
  --enable-roles true|false
  --enable-lambda true|false
  --enable-glue-athena true|false

Names for stack #1 (roles):
  --firehose-role NAME              Firehose IAM role name (required)
  --crawler-role NAME               Glue Crawler IAM role name (required)

Names for stack #2 (lambda):
  --lambda NAME                     Lambda function name (required if enable-lambda=true
                                    OR if you want Firehose to invoke a pre-existing Lambda)
  --lambda-role NAME                Lambda execution role name (required if enable-lambda=true)

Names for stack #3 (glue+athena):
  --database NAME                   Glue Database name (required)
  --workgroup NAME                  Athena Workgroup name (required if enable-glue-athena=true)
  --athena-result-bucket NAME       S3 bucket for Athena query results (required if enable-glue-athena=true)
  --query-result-prefix PREFIX      Prefix inside that bucket (default: query-results/)

Names for stack #4 (pipeline):
  --bucket NAME                     Data landing S3 bucket (required)
  --stream NAME                     Kinesis Data Stream name (required)
  --firehose-stream NAME            Firehose delivery stream name (required)
  --crawler NAME                    Glue Crawler name (required)
  --buffer-interval N               Firehose buffer interval seconds (default: 300)
  --buffer-size N                   Firehose buffer size MB (default: 5)
USAGE
}

while [[ $# -gt 0 ]]; do
  case $1 in
    --region)                 REGION="$2"; shift 2 ;;
    --pipeline)               PIPELINE_NAME="$2"; shift 2 ;;
    --stack)                  STACK_NAME="$2"; shift 2 ;;

    --enable-roles)           ENABLE_ROLES="$2"; shift 2 ;;
    --enable-lambda)          ENABLE_LAMBDA="$2"; shift 2 ;;
    --enable-glue-athena)     ENABLE_GLUE_ATHENA="$2"; shift 2 ;;

    --firehose-role)          FIREHOSE_ROLE_NAME="$2"; shift 2 ;;
    --crawler-role)           GLUE_CRAWLER_ROLE_NAME="$2"; shift 2 ;;

    --lambda)                 LAMBDA_FUNCTION_NAME="$2"; shift 2 ;;
    --lambda-role)            LAMBDA_ROLE_NAME="$2"; shift 2 ;;

    --database)               GLUE_DATABASE_NAME="$2"; shift 2 ;;
    --workgroup)              ATHENA_WORKGROUP_NAME="$2"; shift 2 ;;
    --athena-result-bucket)   ATHENA_RESULT_BUCKET="$2"; shift 2 ;;
    --query-result-prefix)    QUERY_RESULT_PREFIX="$2"; shift 2 ;;

    --bucket)                 S3_BUCKET_NAME="$2"; shift 2 ;;
    --stream)                 KINESIS_STREAM_NAME="$2"; shift 2 ;;
    --firehose-stream)        FIREHOSE_DELIVERY_STREAM_NAME="$2"; shift 2 ;;
    --crawler)                GLUE_CRAWLER_NAME="$2"; shift 2 ;;
    --buffer-interval)        FIREHOSE_BUFFER_INTERVAL="$2"; shift 2 ;;
    --buffer-size)            FIREHOSE_BUFFER_SIZE="$2"; shift 2 ;;

    -h|--help)                usage; exit 0 ;;
    *) err "Unknown parameter: $1"; usage; exit 1 ;;
  esac
done

########################################
# Requirements
########################################
require_cmd aws

# Required at every run
: "${PIPELINE_NAME:?--pipeline is required}"
: "${STACK_NAME:?--stack is required (name of stack #4)}"
: "${FIREHOSE_ROLE_NAME:?--firehose-role is required}"
: "${GLUE_CRAWLER_ROLE_NAME:?--crawler-role is required}"
: "${GLUE_DATABASE_NAME:?--database is required}"
: "${S3_BUCKET_NAME:?--bucket is required}"
: "${KINESIS_STREAM_NAME:?--stream is required}"
: "${FIREHOSE_DELIVERY_STREAM_NAME:?--firehose-stream is required}"
: "${GLUE_CRAWLER_NAME:?--crawler is required}"

# Conditional requirements
if [[ "$ENABLE_LAMBDA" == "true" ]]; then
  : "${LAMBDA_FUNCTION_NAME:?--lambda is required when --enable-lambda=true}"
  : "${LAMBDA_ROLE_NAME:?--lambda-role is required when --enable-lambda=true}"
fi

if [[ "$ENABLE_GLUE_ATHENA" == "true" ]]; then
  : "${ATHENA_WORKGROUP_NAME:?--workgroup is required when --enable-glue-athena=true}"
  : "${ATHENA_RESULT_BUCKET:?--athena-result-bucket is required when --enable-glue-athena=true}"
fi

# Auto-derived stack names for the first three stacks
STACK_ROLES="${PIPELINE_NAME}-roles"
STACK_LAMBDA="${PIPELINE_NAME}-lambda"
STACK_GLUE_ATHENA="${PIPELINE_NAME}-glue-athena"

########################################
# Account lookup
########################################
log "Looking up account id..."
ACCOUNT_ID="$(aws sts get-caller-identity --query 'Account' --output text)"

########################################
# Preview
########################################
hr
echo "                    DEPLOYMENT PREVIEW"
hr
echo "Region                       : $REGION"
echo "Account ID                   : $ACCOUNT_ID"
echo "Pipeline name                : $PIPELINE_NAME"
echo
echo "Step 1 (roles)               : enable=$ENABLE_ROLES  stack=$STACK_ROLES"
echo "  Firehose role              : $FIREHOSE_ROLE_NAME"
echo "  Crawler role               : $GLUE_CRAWLER_ROLE_NAME"
echo
echo "Step 2 (lambda)              : enable=$ENABLE_LAMBDA  stack=$STACK_LAMBDA"
echo "  Lambda function            : ${LAMBDA_FUNCTION_NAME:-<none>}"
echo "  Lambda role                : ${LAMBDA_ROLE_NAME:-<none>}"
echo
echo "Step 3 (glue+athena)         : enable=$ENABLE_GLUE_ATHENA  stack=$STACK_GLUE_ATHENA"
echo "  Glue database              : $GLUE_DATABASE_NAME"
echo "  Athena workgroup           : ${ATHENA_WORKGROUP_NAME:-<none>}"
echo "  Athena result bucket       : ${ATHENA_RESULT_BUCKET:-<none>}"
echo "  Query result prefix        : $QUERY_RESULT_PREFIX"
echo
echo "Step 4 (data pipeline)       : ALWAYS RUN  stack=$STACK_NAME"
echo "  Data bucket                : $S3_BUCKET_NAME"
echo "  Kinesis stream             : $KINESIS_STREAM_NAME"
echo "  Firehose delivery stream   : $FIREHOSE_DELIVERY_STREAM_NAME"
echo "  Glue crawler               : $GLUE_CRAWLER_NAME"
echo "  Firehose buffer interval   : $FIREHOSE_BUFFER_INTERVAL sec"
echo "  Firehose buffer size       : $FIREHOSE_BUFFER_SIZE MB"
echo
echo "Firehose Lambda transform    : $(
  if [[ "$ENABLE_LAMBDA" == "true" && -n "${LAMBDA_FUNCTION_NAME:-}" ]]; then
    echo "YES (attach $LAMBDA_FUNCTION_NAME)"
  else
    echo "NO"
  fi)"
hr
echo

confirm "Proceed with deployment?" || { warn "Cancelled."; exit 0; }

########################################
# Step 1 — pipeline_roles.yaml
########################################
if [[ "$ENABLE_ROLES" == "true" ]]; then
  hr; log "Step 1: deploying $STACK_ROLES"; hr
  [[ -f "$TEMPLATE_ROLES" ]] || { err "Template not found: $TEMPLATE_ROLES"; exit 1; }

  aws cloudformation deploy \
    --region "$REGION" \
    --template-file "$TEMPLATE_ROLES" \
    --stack-name "$STACK_ROLES" \
    --capabilities CAPABILITY_NAMED_IAM \
    --parameter-overrides \
      GlueCrawlerRoleName="$GLUE_CRAWLER_ROLE_NAME" \
      FirehoseRoleName="$FIREHOSE_ROLE_NAME"
else
  log "Step 1 skipped (enable-roles=false)"
fi

########################################
# Step 2 — clean_ctr_lambda.yaml
########################################
if [[ "$ENABLE_LAMBDA" == "true" ]]; then
  hr; log "Step 2: deploying $STACK_LAMBDA"; hr
  [[ -f "$TEMPLATE_LAMBDA" ]] || { err "Template not found: $TEMPLATE_LAMBDA"; exit 1; }

  aws cloudformation deploy \
    --region "$REGION" \
    --template-file "$TEMPLATE_LAMBDA" \
    --stack-name "$STACK_LAMBDA" \
    --capabilities CAPABILITY_NAMED_IAM \
    --parameter-overrides \
      FunctionName="$LAMBDA_FUNCTION_NAME" \
      RoleName="$LAMBDA_ROLE_NAME"
else
  log "Step 2 skipped (enable-lambda=false)"
fi

########################################
# Step 3 — glue_athena.yaml
########################################
if [[ "$ENABLE_GLUE_ATHENA" == "true" ]]; then
  hr; log "Step 3: deploying $STACK_GLUE_ATHENA"; hr
  [[ -f "$TEMPLATE_GLUE_ATHENA" ]] || { err "Template not found: $TEMPLATE_GLUE_ATHENA"; exit 1; }

  aws cloudformation deploy \
    --region "$REGION" \
    --template-file "$TEMPLATE_GLUE_ATHENA" \
    --stack-name "$STACK_GLUE_ATHENA" \
    --parameter-overrides \
      GlueDatabaseName="$GLUE_DATABASE_NAME" \
      AthenaWorkgroupName="$ATHENA_WORKGROUP_NAME" \
      QueryResultBucketName="$ATHENA_RESULT_BUCKET" \
      QueryResultPrefix="$QUERY_RESULT_PREFIX"
else
  log "Step 3 skipped (enable-glue-athena=false)"
fi

########################################
# Step 4 — data_pipeline.yaml
# Always runs. Looks up ARNs by NAME.
########################################
hr; log "Step 4: preparing $STACK_NAME"; hr
[[ -f "$TEMPLATE_PIPELINE" ]] || { err "Template not found: $TEMPLATE_PIPELINE"; exit 1; }

log "Looking up Firehose role ARN ($FIREHOSE_ROLE_NAME)..."
FIREHOSE_ROLE_ARN="$(aws iam get-role \
  --role-name "$FIREHOSE_ROLE_NAME" \
  --query 'Role.Arn' --output text 2>/dev/null)" || {
    err "Firehose role not found: $FIREHOSE_ROLE_NAME"
    err "  Deploy pipeline_roles.yaml first (--enable-roles true) or check the name."
    exit 1
  }

log "Looking up Glue crawler role ARN ($GLUE_CRAWLER_ROLE_NAME)..."
GLUE_CRAWLER_ROLE_ARN="$(aws iam get-role \
  --role-name "$GLUE_CRAWLER_ROLE_NAME" \
  --query 'Role.Arn' --output text 2>/dev/null)" || {
    err "Glue crawler role not found: $GLUE_CRAWLER_ROLE_NAME"
    err "  Deploy pipeline_roles.yaml first (--enable-roles true) or check the name."
    exit 1
  }

# ---- Lambda attachment decision ----
# Attach Lambda transformation ONLY when
#   ENABLE_LAMBDA == true  AND  LAMBDA_FUNCTION_NAME non-empty.
LAMBDA_FUNCTION_ARN=""
ATTACH_LAMBDA="false"
if [[ "$ENABLE_LAMBDA" == "true" && -n "${LAMBDA_FUNCTION_NAME:-}" ]]; then
  ATTACH_LAMBDA="true"
  log "Looking up Lambda ARN ($LAMBDA_FUNCTION_NAME)..."
  LAMBDA_FUNCTION_ARN="$(aws lambda get-function \
    --region "$REGION" \
    --function-name "$LAMBDA_FUNCTION_NAME" \
    --query 'Configuration.FunctionArn' --output text 2>/dev/null)" || {
      err "Lambda function not found: $LAMBDA_FUNCTION_NAME"
      err "  Deploy clean_ctr_lambda.yaml first (--enable-lambda true) or check the name."
      exit 1
    }
fi

########################################
# Step 4 — patch bucket-scoped S3 policies
#
# The Firehose and Crawler roles were created without knowing
# the eventual data bucket name, so we attach an inline policy
# scoped to this specific bucket just before deploying stack #4.
#
# Lambda invoke and Kinesis read permissions are already in the
# role from pipeline_roles.yaml, so we do NOT patch them here.
########################################
FIREHOSE_PATCH_POLICY_NAME="DataPipelineFirehoseS3-${PIPELINE_NAME}"
CRAWLER_PATCH_POLICY_NAME="DataPipelineCrawlerS3-${PIPELINE_NAME}"

FIREHOSE_S3_POLICY_JSON="$(cat <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "S3WriteForPipeline",
      "Effect": "Allow",
      "Action": [
        "s3:AbortMultipartUpload",
        "s3:GetBucketLocation",
        "s3:GetObject",
        "s3:ListBucket",
        "s3:ListBucketMultipartUploads",
        "s3:PutObject"
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

CRAWLER_S3_POLICY_JSON="$(cat <<EOF
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

log "Patching Firehose role with S3 write policy ($FIREHOSE_PATCH_POLICY_NAME)..."
aws iam put-role-policy \
  --role-name "$FIREHOSE_ROLE_NAME" \
  --policy-name "$FIREHOSE_PATCH_POLICY_NAME" \
  --policy-document "$FIREHOSE_S3_POLICY_JSON"

log "Patching Glue crawler role with S3 read policy ($CRAWLER_PATCH_POLICY_NAME)..."
aws iam put-role-policy \
  --role-name "$GLUE_CRAWLER_ROLE_NAME" \
  --policy-name "$CRAWLER_PATCH_POLICY_NAME" \
  --policy-document "$CRAWLER_S3_POLICY_JSON"

########################################
# Step 4 — deploy CloudFormation
########################################
log "Deploying stack $STACK_NAME..."
aws cloudformation deploy \
  --region "$REGION" \
  --template-file "$TEMPLATE_PIPELINE" \
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
    EnableTransformation="$ATTACH_LAMBDA" \
    TransformationLambdaArn="$LAMBDA_FUNCTION_ARN" \
    FirehoseBufferInterval="$FIREHOSE_BUFFER_INTERVAL" \
    FirehoseBufferSize="$FIREHOSE_BUFFER_SIZE"

########################################
# Summary
########################################
hr
log "Deployment complete."
hr
echo "Stack #4 outputs:"
aws cloudformation describe-stacks \
  --region "$REGION" \
  --stack-name "$STACK_NAME" \
  --query 'Stacks[0].Outputs' \
  --output table

if [[ "$ENABLE_GLUE_ATHENA" == "true" ]]; then
  echo
  echo "Athena workgroup ready:"
  echo "  Workgroup       : $ATHENA_WORKGROUP_NAME"
  echo "  Result bucket   : s3://${ATHENA_RESULT_BUCKET}/${QUERY_RESULT_PREFIX}"
  echo "  Glue database   : $GLUE_DATABASE_NAME"
fi
