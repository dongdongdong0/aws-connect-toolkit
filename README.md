# Amazon Connect Infrastructure Automation Toolkit

Toolkit for automating repetitive Amazon Connect infrastructure tasks using AWS APIs and Infrastructure-as-Code.

---

## Background

I work as an AWS Connect engineer and regularly manage large numbers of resources across multiple instances. Many of these tasks are still performed manually through the AWS console, which becomes repetitive, time-consuming, and error-prone at scale.

This toolkit was built to automate these operations by combining AWS APIs and Infrastructure-as-Code, making Amazon Connect resource management more efficient and consistent.

---

## Overview

This project focuses on **automating template-based and repetitive work** when building and managing Amazon Connect environments.

It provides tools for:

- Cross-instance resource migration
- Automated resource deployment
- Data pipeline provisioning

The toolkit primarily leverages:

- AWS APIs
- CloudFormation (CFN)
- AWS CLI
- CDK (where applicable)

Future improvements may introduce **AI-assisted automation** to further simplify configuration generation and execution.

---

## Usage Notes

Before using the toolkit, please note:

1. Scripts with names ending in **`migration`** are typically used for **cross-instance resource migration**.

2. All `.py` files are designed to run in **AWS Lambda**.
   All `.yaml` / `.json` files are intended for **CloudFormation deployment**.

3. Each tool is designed for a specific use case.
   Please refer to the corresponding README files in each folder for detailed instructions and scenarios, and also refer to inline comments in the code for implementation details.

---

## Supported Tools

All tools listed below have been **used in production environments** and are continuously being improved.

---

### 1. Contact Flow Migration Toolkit

Migrates contact flows between Amazon Connect instances and automatically resolves dependencies that would otherwise block publishing.

| Script | Purpose |
|---|---|
| `flow_migration.py` | Bulk migrate flows (created in SAVED state) |
| `update_hoursofoperation_queue_disconnect_flow.py` | Fix blocking dependencies (Hours of Operation, Queue, Disconnect Flow) |
| `update_lambda.py` | Update Lambda function references |
| `update_lexbot.py` | Update Lex bot integrations |

> ⚠️ Firsr two scripts must be executed before the others. Core dependencies must be resolved before Lambda / Lex updates can succeed.

📄 See: `contact_flow_migration/README_flow_migration.md`

---

### 2. Connect Data Pipeline Toolkit

Automates end-to-end deployment of an Amazon Connect CTR data pipeline, including all required AWS resources and IAM configuration.

**Resources provisioned:**

- S3, Kinesis Data Stream, Kinesis Firehose, Glue Database, Glue Crawler

**Additional capabilities:**

- Automated IAM inline policy generation and attachment
- Optional Lambda transformation integration for CTR attribute normalization

> ⚠️ IAM roles must exist before running the deployment script. Resource naming must be consistent across components.

📄 See: `data_pipeline/README_pipeline.md`

---

### 3. Hours of Operation Migration

- Uses AWS APIs to extract Hours of Operation from a source instance
- Generates CloudFormation templates for deployment

> ⚠️ Does not support recurring overrides extraction.

---

### 4. Agent Status Deployment

- Deploys agent statuses using CloudFormation
- Eliminates the need for manual creation in the UI

---

### 5. Phone Number Claiming

- Uses CloudFormation to claim phone numbers

> ⚠️ Check service quotas before deployment. Disable rollback when deploying to avoid partial failures.

---

### 6. CTR Data Cleaning Lambda

- Cleans user-defined attributes in Connect CTR data
- Ensures schema consistency before data is stored in S3
- Designed to work alongside the data pipeline

---

### 7. Evaluation Form Migration

- Migrates evaluation forms between instances
- Only migrates **active versions** to avoid duplicates and incomplete drafts

---

### 8. Flow Mapping to Phone Numbers

- Maps contact flows to phone numbers after porting
- Supports bulk updates
- Automatically adds descriptions

---

### 9. Routing Profile Deployment

- Automates deployment of routing profiles into a target instance
- Reduces manual configuration effort

---

## Thoughts

During development and usage of this toolkit, a few key ideas stood out:

- **Practicality over complexity** — The tools may not be technically complex, but they solve real operational problems effectively.
- **Problem discovery is the core skill** — While AI significantly accelerated implementation, identifying the right problems to solve was the most valuable part.
- **Small improvements matter** — Even incremental efficiency gains can have meaningful impact in large-scale environments.

This project was built with the assistance of AI tools such as ChatGPT, which helped accelerate development and iteration.

---

## Disclaimer

This project is experimental and continuously evolving.
It is developed based on real-world operational needs and will expand as new automation opportunities are identified.