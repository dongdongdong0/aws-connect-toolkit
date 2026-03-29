# Number Management Toolkit

## Overview

This module provides automation tools for managing Amazon Connect phone numbers, including:

- **Claiming phone numbers in batch**
- **Mapping phone numbers to contact flows**
- **Standardizing descriptions and configurations**

It is designed to reduce manual work in the AWS Console and improve consistency, especially in scenarios involving **number porting, backup number provisioning, and large-scale configuration**.

---

## Included Tools

### 1. `claim-numbers.yaml`

A CloudFormation template used to **bulk claim phone numbers** for an Amazon Connect instance.

#### Why use CloudFormation instead of API?

When claiming phone numbers, there are two common approaches:

- **AWS API (e.g., `SearchAvailablePhoneNumbers` + `ClaimPhoneNumber`)**
- **CloudFormation (`AWS::Connect::PhoneNumber`)**

This toolkit uses **CloudFormation** for the following reasons:

- ✅ **No need to manually check number availability**  
  CloudFormation handles number allocation internally, eliminating the need for pre-check logic

- ✅ **Simpler and more declarative**  
  You define the desired state (number type, country, target instance), and AWS handles provisioning

- ✅ **Better for batch operations**  
  Easier to scale when claiming multiple numbers at once

---

#### ⚠️ Important Deployment Notes

- **Check phone number quota before deployment**
  - Amazon Connect instances have limits on how many numbers can be claimed
  - Exceeding quota will cause stack failures

- **Disable rollback when deploying**
  - If rollback is enabled, a failure in any single number claim will cause:
    → the entire stack to roll back  
    → all successfully claimed numbers to be released ❌

  - If rollback is disabled:
    → successfully claimed numbers are retained ✅  
    → only failed resources need to be retried  

---

### 2. `map_numbers_to_flows.py`

A Python script used to:

- Associate phone numbers with contact flows
- Update phone number descriptions
- Apply consistent naming conventions

#### Typical Use Case

After numbers are:

- Ported into Amazon Connect  
  or  
- Claimed via CloudFormation  

They still need to be:

1. Mapped to the correct contact flow  
2. Updated with a standardized description  

This script automates that process.

---

## Typical Workflow

```text
1. Claim numbers (CloudFormation)
        ↓
2. Numbers appear in Connect instance
        ↓
3. Run mapping script
        ↓
4. Numbers are linked to flows and configured