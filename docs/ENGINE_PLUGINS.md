# Building your own engine

Any script or service your team already uses can become a Fieldwork engine.
Register it in **Workspace settings → Engines** (requires `engine.manage`), then
attach it to a stage in **Deployment chain**.

Every result becomes a finding on the deployment. Someone other than the person
who ran the engine has to confirm it, and it's recorded in the audit trail.

## Webhook engines: Fieldwork calls you

Use for anything someone should run on demand from the deployment page.

**Request.** `POST <your url>`

```
Content-Type: application/json
X-Fieldwork-Timestamp: 1790000000
X-Fieldwork-Signature: sha256=<hex HMAC-SHA256(signing_secret, "<timestamp>.<raw body>")>

{
  "engine": "readiness",
  "run_id": "run_3f9a...",
  "input": "<whatever the person typed or pasted>",
  "requested_by": {"id": "usr_maya", "name": "Maya Chen", "role": "fde"},
  "deployment": {"id": "dep_castellan", "name": "...", "customer": "...", "stage": "golive",
                 "health": "blocked", "fields": {...}, "members": [...], "tasks": {...}},
  "workspace": "ten_meridian"
}
```

Verify the signature and reject timestamps more than 5 minutes old. See
`verify()` in `examples/engines/readiness_engine.py`.

**Response.** `200` with JSON:

```
{"summary": "Not ready: 1 open blocker · 5/7 items done",
 "status": "fail",
 "result": {"open_blockers": ["..."]}}
```

- `summary` (required): one line people will read.
- `status`: `pass`, `warn`, `fail` or `info`.
- `result`: any JSON object or list, shown under Details.

Limits: 20 s timeout, 1 MB response, no redirects. In SaaS mode the URL must be
https and can't resolve to a private or cloud-metadata address. Self-hosted
installs whose engines live on an internal network set
`FIELDWORK_ALLOW_PRIVATE_ENGINES=1`.

## Push engines: your script calls Fieldwork

Use for checks that have to run where the customer's systems are (behind a
firewall, on a schedule, in CI).

```
POST /api/ingest/findings
Authorization: Bearer fwe_...
Content-Type: application/json

{"deployment_id": "dep_castellan",
 "title": "Agent latency vs SLO (last 24h)",
 "summary": "p95 1840 ms against a 1500 ms SLO",
 "status": "fail",
 "result": {"p95_ms": 1840}}
```

An engine token can only post into its own workspace. The audit trail records
the engine itself (`engine:<key>`) as the actor.

## Credentials

- Webhook engines get a **signing secret**; push engines get an **engine token**.
- Each is shown once, at registration or rotation.
- Rotating replaces the old credential immediately.
- Removing an engine deletes its credential.
