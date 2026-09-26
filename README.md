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
python -m pytest -q                                              # 70 tests on SQLite (130 with Postgres too)
FIELDWORK_TEST_POSTGRES=postgresql://... python -m pytest -q     # plus the same tests on Postgres
```

## What people use every day

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
- A connection pool, and IP pinning for outbound calls.
