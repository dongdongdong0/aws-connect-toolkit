# Contact Flow Migration Toolkit

## 1. Purpose

This toolkit is designed to support **Amazon Connect contact flow migration across instances** in a structured and reliable way.

In real-world scenarios, migrating contact flows is not just a simple copy operation. After migration, flows often become **invalid** due to missing or mismatched dependencies, such as:

- Hours of Operation  
- Queues  
- Disconnect flows  
- Lambda functions  
- Lex bots  

If these dependencies are not correctly updated:

- The flow cannot be published in the UI  
- The `UpdateContactFlowContent` API will fail  
- Downstream resources (Lambda / Lex) cannot be properly attached  

This toolkit provides a **step-by-step approach** to:

1. Migrate flows in bulk  
2. Fix critical dependencies first (blocking issues)  
3. Update functional components (Lambda / Lex)  
4. Prepare flows for publishing  

---

## 2. Included Tools

This folder contains the following scripts:

- `flow_migration.py`  
- `update_hoursofoperation_queue_disconnect_flow.py`  
- `update_lambda.py`  
- `update_lexbot.py`  

Each script is designed for a specific stage in the migration workflow.

---

## 3. Migration Strategy (Recommended Workflow)

The order of execution is **critical**.

### Step 1 – Flow Migration

Run:
flow_migration.py


- Migrates contact flows from source to target instance  
- Flows are created in **SAVED** status (not published)  
- No dependency updates are performed at this stage  

---

### Step 2 – Fix Blocking Dependencies

Run:
update_hoursofoperation_queue_disconnect_flow.py


This step is **mandatory before any further updates**.

Why:

- Hours of Operation, Queue, and Disconnect Flow are **core dependencies**
- If any of them are invalid:
  - The flow cannot be published in UI  
  - The flow is considered invalid internally  
  - Any further updates (Lambda / Lex) will fail  

💡 Recommended approach:

- Open the flow in **Amazon Connect UI**
- Attempt to publish
- Review error messages to identify missing/invalid resources
- Confirm resource names match target instance

---

### Step 3 – Update Functional Components

Run:
update_lambda.py
update_lexbot.py


After core dependencies are valid:

- Update Lambda integrations  
- Update Lex bot configurations  

At this stage:

- `UpdateContactFlowContent` API should work successfully  
- Flow structure is now fully functional  

---

### Step 4 – Validation & Publish

- Open flows in UI  
- Validate all blocks  
- Publish flows  

---

## 4. Tool Descriptions

### flow_migration.py

**Purpose:**
- Bulk migrate contact flows between instances  

**Behavior:**
- Skips existing flows (based on name)  
- Creates flows in **SAVED** state  

**Limitations:**
- Does not handle any dependency updates  
- Requires follow-up scripts  

---

### update_hoursofoperation_queue_disconnect_flow.py

**Purpose:**
- Fix **blocking dependencies** that prevent flow publishing  

**Scope:**
- Hours of Operation  
- Queue  
- Disconnect Flow  

**Important:**
- This step must be executed before any Lambda or Lex updates  

**Assumptions:**
- Resource names remain unchanged in target instance  

**If names differ:**
- A mapping layer should be introduced  
- Core logic of this script remains applicable  

---

### update_lambda.py

**Purpose:**
- Update Lambda function references inside flows  

**When to use:**
- After core dependencies are valid  
- When Lambda ARNs differ between instances  

**Notes:**
- Requires flows to be in a valid (publishable) state  

---

### update_lexbot.py

**Purpose:**
- Update Lex bot integrations inside flows  

**When to use:**
- After Lambda updates  
- After flow becomes structurally valid  

**Notes:**
- Depends on successful dependency resolution  

---

## 5. Notes

- This toolkit assumes **consistent naming across instances**
- For environments with different naming conventions:
  - Introduce mapping dictionaries  
- Always validate flows in UI before publishing  
- UI error messages are the most reliable way to identify missing dependencies  

For detailed implementation logic, please refer to **inline comments in each script**.