# Connect Resources Toolkit

## Overview

This folder is a collection of small, standalone tools for **migrating and provisioning Amazon Connect resources** — typically from one instance to another (DEV → UAT → PROD, or cross-region), but some tools also serve bulk provisioning on a single instance.

Each file is an independent tool. There is no shared framework: pick the one you need and run it on its own. Common traits across the Python tools:

- Resources are **matched by name**, and anything already present on the target is **skipped** — so re-running is safe.
- Most support **`--dry-run`** to preview before making changes.
- IDs that differ between instances (queues, hours of operation, flows) are **resolved by name**, not copied.
- Scripts use `boto3` and your ambient AWS credentials.

The numeric prefixes reflect a sensible **execution order** when doing a full migration — dependencies come first.

---

## Tools

### `01_sync_lambda.py`
Syncs **Lambda function associations** between two Connect instances. Finds Lambdas associated with the source but missing on the target, associates them, and adds the target instance to each Lambda's resource-based policy so it can be invoked. Both instances must be in the same account and region.

### `02_sync_lex_v2.py`
Syncs **Amazon Lex V2 bot associations**. The bots themselves are not copied — only the association is created, which works because both instances share an account and region. Handles the varying shapes of the `list_bots` response; `--debug` dumps the raw response.

### `03_migrate_resources.py`
The **all-in-one migration driver**. Runs six resource types in dependency order: hours of operation → agent statuses → prompts → queues → quick connects → routing profiles. Use `--only` to run a subset. This is the same logic as tools 04–09 bundled into one entry point; `--s3-bucket` is only needed for the prompts phase.

### `04_migrate_hours_of_operation.py`
Copies **Hours of Operation** profiles (timezone + weekly config) to the target. Run this before queues, which reference them. Does **not** copy overrides — see tool 13.

### `05_migrate_agent_statuses.py`
Copies **Agent Statuses**. The built-in `ROUTABLE` system status is skipped since every instance already has it and it cannot be created. No dependencies — can run at any point.

### `06_migrate_prompts.py`
Copies **prompt audio files**. Connect does not expose an S3 URI for prompt audio, so each prompt is moved in four steps: presigned download from the source → temp file → upload to an S3 bucket you own → `create_prompt` on the target. The staged S3 objects are deleted afterwards (`--keep-s3` to retain them). Requires an S3 bucket in the same region, readable by the Connect service role.

### `07_migrate_queues.py`
Copies **queues**, resolving each queue's Hours of Operation by name. A queue whose HoO is missing on the target is skipped rather than created incorrectly. Deliberately not copied: `AGENT`-type queues (auto-created per user) and `OutboundCallerConfig` (references numbers/flows that don't exist on the target).

### `08_migrate_quick_connects.py`
Copies **QUEUE-type quick connects**, resolving both the queue and the contact flow by name. `USER` and `PHONE_NUMBER` types are skipped (they reference agents and claimed numbers, neither of which these scripts migrate); `--verbose` lists what was skipped. Run after queues and flow migration.

### `09_migrate_routing_profiles.py`
Copies **routing profiles**. Resolves `DefaultOutboundQueueId` by name (profile skipped if that queue is missing) and copies per-channel queue assignments, dropping any whose queue doesn't exist on the target. Agent assignments are not migrated. Run after queues.

### `10_create_agent_status.yaml`
A **CloudFormation template** that creates a predefined set of offline agent statuses (Special Event, Project, …) with display order. Use this instead of tool 05 when you want the statuses declared as infrastructure rather than copied from another instance.

### `11_bulk_create_quick_connects.py`
**Bulk-creates QUEUE-type quick connects on a single instance** from a list you supply — not an instance-to-instance migration. Resolves queue names to IDs, finds the instance's built-in "Default queue transfer" flow, and creates each entry. Input can come from the `QUICK_CONNECTS` list inside the file, `--csv` (name, queue, description), or `--queues a,b,c`. Exits non-zero on any failure, so it fits in a pipeline.

### `12_migrate_evaluation_form.py`
Migrates **Evaluation Forms**, including **cross-region** (source and target region are separate flags). Compares by title, skips existing ones, and recreates each form with its title, description, items, and scoring strategy. Pagination is intentionally omitted — assumes fewer than 100 forms.

### `13_migrate_hoo_overrides.py`
Migrates **Hours of Operation overrides** (holiday / special-date exceptions). The HoO profiles must already exist on the target with the same name — run tool 04 first. Copies `RecurrenceConfig` and `OverrideType`, which matters: dropping the recurrence turns a narrow recurring rule into one long continuous block and makes every later override fail with an overlap error. `--hoo-name` (repeatable) limits it to specific profiles.

### `14_migrate_cases.py`
Migrates **Amazon Connect Cases** configuration in three stages: fields (including SingleSelect options), layouts, and templates — remapping field and layout IDs to their target equivalents as it goes. Both instances need Cases already enabled. Writes an ID mapping JSON (`--output`, default `cases_id_mapping.json`).

### `15_migrate_quick_responses.py`
Migrates **Quick Responses** between instances, matched by name.

### `16_claim_numbers.yaml`
A **CloudFormation template for bulk-claiming phone numbers**. CloudFormation is used instead of the API because it handles number availability and allocation internally — no pre-check logic needed. Phone number type is parameterized (DID, TOLL_FREE, …). Two deployment cautions: **check the instance's phone number quota first**, and **disable rollback** — with rollback enabled, one failed claim releases every number that succeeded.

### `17_map_phone_numbers.py`
**Maps phone numbers to contact flows and standardizes their descriptions** on one instance. Intended for after a port completes and the numbers are already in the instance but still unassigned. Derives the target flow name from a label using the convention `CS_<LABEL>_Voice` (e.g. `HD & ISQ FR` → `CS_HD_ISQ_FR_Voice`). That convention is environment-specific — adjust `build_flow_name()` for your own standard.

---

## Typical Full-Migration Order

```text
01 Lambda associations ─┐
02 Lex V2 associations ─┤  (independent, any time)
                        │
04 Hours of Operation ──┤
05 Agent statuses       │
06 Prompts              │
        ↓               │
07 Queues  (needs 04)   │   or run 03 to do 04–09 in one pass
        ↓               │
08 Quick connects (needs 07 + flows)
09 Routing profiles (needs 07)
        ↓
13 HoO overrides (needs 04)
12 / 14 / 15  Evaluation forms, Cases, Quick responses
        ↓
16 Claim numbers → 17 Map numbers to flows
```

> Not covered by these tools: contact flows and modules themselves, users/agents, and phone number porting. Run your flow/module migration between steps 07 and 08, since quick connects reference flows.
