# Fieldwork

The deployment engine for deployment teams: run every deployment, see why it stalls, and prove what it delivered.

Every software company that sells complex products has a deployment team:
engagement managers, implementation consultants, forward-deployed engineers,
AI engineers. Almost all of them run on tools they built themselves, because
nothing on the market fits how they work.

Fieldwork starts from one observation: **deployments work similarly enough
across companies to ship a strong default, and differently enough that every
team needs to make it its own.** So it ships a default template that works on
day one, and lets each team change all of it:

- **White-label it.** Their product name, accent color and logo.
- **Their roles and access.** Define roles and grant each action at *all* scope (every deployment) or *own* scope (only deployments they're staffed on).
- **Their views.** Choose what each role sees on its home screen.
- **Their methodology.** Stages, exit criteria and custom fields.
- **Their own scripts and integrations.** Plug them in as engines next to the built-ins.

Fieldwork doesn't sell a methodology. It's the system of record teams build theirs on.

## Run it

```
pip install -e ".[dev]"
fieldwork engines                          # the built-in engines and their inputs
fieldwork run census systems.csv           # run an engine on a file, no server
fieldwork seed                             # demo workspace
FIELDWORK_DEMO=1 FIELDWORK_ALLOW_PRIVATE_ENGINES=1 fieldwork serve
```

Open http://127.0.0.1:8000 for the console and http://127.0.0.1:8000/welcome for the landing page.
To let the demo's own webhook engine answer locally, also run
`FIELDWORK_ENGINE_SECRET=fws_demo_readiness_signing_secret python examples/engines/readiness_engine.py`.
Hosted demos serve that engine themselves.

**Deploy:** see [LAUNCH.md](LAUNCH.md): one-click Render blueprint (`render.yaml`), or `docker compose up` for Postgres locally.

```
python -m pytest -q                                              # 108 tests on SQLite (207 with Postgres too)
FIELDWORK_TEST_POSTGRES=postgresql://... python -m pytest -q     # plus the same tests on Postgres
```

## What people use every day

- **A home for each role.** Engineers see their deployments as cards, their tasks, what's blocked on the customer and their hours this week. Managers see capacity, unassigned work, flags and approvals. Directors see projects, the pipeline and the portfolio. Customers see only what's shared with them. Each workspace picks the sections per role. Light and dark mode.
- **Chains.** Every deployment on one screen: the stage chain, days in stage against the stage's target, and who the current delay belongs to.
- **Stages the way work actually happens.** Each stage is not started, in progress, done or skipped, and several can be in progress at once. Anyone staffed can move a deployment either way; a note is welcome, never required. Pause a deployment and the stage clock stops while the pause lands on the delay ledger.
- **Nothing gates the work.** Blocked is a label, not a stop: say who it's waiting on if you know (one tap), and the delay ledger records it. Engine verdicts, flags and conformance are advisory. The tool keeps the record; people make the calls.
- **One-tap time.** "Log today as planned" from home; adjust only when the day was different.
- **Delay ledger.** A blocked task, a blocked tracker issue or a stage past its target opens a delay with a *proposed* owner: the customer, your team, the model vendor or a software vendor. A person confirms or reassigns it. Batch decisions count a quarter as much as one-at-a-time ones, and delays provable from the signal (a model provider rate-limiting you) settle by rule with no weight. Once a signal has enough confirmations, the proposal learns your workspace's pattern and says why.
- **Flags.** Rules raise them on their own (stage past target, hours burning ahead of the calendar, a failed conformance gate, a task blocked more than 3 days) and clear them when the condition clears. People raise their own. A director can take one or hand it back to the team lead with a note.
- **Capacity.** Weekly hours, allocations per deployment, logged time (entered or imported), roll-offs and unassigned work.
- **Pipeline.** Deals from a CRM export, each with a staffing check: does the team have the weekly hours free for its first four weeks? Deals that fit alone but not together are called out.
- **Portfolio.** Contract value in flight, median days to go-live against your targets, median days per stage, utilization, and who owns the delay.
- **Agent approvals.** An agent asks before acting on a customer system; someone other than the requester approves or holds it.
- **Go-live checklist** on each deployment.
- **Today.** Blocked work, what's due, findings waiting for your confirmation, and deployments that moved or need attention.
- **Status reports, drafted for you.** Internal and customer versions, built only from the record: tasks, stage moves and confirmed findings. Share the customer version with one click, or copy it into an email.
- **Slack.** Alerts when work is blocked, assigned or waiting for confirmation, plus a weekday digest.
- **Two-way task sync** with GitHub Issues, Linear and Jira. Link a deployment to a repo, team or project and tasks flow both ways. Changes that arrive from the tracker are never echoed back.
- **Signed event webhooks** to feed any other system.
- **AI tools.** An MCP server at `/mcp` works with Claude Code, Claude Desktop, Cursor, Codex, Gemini CLI, the OpenAI API and the Grok API. The console's AI tools page has copy-paste setup for each. An agent acts as its person, with their permissions, and its changes are audited under their name.
- **Import.** Bring deployments and tasks in from a spreadsheet.

## The default template

| Role | Sees | Can |
|---|---|---|
| Head of Deployments | all deployments | everything, including settings, integrations, engines, people and sign-in |
| Engagement Manager | their engagements | open deployments, move stages freely, staff, assign, confirm findings, share with the customer, draft reports, match bench |
| Implementation Consultant, FDE, AI Engineer | deployments they're on | advance one stage, own tasks, run engines, draft reports |
| Customer Stakeholder | their own deployment, **shared items only** | read, and work tasks assigned to them |

Stages: Discover → Integrate → Test → Go-live → Adopt → Value. Every row, stage, permission and home screen is editable in Workspace settings.

## Engines

| Stage | Built-in engine | What it decides |
|---|---|---|
| Discover | **Census** | Which systems are ready, and which have ownership, access, documentation or data-control gaps. Writes the findings memo. |
| Integrate | **Cortex authority check** | Runs an agent's delegated authority through Cortex (vendored unchanged) against expected scenarios, and flags risky grants such as write actions with no human gate. |
| Test | **Conformance** | Pass rate by category against a bar; any critical miss fails the stage. |
| Go-live | **Command center** | Whether it's safe to stand down: no open sev1, sev2 under a limit, new issues trending down. |
| Adopt | **Sendero** | Whether friction is a BUILD or TRAINING problem (two-level outlier test). |
| Value | **Value study** | Difference in differences against a comparison group, with a permutation test. Refuses to call it below 8 people per group. |
| Value | **Value attribution** | Value against baseline, and every day of delay attributed to customer, vendor or third party. |
| any | **Threshold** (bench) | Who can staff the deployment, gate by gate, with citations. |

A stage can carry up to four engines. Teams plug in their own as signed webhooks or push engines ([docs/ENGINE_PLUGINS.md](docs/ENGINE_PLUGINS.md)). Every result is a finding that someone other than the runner confirms.

## Guarantees (each is a test)

- **Tenant isolation** across reads, writes, engine runs and tokens, inbound tracker webhooks, and sign-in. Another tenant's objects return 404.
- **Scoped permissions from the customer's own map.** New permissions in a release inherit from the closest existing one.
- **Customer-safe views.** Internal tasks, findings and reports never reach customer roles.
- **Tamper-evident audit** of every write, engine run, sign-in, AI-tool action and tracker change, on a per-tenant hash chain (advisory-locked on Postgres).
- **Secrets encrypted at rest** (Fernet, rotatable). Tokens are stored as hashes.
- **Company sign-in** over OIDC with PKCE, nonce, audience, expiry and domain checks. Required mode is available.
- **Outbound safety.** Every call to engines, Slack, trackers, webhooks and identity providers is guarded against private addresses, refuses redirects, is size-capped and times out. It's queued in an outbox with retries, never made while a request is open.
- **Inbound safety.** Tracker webhooks must be signed with the workspace's secret.

## Layout

```
fieldwork/app.py         core API: sign-in, tenancy, permissions, deployments, tasks, sharing, engines, audit
fieldwork/launch.py      Today, reports, integrations, import, personal tokens, operator metrics, access gate
fieldwork/ops.py         delay ledger, flags and the sweep, capacity, pipeline, portfolio, approvals, checklist
fieldwork/mcp.py         MCP server (/mcp) and stdio bridge
fieldwork/events.py      events, outbox worker, Slack, webhooks, digest
fieldwork/trackers.py    GitHub, Linear, Jira two-way sync
fieldwork/reports.py     status report drafting
fieldwork/config.py      workspace config and validation
fieldwork/db.py          SQLite / Postgres adapter and migrations
fieldwork/sso.py         OpenID Connect sign-in
fieldwork/crypto.py      encryption at rest
fieldwork/plugins.py     customer-built engines
fieldwork/engines/       stage engines, Sendero, Threshold, vendored Cortex core
fieldwork/static/        console (index.html) and landing page (welcome.html)
examples/engines/        a webhook engine and a push engine to copy from
```

## Not built yet

- OAuth for remote MCP, which ChatGPT and Claude.ai connectors require. The other AI tools work today.
- SAML and SCIM provisioning.
- Slack slash commands and direct messages (today: channel alerts and the digest).
- Assignee sync with trackers (status and title sync both ways today).
- AI drafting with each team's own model. Reports are deterministic today.
- Native Harvest, Toggl, Salesforce and HubSpot sync (CSV import today), calendar time off, and email silence as a delay signal.
- A connection pool, and IP pinning for outbound calls.
