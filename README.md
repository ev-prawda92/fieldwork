# Fieldwork

The operating platform for forward-deployed engineering teams.

Every AI company that sells into the enterprise now runs an FDE team, and every
one of those teams runs its deployments out of spreadsheets, Slack and a
project tracker built for something else. Fieldwork is the system of record
for that work. Each customer deployment is a chain of stages. Engines at each
stage do the diagnostic work, and every change is audited.

It's customizable SaaS. Each FDE org gets its own workspace and defines its own
stages, role names, permissions and custom fields.

## Run it

```
pip install -r requirements.txt
python -m fieldwork seed
FIELDWORK_DEMO=1 python -m fieldwork serve
```

Open http://127.0.0.1:8000 and sign in as any of the demo people. The demo
workspace is **Meridian AI**, a fictional FDE org running four customer
deployments. Or use Docker: `docker compose up`.

```
python -m pytest -q          # 18 tests
```

## Roles

| | FDE | FDE Manager | Director of Deployments |
|---|---|---|---|
| Sees | the deployments they're on | every deployment | every deployment |
| Moves stages | one step forward | any direction (back needs a note) | any direction |
| Tasks | creates their own, updates their own | creates and assigns to anyone | same |
| Engines | runs on their deployments | runs any, matches bench | same |
| Findings | — | confirms others' findings | same |
| Admin | — | reads audit trail | verifies the audit chain, edits workspace settings |

These are defaults. A director can change any of them in Workspace settings,
except that editing settings stays with directors so nobody gets locked out.

## Engines

Each engine is an existing standalone project, vendored unchanged and wrapped
by an adapter (`fieldwork/engines/__init__.py`). Source commits are in
[ENGINES.md](ENGINES.md).

| Stage | Engine | Status |
|---|---|---|
| Adopt | **Sendero**: two-level outlier test classifying a friction point as BUILD vs TRAINING | live |
| any (staffing) | **Threshold**: gate-by-gate bench matching with citations; never infers what it can't see | live |
| Integrate | Cortex: agent permissions and human approval gates | planned |
| Discover | Interface Census + advisory engine | planned |
| Test | Conformance harness | planned |
| Go-live | Sendero Go-Live Command Center | planned |
| Value | Coyote attribution | planned |

A planned engine shows as "not connected yet" in the console. Stage tracking
and tasks work on every stage today.

Engine output is saved as a **finding**. Someone other than the person who ran
the engine has to confirm it before it goes into the record.

## Guarantees (each one is a test in `tests/test_platform.py`)

- **Tenant isolation.** No read, write, assignment or engine run crosses workspaces. Another tenant's objects return 404, not 403, so their existence isn't leaked.
- **Least privilege by default.** FDEs see only their deployments, and role checks read the customer's own permission map.
- **Tamper-evident audit.** Every write appends to a per-tenant SHA-256 hash chain in the same transaction (Arbiter's construction). Editing any row is detected by `/api/audit/verify`.
- **Two-person confirmation** on engine findings.
- **Config can't corrupt the workspace.** Settings are strictly validated. A stage can't be deleted while deployments sit in it, and directors can't be locked out.

## Layout

```
fieldwork/app.py        API: auth, tenancy, roles, deployments, tasks, engines, audit
fieldwork/config.py     per-tenant config schema + validation (stages, fields, permissions)
fieldwork/audit.py      hash-chained audit log
fieldwork/db.py         schema (SQLite; portable SQL for Postgres)
fieldwork/engines/      adapters + vendored engine cores
fieldwork/seed.py       demo workspace
frontend/index.html     the console, one file, no build step
tests/                  18 tests
```

## Not built yet

- Real identity. Today it's bearer tokens; SSO/OIDC comes next, and Arbiter's `identity_federation.py` is the starting point.
- Postgres migrations (the schema is portable, but no migration tool is wired in).
- The five planned engines.
- Customer-facing executive view.
