# Amazon Connect Infrastructure Automation Toolkit

Toolkit for automating Amazon Connect resource migration and deployment using AWS APIs and CloudFormation.

---

## Background

I work as an AWS Connect engineer and regularly manage large numbers of Amazon Connect resources across multiple instances. In practice, many deployment and migration tasks are still performed manually through the AWS console UI, which becomes repetitive, slow, and error-prone when dealing with large environments.

After repeatedly facing these manual operations, I started building this toolkit to automate common tasks such as resource extraction, migration, and deployment across instances. The goal of this project is to gradually modularize and automate Amazon Connect infrastructure management using AWS APIs and Infrastructure-as-Code.

This project was also developed with significant assistance from AI tools, which helped accelerate experimentation, scripting, and design iterations.

---

## Roadmap

### Stage 1 – Resource Migration (Current)

The current stage focuses on **cross-instance resource migration**.

The toolkit can extract resources from one Amazon Connect instance and deploy them to another instance automatically.

Currently supported resources include:

- Hours of Operation  
- Agent Statuses  
- Contact Flows  
- Evaluation Forms  
- Phone Number claiming  

For **contact flows**, the toolkit also provides dependency repair tools that automatically update ARN references such as:

- Lambda functions  
- Lex bots  
- Queues  
- Hours of Operation  
- Disconnect flows  

After automated migration, users only need to **review the configuration in the AWS console UI** before activation.

---

### Stage 2 – Automated Deployment (Planned)

The second stage focuses on **automating initial resource deployment**.

Instead of extracting resources from an existing instance, the goal is to define a **standardized resource specification** that describes the required configuration.

Deployment will then be automated using:

- AWS APIs  
- CloudFormation templates  

The key challenge in this stage is designing a **standardized input format** that can represent Amazon Connect resources clearly and consistently.

---

### Stage 3 – AI-Assisted Automation (Future)

Once standardized deployment specifications are established, the third stage will introduce **AI-assisted configuration generation**.

The idea is to combine:

- RAG (Retrieval Augmented Generation)
- AI agent skills representing deployment APIs and CloudFormation templates

With this architecture, an AI agent could generate the required configuration inputs and execute deployment skills automatically.

The long-term goal is to enable **fully automated Amazon Connect infrastructure management**, where repetitive configuration tasks can be handled by AI-driven workflows.

---

## Project Goals

- Reduce manual configuration in Amazon Connect UI
- Automate cross-instance migration
- Enable Infrastructure-as-Code style management for Connect resources
- Gradually introduce AI-assisted infrastructure automation

---

## Disclaimer

This project is currently experimental and evolving. The toolkit is being actively developed based on real-world operational needs and will continue to expand as new automation opportunities are identified.