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

### 1. Flow Management Toolkit

Migrates and updates Amazon Connect contact flows across instances, including dependency resolution and configuration updates.

| Script | Purpose |
|---|---|
| `flow_migration.py` | Bulk migrate flows (created in SAVED state) |
| `update_hoursofoperation_queue_disconnect_flow.py` | Fix blocking dependencies (Hours of Operation, Queue, Disconnect Flow) |
| `update_lambda.py` | Update Lambda function references |
| `update_lexbot.py` | Update Lex bot integrations |

> ⚠️ First two scripts must be executed before the others. Core dependencies must be resolved before Lambda / Lex updates can succeed.

📄 See: `contact_flow_migration/README.md`

---
### 2. Data Pipeline Toolkit

Automates deployment and processing of Amazon Connect CTR data pipelines.

| Tool | Purpose |
|---|---|
| `data_pipeline.yaml` + `deploy.sh` | Deploy full CTR data pipeline (S3, Kinesis, Firehose, Glue) |
| CTR Data Cleaning Lambda | Normalize and clean CTR attributes before storage |

**Capabilities:**

- End-to-end pipeline provisioning
- IAM automation
- Optional Lambda transformation integration

📄 See: `data_pipeline/README.md`

---
### 3. Number Management Toolkit

Provides automation for **phone number provisioning and configuration** in Amazon Connect.

| Tool | Purpose |
|---|---|
| `claim_numbers.yaml` | Bulk claim phone numbers using CloudFormation |
| `map_numbers_to_flows.py` | Associate phone numbers with contact flows and update descriptions |

**Key Notes:**

- CloudFormation is used for number claiming to avoid manual availability checks required by APIs
- Disable rollback during deployment to retain successfully claimed numbers in case of partial failure
- Particularly useful after number porting or bulk provisioning scenarios

---

### 4. Connect Resource Configuration Toolkit

Automates deployment and migration of supporting Amazon Connect configuration resources.

| Tool | Purpose |
|---|---|
| Hours_of_Operation Migration | Extracts HOO via API and generates CFN templates |
| Agent Status Deployment | Deploys agent statuses using CloudFormation |
| Evaluation Form Migration | Migrates evaluation forms (active versions only) |

**Key Notes:**

- Designed for cross-instance migration and configuration standardization
- Focuses on non-flow configuration resources

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
