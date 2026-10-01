# Fieldwork v0.9: AI deployment layer

Fieldwork keeps the deployment record. Cortex evaluates delegated agent authority.
An external agent runner executes model evaluations; a separately authorized
reviewer approves the resulting rollout packet. Existing deployment stages,
customer views, billing and integrations continue to work.

## What this release adds

| Capability | Implemented behavior |
|---|---|
| Workflow specification | Typed workflow, trigger, systems, data references, agent, pinned model, prompt, executable artifact, tools, scopes, human controls, KPI and evaluation cases |
| Deployment graph | Connected workflow/agent/model/tool/system/control/eval/outcome objects generated from the specification |
| Version registry | Immutable revisions and canonical fingerprints; compare-and-save rejects stale edits; model filtering in the console |
| Continuous evaluations | Every change queues a complete evaluation run. Dependency matches prioritize investigation; no old evidence silently carries forward |
| Evidence evaluation | Deterministic output, escalation, approval, latency, citation and tool-allowlist checks. Unknown tool calls, missing cases and ungated writes fail |
| Cortex | Compiles versioned tools into the existing vendored authorization engine. Deny by default; checks environment, target, data scope, permission metadata, evidence, financial and hourly limits |
| Human authority | Cortex action approvals use existing Fieldwork approvers and bind to requester, version and complete action context. Generic approvals cannot forge that binding |
| Production control | Cortex production requests require a separately reviewed current rollout packet and fresh passing observed evaluation evidence |
| Rollout packet | Freezes spec, version fingerprint, exact evaluation and readiness snapshot. Requester, spec author and evidence submitter cannot approve their own rollout |
| Deployment assistant | Rules-based gap analysis and remediation task creation; reviewed, atomic, replay-safe, respects workspace stage keys |
| Discovery | Proposes a workflow from explicitly supplied metadata and handoffs. Does not inspect connected systems or infer verified permissions |
| Deployment memory | Evidence-backed lessons require a second person to confirm; restricted to workspace and authorized deployment scope |
| Console and MCP | Workflow editor, graph, Cortex checks, rehearsal/observed runs, approvals, history/export, registry and agent tools |

## Try it

```bash
pip install -e ".[dev]"
python scripts/ai_deployment_demo.py
fieldwork seed
FIELDWORK_DEMO=1 fieldwork serve
```

Open the console, sign in as an FDE and open **Northfield → AI workflow**.
Review the example and create the first specification. The demo starts with
missing Box access, inactive Cortex authority and no executable artifact.
The deployment assistant can propose and create remediation tasks.

Review system access and the authority grants, pin the real executable agent
artifact, save a new version, then run a synthetic rehearsal. To test an actual
model, connect your external runner using the worker contract below. A rehearsal
cannot authorize production. Model or prompt changes queue a new evaluation run.

## External runner contract

`python scripts/ai_eval_worker.py` processes one pass of accessible pending jobs.
Schedule it in your existing job system; no background runner starts by default.
Configure `FIELDWORK_URL`, `FIELDWORK_TOKEN`, `FIELDWORK_AGENT_RUNNER_URL` and,
optionally, `FIELDWORK_AGENT_RUNNER_TOKEN`. The Fieldwork token never goes to the
runner. HTTPS is required except localhost development. Redirects are disabled.

The runner receives `{version_id, fingerprint, artifact_ref, spec}`. It must
execute every case's input against the pinned agent, then return those same three
identity fields, plus `runner`, `evidence_ref` and `traces`. Each trace contains
`case_id`, `output`, `tools_called`, `escalated`, `human_approved`, `latency_ms`,
and `citation_count`. Numeric and boolean values must use their JSON types.
The worker checks identity and submits the traces to Fieldwork in observed mode.

Incomplete cases fail. Duplicate/unknown case IDs and mismatched artifacts are
rejected. A latest failed run removes readiness even if an earlier run passed.
Evidence expires according to the versioned maximum age. Changed versions reject
old runs and supersede pending jobs. Worker errors leave uncompleted jobs pending.
Parallel runners can duplicate a run; this first worker has no distributed lease.

## API and MCP

The source of truth is `/api/deployments/{id}/ai`. Additional routes cover
`/spec`, `/example`, `/discover`, `/evals`, `/plan`, `/plan/apply`, `/cortex`,
`/cortex/check`, `/cortex/approvals`, `/releases`, `/releases/{id}/decide`,
`/lessons`, and `/lessons/{id}/confirm`. Workspace views are `/api/ai/registry`,
`/api/ai/eval-jobs` and `/api/ai/memory`. Schemas are in `/openapi.json`.

MCP exposes reading/versioning, evaluation submission, the evaluation queue,
remediation planning, Cortex checks and approval requests, rollout requests,
registry and memory. All use the caller's existing Fieldwork identity and API
permissions. MCP does not expose rollout approval as a convenience tool.

## Authorization and migration

Migration 11 preserves yesterday's self-service onboarding; migration 12 adds
the AI tables. All data belongs to a tenant and deployment. New editable permission
actions (`ai.view`, `ai.edit`, `ai.evaluate`, `ai.release`) inherit existing internal
finding, deployment edit, engine run and approval decision grants on upgrade.
Customer roles receive no AI internals by default. Explicit workspace configuration
can grant them if a customer chooses. Changes are audited in the same transaction.
Demo reset deletes the new child tables before existing parent rows.

## Evidence boundaries and next production work

Access metadata, trace results and action counters are caller-attested. Fieldwork
checks their consistency and attributes them; it does not attest runner execution,
validate live OAuth scope grants or verify that citations support their claims.
The current evaluation suite uses deterministic assertions, not semantic judges.
Rollout approval records readiness; it never deploys a model or executes a tool.
Cortex returns a decision, which an external runtime must enforce before dispatch.
Re-check before each action: an approval is not a perpetual authorization token.
An approved packet becomes ineffective on a failed/new run, expiry or version change.

The first customer deployment should add a runner with enforceable Cortex tool
wrapping, trustworthy action counters and evidence capture; live permission checks;
a production rollout/rollback adapter; metrics ingestion and alerting; and, if
multiple runners are needed, leased queue claims. Workflow discovery from live
connectors and tenant-wide statistical pattern mining are future work. The
existing adoption/value engines remain the source for measured customer outcomes;
a KPI target in a specification is not measured ROI.
