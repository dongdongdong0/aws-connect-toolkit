# Contact Flow Migration Toolkit

## 1. Purpose

This toolkit is designed to support **Amazon Connect contact flow migration across instances** in a structured and reliable way.

In real-world scenarios, migrating contact flows is not just a simple copy operation. After migration, flows often become **invalid** due to missing or mismatched dependencies, such as:

- Hours of Operation
- Queues
- Prompts
- Disconnect flows
- Contact flow modules
- Lambda functions
- Lex bots
- Q in Connect AI agents

If these dependencies are not correctly updated:

- The flow cannot be published in the UI
- The `UpdateContactFlowContent` API will fail
- Downstream resources (Lambda / Lex) cannot be properly attached

This toolkit provides a **step-by-step approach** to:

1. Migrate flows and modules in bulk, as unpublished shells
2. Rewrite every instance-scoped resource reference in one pass
3. Update references that live outside the instance (Lambda / Lex)
4. Prepare flows for publishing

### Prerequisite

These scripts rewrite references **by resource name**, so the resources must already exist on the target instance. Migrate the base resources first — Hours of Operation, Agent Statuses, Prompts, Queues, Quick Connects, Routing Profiles — otherwise every flow that references a missing resource will be skipped.

---

## 2. Included Tools

| Script | Stage | Notes |
|---|---|---|
| `01_migrate_flows.py` | Bulk flow migration | Creates flows in SAVED status |
| `02_migrate_modules.py` | Bulk module migration | Falls back to placeholder content |
| `03_update_modules.py` | Rewrite module content | Full content overwrite |
| `04_update_flows.py` | Rewrite flow content | Full content overwrite |
| `update_lambda.py` | Update Lambda references | Only when ARNs differ between environments |
| `update_lexbot.py` | Update Lex bot integrations | Only when bot or alias differs between environments |

### Superseded scripts

Two scripts from the earlier version of this toolkit are no longer needed:

| Old script | Replaced by | Why |
|---|---|---|
| `flow_migration.py` | `01_migrate_flows.py` | Same logic, parameterised for the command line |
| `update_hoursofoperation_queue_disconnect_flow.py` | `04_update_flows.py` | Covers those three resource types plus Prompts, Modules, Quick Connects, Routing Profiles and AI agents in a single pass |

---

## 3. Migration Strategy (Recommended Workflow)

> ⚠️ The order of execution is **critical**.

### Step 1 — Migrate flows and modules

```bash
python3 01_migrate_flows.py   -s <SOURCE_ID> -t <TARGET_ID> -r <REGION>
python3 02_migrate_modules.py -s <SOURCE_ID> -t <TARGET_ID> -r <REGION>
```

- Copies flows and modules from source to target
- Flows are created in **SAVED** status (not published)
- No dependency updates are performed at this stage
- These two can run in either order

**Why SAVED matters:** Connect does not validate referenced resources for a saved flow. That is what allows a flow to be created before its Lambda functions, queues and prompts exist on the target.

Modules have no SAVED equivalent, so `create_contact_flow_module` validates content on create. A module whose dependencies are missing fails with an error such as `PromptEntity@0 not found`; the script retries with minimal placeholder content so the module still gets an ID. **A module created with a placeholder is the normal path, not a failure** — its real content is written in Step 2.

---

### Step 2 — Rewrite content

```bash
python3 03_update_modules.py -s <SOURCE_ID> -t <TARGET_ID> -r <REGION> --dry-run
python3 04_update_flows.py   -s <SOURCE_ID> -t <TARGET_ID> -r <REGION> --dry-run
```

Drop `--dry-run` to apply. Run modules before flows, since flows reference modules.

This replaces the old "fix blocking dependencies first" step. Both scripts build one flat `{source ID: target ID}` table by listing every resource type on **both** instances and matching by name, then rewrite the whole content body in a single pass.

Because every reference is corrected at once, the old ordering constraint no longer applies. `UpdateContactFlowContent` validates the entire content body, so a single bad reference fails the whole write regardless of what you were trying to change — which is why the old toolkit had to fix Hours of Operation, Queues and Disconnect Flows before touching anything else.

**Scope selection** (flows only):

```bash
# everything present on both instances
python3 04_update_flows.py -s <SOURCE> -t <TARGET> -r <REGION>

# only flows whose name contains PRODUCTION
python3 04_update_flows.py -s <SOURCE> -t <TARGET> -r <REGION> --filter PRODUCTION

# one specific flow
python3 04_update_flows.py -s <SOURCE> -t <TARGET> -r <REGION> --name "Exact Flow Name"
```

**Q in Connect** is optional and off unless both assistant IDs are supplied:

```bash
python3 04_update_flows.py -s <SOURCE> -t <TARGET> -r <REGION> \
    --source-assistant-id <SRC_ASSISTANT> --target-assistant-id <TGT_ASSISTANT>
```

**Expect to run these more than once.** Before writing, each script scans the rewritten content for IDs that exist on the source but have no match on the target, and skips that flow or module while naming the missing resources. A flow that references another flow can only be written once that other flow has been written, so each pass unlocks more. Run until nothing is skipped.

A flow in SAVED state can be referenced by other flows — it does not need to be published first.

---

### Step 3 — Update references outside the instance

