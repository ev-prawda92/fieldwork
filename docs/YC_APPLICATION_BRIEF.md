# Fieldwork — YC application working brief

## Company description

AI deployment software for enterprise teams.

## What we are building

Fieldwork helps enterprise AI teams turn prototypes into governed production
workflows. It connects the deployment record to a versioned specification of the
workflow, agent, model, systems, tools, permissions, evaluations and business goal.
When the configuration changes, Fieldwork queues fresh evaluations, identifies
readiness gaps and prepares a rollout packet for a separate reviewer. Cortex,
our integrated authorization engine, checks what the agent is allowed to do.

The initial user is a head of forward-deployed engineering, deployment or
implementation at an AI vendor. Their team coordinates customer systems,
engineers and reviewers to put an AI workflow into production. The initial
commercial hypothesis is a self-service annual workspace license; the earlier
$25,000–$50,000 range is proposed pricing, not demonstrated willingness to pay.

## Initial wedge

Start with one repeatable workflow: a support agent using enterprise content and
CRM case records. Show a missing permission, a blocked write, the exact human
approval, a regression, a configuration change and a fresh evaluation requirement.
The broader deployment lifecycle remains available; this demo focuses on the
software that reduces repeated readiness and approval work.

## Working today

The repository contains a functioning deployment operations product with scoped
workspace access, customer participation, integrations, audit records and
commercial delivery records. This build adds versioned workflow specifications,
a graph, a model registry, evaluation queue/worker contract, trace assertions,
Cortex checks, human review packets, a remediation assistant and reviewed lessons.
The console and MCP expose this flow. The isolated demo runs without credentials.

## What we cannot claim yet

No customer count, revenue, paid pilot, time savings or production model result
is established by this build. Synthetic rehearsals demonstrate plumbing; they do
not demonstrate agent quality. Metadata proposals are not live workflow discovery.
The assistant plans and creates Fieldwork tasks; it does not autonomously deploy
customer systems. Production dispatch enforcement, permission verification and
real outcome measurement need an external runner and a real customer rollout.

## Why this founder

Evan Prawda's work in EHR implementation at Epic and enterprise technology
consulting at Box is relevant to the problem: deploying software into complex
customer environments, coordinating access and workflows, and demonstrating
value. Confirm exact employment dates and all quantified claims against the
current resume before using them in an application. Do not add unverified
customers, patents, teammates or advisor relationships.

## 90-second demo

1. Open a customer deployment and its versioned support workflow.
2. Show Box access missing and the deployment assistant's proposed remediation.
3. Inspect Cortex's deny-by-default policy; a Salesforce draft requires a
   separately authorized person to approve the exact action context.
4. Show a synthetic regression and how evaluation results stay attached to the
   configuration version. Clearly label the run synthetic.
5. Change the pinned model. A new version supersedes pending work and queues
   fresh evaluations; the previous evidence cannot unlock the new version.
6. Show the readiness boundary: real observed traces and a separate rollout
   review are required. The existing operations record tracks delivery and value.

Use `python scripts/ai_deployment_demo.py` for the reproducible API walkthrough.
For a full test-only review flow, run `pytest tests/test_ai_deployments.py`.
Before recording a demo of a working real model, connect the external runner and
show genuinely observed evidence. Do not relabel fixtures as production results.

## Evidence to collect before submission

- A design partner with a repeatable enterprise AI deployment workflow and an
  identified budget owner.
- One actual workflow evaluated through a pinned external agent runner, including
  a permission failure, regression and separately reviewed remediation.
- Baseline and observed deployment effort: engineering hours, time to readiness,
  review turnaround and repeated work. State sample sizes and limitations.
- A proposed pilot price and the buyer's actual response; distinguish an interview,
  letter of intent, signed pilot and paid license.
- Founder/team details, current location, availability, ownership and incorporation
  status; batch and application deadline must be checked when applying.

## Product sequence after this release

1. One trustworthy runner and enforced Cortex tool adapter for the first customer.
2. Live permission checks, runtime rollout/rollback and metrics ingestion.
3. Metadata-driven discovery from already connected systems.
4. Reusable deployment patterns grounded in reviewed customer evidence.

The investable claim to test is that a common deployment layer reduces repeated
engineering and review effort across customer environments. The code establishes
a testable product; customer results must establish that claim.
