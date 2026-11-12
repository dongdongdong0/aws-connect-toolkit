# Amazon Connect Infrastructure Automation Toolkit

Toolkit for automating repetitive Amazon Connect infrastructure tasks using AWS APIs and Infrastructure-as-Code.

---

## Background

I work as an AWS Connect engineer and regularly manage large numbers of resources across multiple instances. Many of these tasks are still performed manually through the AWS console, which becomes repetitive, time-consuming, and error-prone at scale.

This toolkit was built to automate these operations by combining AWS APIs and Infrastructure-as-Code, making Amazon Connect resource management more efficient and consistent.

---

## Overview

The project covers three kinds of work:

- **Cross-instance migration** — moving the resources in one Connect instance to another instance, or across regions
- **Bulk provisioning** — creating many resources at once on a single instance (quick connects, agent statuses, phone numbers)
- **Infrastructure deployment** — the CTR data pipeline and the email-archive Lambdas, as repeatable deployments

Everything here is driven by `boto3`, the AWS CLI, CloudFormation and Terraform. All of it has been used on real instances.

---

## Repository map

| Directory | What it covers | Start here |
|---|---|---|
| [`connect_resources/`](connect_resources/) | 17 standalone tools for migrating and provisioning instance resources: Lambda/Lex associations, hours of operation, agent statuses, prompts, queues, quick connects, routing profiles, evaluation forms, Cases, quick responses, phone numbers | [README](connect_resources/README.md) |
| [`contact_flow_migration/`](contact_flow_migration/) | Contact flows and flow modules — a 4-stage pipeline that copies them as unpublished shells, then rewrites every instance-scoped reference to the target's own IDs | [README](contact_flow_migration/README.md) |
| [`data_pipeline/`](data_pipeline/) | CTR data pipeline as 4 CloudFormation stacks (IAM → cleaning Lambda → Glue/Athena → Kinesis/Firehose/S3) plus a shell orchestrator | [README](data_pipeline/README.md) |
| [`data_pipeline_tf/`](data_pipeline_tf/) | The same pipeline layer in Terraform, reusing the CFN-created roles. Side-by-side comparison of the two IaC approaches | [README](data_pipeline_tf/README.md) |
| [`email_archive/`](email_archive/) | Two Lambdas that archive inbound and outbound Connect emails to an external mailbox, threaded together | Module docstrings |

---

## Conventions

Three kinds of file live in this repo, and they are used differently:

| Kind | How it runs |
|---|---|
| **Numbered `.py` scripts** (`01_…`–`17_…`, `01_…`–`04_…`) | **Local CLI tools.** `python3 07_migrate_queues.py -s <SRC> -t <TGT> -r <REGION> [--dry-run]`, using your ambient AWS credentials. Not Lambda code. |
| **`.yaml` templates** (`10_`, `16_`, `data_pipeline/*.yaml`) | CloudFormation — deploy with `aws cloudformation deploy` or the provided shell script |
| **`email_archive/*.py`** | Genuine **Lambda** handlers, invoked from an *Invoke AWS Lambda* block in an email flow. Configuration is documented in each file's docstring. |

Shared behaviour across the migration scripts:

- **Numeric prefixes are the execution order** — dependencies come first
- **Matched by name, existing resources skipped** — re-running is safe
- **IDs are resolved by name, never copied** — a queue ID from the source is meaningless on the target
- **`--dry-run` almost everywhere**, and a non-zero exit when anything failed
- **Consistent naming across instances is assumed.** Where names genuinely differ per environment (Lambda functions, Lex aliases), an explicit mapping in the script is the place to fix it

---

## End-to-end migration order

The two migration folders interleave. A full instance-to-instance migration runs roughly:

```text
connect_resources/   01 Lambda associations
                     02 Lex V2 associations          (independent, any time)

connect_resources/   04 Hours of operation
                     05 Agent statuses               ← or run 03 to do 04–09 in one pass
                     06 Prompts
                     07 Queues         (needs 04)

contact_flow_migration/  01 Migrate flows  → 02 Migrate modules
                         03 Update modules → 04 Update flows   (expect several passes)

connect_resources/   08 Quick connects   (needs 07 + flows)
                     09 Routing profiles (needs 07)
                     13 HoO overrides    (needs 04)
                     12 / 14 / 15  Evaluation forms, Cases, quick responses
                     16 Claim numbers → 17 Map numbers to flows
```

Flows must exist before quick connects, because a quick connect points at a flow — that is why flow migration sits in the middle rather than at the end.

**Not covered by any script here:** users and their agents, phone number porting, `OutboundCallerConfig` on queues. Flows always need a final validate-and-publish pass in the UI; the UI's publish errors remain the most reliable way to find a dependency the scripts could not see.

---

## Thoughts

During development and usage of this toolkit, a few key ideas stood out:

- **Practicality over complexity** — The tools are not technically complex, but they solve real operational problems effectively.
- **Problem discovery is the core skill** — While AI significantly accelerated implementation, identifying the right problems to solve was the most valuable part.
- **Name-based resolution is the one idea that generalises** — Connect IDs are instance-scoped and worthless across instances; names are the only stable identity, and almost every script in here is a variation on "list both sides, match by name, rewrite".
- **Small improvements matter** — Even incremental efficiency gains have meaningful impact in large-scale environments.

This project was built with the assistance of AI tools, which helped accelerate development and iteration.

---

## Disclaimer

This project is experimental and continuously evolving.
It is developed based on real-world operational needs and will expand as new automation opportunities are identified.
Always run with `--dry-run` first, and validate in a non-production instance before pointing any of this at PROD.