```bash
python3 update_lambda.py
python3 update_lexbot.py
```

Step 2 deliberately does **not** touch Lambda function ARNs or Lex bot alias ARNs. Those ARNs do not contain the instance ID, so when both instances share the same account, region, function and alias they are already correct and need no rewriting. They do have to be **associated** with the target instance.

Run these two scripts only when the target environment uses a *different* resource:

| Situation | Needed? |
|---|---|
| Same account, same Lambda, same Lex alias | No — the ARNs are already valid |
| Target uses `my-function-dev` instead of `my-function` | Yes — `update_lambda.py` |
| Target uses the `dev` alias instead of `prod` | Yes — `update_lexbot.py` |
| Target uses a different Lex bot entirely | Yes — `update_lexbot.py` |

> ⚠️ Associate the new Lambda or Lex alias with the target instance **before** rewriting flows to point at it, or the update will be rejected or will fail at runtime.

`update_lexbot.py` also rewrites the `Metadata.ActionMetadata` fields (`lexV2BotName`, `lexV2BotAliasName`, `displayName`). These hold human-readable names rather than IDs, so a name-based ID mapping cannot reach them. Without this, the flow runs correctly but the Connect flow editor still shows the old bot name.

---

### Step 4 — Validation & Publish

- [ ] Open flows in UI
- [ ] Validate all blocks
- [ ] Publish flows

The UI's publish error messages remain the most reliable way to find a dependency the scripts could not see.

---

## 4. Tool Descriptions

### `01_migrate_flows.py`

**Purpose:** Bulk migrate contact flows between instances.

| Attribute | Detail |
|---|---|
| Behavior | Skips existing flows (based on name); creates flows in **SAVED** state |
| Scope | `CONTACT_FLOW` type only |
| Limitations | Does not handle dependency updates; requires follow-up scripts |
| Known gap | No pagination (`MaxResults=1000`). Flows beyond the API's cap are silently missed |

---

### `02_migrate_modules.py`

**Purpose:** Bulk migrate contact flow modules between instances.

| Attribute | Detail |
|---|---|
| Behavior | Creates missing modules by name; retries with placeholder content when dependencies are missing |
| Limitations | `--update` replaces the instance ID only — use `03_update_modules.py` for a real content rewrite |
| Unverified | The placeholder retry path has not been exercised against a live instance. Watch the first run |

---

### `03_update_modules.py` / `04_update_flows.py`

**Purpose:** Rewrite module and flow content so every reference points at the target's own IDs.

**Covered resource types:** Queues, Prompts, Hours of Operation, Contact Flows, Contact Flow Modules, Quick Connects, Routing Profiles, and optionally the Q in Connect assistant and its AI agents.

**How it works:**

1. List each resource type on both instances and match by name
2. Build one flat `{source ID: target ID, source ARN: target ARN}` table
3. Replace the instance ID, then every table key, **longest key first** so a short ID cannot partially overwrite a longer string containing it
4. Write the whole body back

The replacement step is type-blind: once the table is built it is a flat string substitution over the content, with no per-block or per-resource-type handling to maintain. Resource types are enumerated only because Connect has no single "list everything" API — each type needs its own `list_*` call and response key.

**Known gap:** a flow referencing an instance-scoped resource type *not* in the list above keeps its source ID, and the pre-check cannot flag it because it only knows the types it enumerated. Candidates include Views, Case and Task templates, Voice ID and Customer Profiles domains, Traffic distribution groups, and AGENT-type queues (deliberately filtered out, since they have no `Name` field). The API may reject the write, or may accept it and leave the flow pointing at the source instance. Validate in the UI.

---

### `update_lambda.py`

**Purpose:** Update Lambda function references inside flows.

**When to use:** when the target environment uses a different function than the source. Not needed when both instances share the same function.

> **Note:** The function must be associated with the target instance, with an invoke permission for it, before flows are pointed at it.

---

### `update_lexbot.py`

**Purpose:** Update Lex bot integrations inside flows.

**When to use:** when the target environment uses a different bot or a different alias — for example `dev` instead of `prod`.

**What it rewrites:**

- `Actions[*].Parameters.LexV2Bot.AliasArn` — the runtime field
- `Metadata.ActionMetadata[*]` display fields — what the flow editor shows

`BOT_MAPPING` in the script maps source bot names to target bot names. Update it for your environment before use.

> **Note:** The script identifies the old bot from `Metadata.ActionMetadata[id].lexV2BotName`. A flow whose metadata is missing that field is skipped silently, and its runtime ARN is left unchanged.

---

## 5. Notes

- This toolkit assumes **consistent naming across instances** for everything inside the instance. Where names differ, the mapping is still the right place to fix it — see `BOT_MAPPING` in `update_lexbot.py` for the pattern
- Lambda and Lex are the realistic exceptions: the same logical resource often has a different name or alias per environment
- Not migrated by these scripts: phone numbers, users and their AGENT queues, `OutboundCallerConfig` on queues, Hours of Operation overrides, USER and PHONE_NUMBER quick connects
- Every script supports `--dry-run`, and exits non-zero when anything failed
- Always validate flows in the UI before publishing
- UI error messages are the most reliable way to identify missing dependencies

For detailed implementation logic, refer to the **inline comments in each script**.