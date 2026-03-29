# Amazon Connect Data Pipeline Deployment Toolkit
## Purpose

This toolkit is designed to automate the deployment of an Amazon Connect data pipeline in a consistent, repeatable, and production-friendly way.

In real-world projects, setting up a data pipeline for Amazon Connect (especially for CTR data) typically involves:

Creating multiple AWS resources (Kinesis, Firehose, S3, Glue)
Configuring IAM roles and permissions
Handling optional data transformation logic
Ensuring everything works together correctly

Manual setup through the AWS UI is:

Time-consuming
Error-prone (especially IAM permissions)
Hard to reproduce across environments

This toolkit solves these problems by providing a fully automated workflow using:

CloudFormation (CFN) → infrastructure provisioning
AWS CLI → deployment orchestration
JMESPath → dynamic value extraction (e.g., ARNs, account info)
Lambda → optional data transformation (e.g., attribute normalization)
## Included Components

This toolkit consists of three main parts:

### CloudFormation Template (data_pipeline.yaml)

Defines the full data pipeline infrastructure:

S3 bucket (data landing zone)
Kinesis Data Stream (data ingestion)
Kinesis Firehose (delivery to S3)
Glue Database (metadata layer)
Glue Crawler (schema discovery)

Key feature:

Supports optional Firehose Lambda transformation
Designed specifically for Amazon Connect CTR data pipelines
### Deployment Script (deploy.sh)

A helper script that automates:

Parameter input (CLI or interactive)
IAM role ARN lookup
Lambda ARN lookup (if transformation enabled)
Inline IAM policy generation and attachment
CloudFormation deployment

Key value:

Eliminates common deployment failures caused by missing IAM permissions
Provides a one-command deployment experience
Adds a confirmation step before execution
### Lambda Function (Optional)

This Lambda function is used for data transformation in Firehose.

Typical use case (CTR data):

Normalize attribute keys (e.g., lowercase, remove spaces/hyphens)
Clean inconsistent user-defined attributes
Standardize schema before data lands in S3

Important:

This Lambda is optional
Only required if EnableTransformation=true
## Deployment Workflow
### Step 1 – (Optional) Deploy Lambda

If you plan to enable transformation:

Create the Lambda manually via AWS Console or CLI
No need for additional automation (single function setup is simple)

Make sure:

Lambda is in the same region
It can be invoked by Firehose
### Step 2 – Prepare CloudFormation Template

Ensure your data_pipeline.yaml file is available locally.

### Step 3 – Run Deployment Script

Execute the script:

./deploy.sh

You can either:

Provide parameters via CLI
Or enter them interactively
Example CLI Input
./deploy.sh \
  --region us-east-1 \
  --stack my-connect-pipeline \
  --pipeline ctr \
  --bucket my-ctr-data-bucket \
  --stream ctr-stream \
  --firehose-stream ctr-firehose \
  --database ctr_db \
  --crawler ctr_crawler \
  --firehose-role my-firehose-role \
  --crawler-role my-glue-role \
  --enable-transformation true \
  --lambda my-transform-lambda \
  --buffer-interval 300 \
  --buffer-size 5
### Step 4 – Confirm Deployment

Before execution, the script will display a preview:

========== PREVIEW ==========
...
Apply IAM patch and deploy stack now? [y/N]:

Enter:

y

The script will then:

Patch IAM roles (inline policies)
Deploy CloudFormation stack
Output stack results
Step 5 – Verify Deployment

After deployment:

Check S3 bucket for incoming data
Verify Firehose is active
Run Glue Crawler
Query data via Athena
## Example Output

![alt text](image.png)

## Notes
IAM roles must already exist before running the script
This toolkit assumes consistent naming across resources
Inline policies are used for simplicity and automation
For enterprise setups, consider switching to managed IAM policies
## Summary

This toolkit provides a fully automated, end-to-end solution for deploying an Amazon Connect data pipeline.

It transforms a traditionally manual and error-prone process into:

a reproducible, script-driven deployment workflow

Key benefits:

Faster setup
Fewer IAM-related failures
Easy reuse across