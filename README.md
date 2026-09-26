# Fieldwork

The platform deployment teams build their methodology on.

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
pip install -r requirements.txt
python -m fieldwork seed
FIELDWORK_DEMO=1 FIELDWORK_ALLOW_PRIVATE_ENGINES=1 python -m fieldwork serve
```

To let the demo's own webhook engine answer, run this in a second terminal:

```
FIELDWORK_ENGINE_SECRET=fws_demo_readiness_signing_secret python examples/engines/readiness_engine.py
```

Open http://127.0.0.1:8000 and sign in as anyone on the demo team. **Meridian
AI** has white-labeled its workspace as "Meridian Deploy", plugged its own
go-live readiness script into the Go-live stage, and runs a latency probe
inside a customer's environment that pushes results in.

```
python -m pytest -q          # 29 tests
```

## The default template

| Role | Sees | Can |
|---|---|---|
| Head of Deployments | all deployments | everything, including settings, engines and people |
| Engagement Manager | their engagements | open deployments, move stages freely, staff, assign, confirm findings, match bench |
| Implementation Consultant, FDE, AI Engineer | deployments they're on | advance one stage, own tasks, run engines |
| Customer Stakeholder | their own deployment | read only |

Stages: Discover → Integrate → Test → Go-live → Adopt → Value.

Every row, stage and permission above is editable in Workspace settings.

## Engines

Engines do the diagnostic work at each stage. Their output becomes a
**finding**, which someone other than the person who ran the engine has to
confirm before it counts.

**Built in**, each an existing standalone project vendored unchanged (see [ENGINES.md](ENGINES.md)):

| Engine | Does | Status |
|---|---|---|
| Sendero | Two-level outlier test: is a friction point a BUILD problem or a TRAINING problem? | live |
| Threshold | Scores people against a deployment's staffing needs, gate by gate, with citations; never infers what it can't see | live |
| Cortex, Interface Census, conformance harness, Go-Live Command Center, Coyote attribution | | planned |

**Customer-built engines** ([docs/ENGINE_PLUGINS.md](docs/ENGINE_PLUGINS.md)):

- **Webhook engines.** Fieldwork calls the team's service when someone clicks Run, with a signed request (HMAC-SHA256). Example: `examples/engines/readiness_engine.py`.
- **Push engines.** The team's script runs wherever it needs to (CI, cron, inside the customer's firewall) and posts results in with an engine token. Example: `examples/engines/push_probe.py`.

Both examples use only the Python standard library, so they drop into any environment.

## Guarantees (each is a test)

- **Tenant isolation.** No read, write, assignment, engine run or engine token crosses workspaces. Another tenant's objects return 404, not 403.
- **Scoped access from the customer's own permission map.** Workspace-wide actions can't be granted at "own" scope.
- **Tamper-evident audit.** Every write, including engine runs and engine pushes, appends to a per-tenant SHA-256 hash chain in the same transaction. Editing any row is detected.
- **Two-person confirmation** on every finding.
- **Settings can't break the workspace.** Strict validation. Roles that people hold and stages with live deployments can't be deleted, and nobody can remove their own access to settings.
- **Engine safety.** Webhook calls are signed, refuse redirects, cap response size and time out. In SaaS mode they must use https and can't reach private or cloud-metadata addresses. Engine credentials are shown once, stored hashed where possible, and rotatable (rotation revokes the old one).

## Layout

```
fieldwork/app.py        API: auth, tenancy, scoped permissions, deployments, tasks, engines, audit
fieldwork/config.py     workspace config: branding, roles, permissions, views, stages, engines, fields
fieldwork/plugins.py    customer-built engines: signed webhook calls, push ingestion
fieldwork/audit.py      hash-chained audit log
fieldwork/db.py         schema (SQLite; portable SQL for Postgres)
fieldwork/engines/      built-in engine adapters + vendored cores
fieldwork/seed.py       demo workspace
frontend/index.html     the console, one file, no build step
examples/engines/       a webhook engine and a push engine to copy from
tests/                  29 tests
```

## Not built yet

- Real identity: SSO/OIDC for people, instead of bearer tokens.
- Postgres migrations and encryption at rest for webhook signing secrets (KMS).
- The five planned built-in engines.
- Pinning the resolved IP for webhook calls, to close DNS-rebinding gaps in the SSRF guard.
- A customer-safe task view (today a customer stakeholder sees every task on their deployment).
