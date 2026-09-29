"""Launch features: the parts that make Fieldwork a daily tool.

  Today            /api/today: what needs you now
  Reports          drafted internal and customer status reports
  Integrations     Slack, signed event webhooks, GitHub / Linear / Jira sync
  Import           bring existing deployments and tasks in from a spreadsheet
  Personal tokens  for AI tools (MCP) and scripts
  Operator         usage metrics for whoever runs the service
  Access gate      optional password in front of the whole site

register(app, d) wires these onto the app; `d` carries the helpers create_app
builds (auth context, deployment visibility, event emission).
"""


import csv
import hashlib
import hmac
import io
import json
import os
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from fastapi import Depends, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

from . import audit, config, db, events, plugins, reports, trackers

SECRET_NAMES = {"slack_webhook_url", "github_token", "linear_api_key", "jira_api_token",
                "github_webhook_secret", "linear_webhook_secret", "jira_webhook_secret"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def register(app, d) -> None:
    conn, ctx, Ctx = d.conn, d.ctx, d.Ctx

    # ------------------------------------------------------------------ today

    @app.get("/api/today")
    def today(c: Ctx = Depends(ctx)):
        now = _now()
        day = now.date().isoformat()
        soon = (now + timedelta(days=3)).date().isoformat()
        recent = (now - timedelta(hours=48)).isoformat()
        deps = {r["id"]: r for r in d.visible_deployments(c)}
        mine = [t for t in d.tasks(mine=True, deployment_id=None, c=c) if t["status"] != "done"]
        out = {
            "blocked": [t for t in mine if t["status"] == "blocked"],
            "due": [t for t in mine if t["status"] != "blocked" and t["due"] and t["due"] <= soon],
            "new": [t for t in mine if t["created_at"] >= recent and t["created_by"] != c.uid],
            "confirm": [], "moved": [], "attention": [],
        }
        out["overdue"] = sum(1 for t in mine if t["due"] and t["due"] < day)
        can_confirm = [k for k in deps if c.can_on("finding.confirm", k)]
        if can_confirm:
            rows = conn.execute(
                "SELECT f.id, f.title, f.engine, f.deployment_id, f.created_at, d.name deployment FROM findings f"
                " JOIN deployments d ON d.id=f.deployment_id WHERE f.tenant_id=? AND f.confirmed_by IS NULL"
                f" AND f.created_by!=? AND f.deployment_id IN ({','.join('?' * len(can_confirm))})"
                " ORDER BY f.created_at", (c.tenant_id, c.uid, *can_confirm)).fetchall()
            out["confirm"] = [dict(r) for r in rows]
        if deps:
            ids = list(deps)
            rows = conn.execute(
                "SELECT e.deployment_id, e.from_stage, e.to_stage, e.at, u.name actor FROM stage_events e"
                " JOIN users u ON u.id=e.actor_id WHERE e.from_stage IS NOT NULL AND e.at>=?"
                f" AND e.deployment_id IN ({','.join('?' * len(ids))}) ORDER BY e.id DESC LIMIT 20",
                (recent, *ids)).fetchall()
            out["moved"] = [{**dict(r), "deployment": deps[r["deployment_id"]]["name"]} for r in rows]
            out["attention"] = [{"id": k, "name": v["name"], "health": v["health"]} for k, v in deps.items()
                                if v["health"] != "on_track"]
        return out

    # ---------------------------------------------------------------- reports

    class ReportIn(BaseModel):
        audience: str = "internal"

    def report_out(r) -> dict:
        who = conn.execute("SELECT name FROM users WHERE id=?", (r["created_by"],)).fetchone()
        return {**dict(r), "created_by_name": who["name"] if who else r["created_by"]}

    @app.post("/api/deployments/{dep_id}/reports", status_code=201)
    def draft_report(dep_id: str, body: ReportIn, c: Ctx = Depends(ctx)):
        dep = c.deployment(dep_id)
        c.require_on("report.create", dep_id)
        if body.audience not in ("internal", "customer"):
            raise HTTPException(422, "audience must be internal or customer")
        if body.audience == "internal" and not c.can_on("task.view_internal", dep_id):
            raise HTTPException(403, "you can only draft customer reports")
        md = reports.build(conn, c.cfg, dep, body.audience)
        rid = "rpt_" + secrets.token_hex(6)
        with db.tx(conn):
            conn.execute("INSERT INTO reports (id, tenant_id, deployment_id, audience, body_md, visibility,"
                         " created_by, created_at) VALUES (?,?,?,?,?,?,?,?)",
                         (rid, c.tenant_id, dep_id, body.audience, md, "internal", c.uid, audit.now()))
            c.log("report.create", rid, {"deployment": dep_id, "audience": body.audience})
            d.emit(c, "report.created", dep_id, audience=body.audience)
        return {"id": rid, "body_md": md}

    @app.get("/api/deployments/{dep_id}/reports")
    def list_reports(dep_id: str, c: Ctx = Depends(ctx)):
        c.deployment(dep_id)
        vis = "" if c.can_on("task.view_internal", dep_id) else " AND visibility='shared'"
        rows = conn.execute(f"SELECT * FROM reports WHERE deployment_id=? AND tenant_id=?{vis}"
                            " ORDER BY created_at DESC LIMIT 20", (dep_id, c.tenant_id)).fetchall()
        return [report_out(r) for r in rows]

    class ShareIn(BaseModel):
        shared: bool

    @app.post("/api/reports/{rid}/share")
    def share_report(rid: str, body: ShareIn, c: Ctx = Depends(ctx)):
        r = conn.execute("SELECT * FROM reports WHERE id=? AND tenant_id=?", (rid, c.tenant_id)).fetchone()
        if not r:
            raise HTTPException(404, "report not found")
        c.deployment(r["deployment_id"])
        c.require_on("customer.share", r["deployment_id"])
        if body.shared and r["audience"] != "customer":
            raise HTTPException(409, "only customer reports can be shared; draft a customer version")
        vis = "shared" if body.shared else "internal"
        with db.tx(conn):
            conn.execute("UPDATE reports SET visibility=? WHERE id=?", (vis, rid))
            c.log("report.share", rid, {"visibility": vis})
        return {"visibility": vis}

    # ----------------------------------------------------------- integrations

    def webhook_urls(c: Ctx) -> dict:
        base = events.public_url()
        return {p: f"{base}/integrations/{c.tenant_slug}/{p}/webhook" for p in config.TRACKERS}

    @app.get("/api/integrations")
    def integrations(c: Ctx = Depends(ctx)):
        c.require("integrations.manage")
        have = {r["name"] for r in conn.execute("SELECT name FROM tenant_secrets WHERE tenant_id=?", (c.tenant_id,))}
        pending = conn.execute("SELECT kind, status, COUNT(*) n FROM outbox WHERE tenant_id=? GROUP BY kind, status",
                               (c.tenant_id,)).fetchall()
        errors = conn.execute("SELECT kind, last_error, created_at FROM outbox WHERE tenant_id=? AND last_error IS NOT NULL"
                              " ORDER BY id DESC LIMIT 5", (c.tenant_id,)).fetchall()
        return {"config": c.cfg["integrations"], "events": config.EVENTS,
                "secrets_set": sorted(n for n in have if n in SECRET_NAMES or n.startswith("webhook:")),
                "inbound_webhooks": webhook_urls(c),
                "delivery": [dict(r) for r in pending], "recent_errors": [dict(r) for r in errors]}

    class SecretIn(BaseModel):
        name: str
        value: str | None = Field(default=None, max_length=4000)

    @app.put("/api/integrations/secret")
    def set_secret(body: SecretIn, c: Ctx = Depends(ctx)):
        c.require("integrations.manage")
        hook_ids = {h["id"] for h in c.cfg["integrations"]["webhooks"]}
        if body.name not in SECRET_NAMES and not (body.name.startswith("webhook:") and body.name[8:] in hook_ids):
            raise HTTPException(422, f"unknown secret {body.name!r}")
        if body.name == "slack_webhook_url" and body.value:
            u = urlparse(body.value)
            if not (u.scheme == "https" and u.hostname == "hooks.slack.com") and not events._allow_private():
                raise HTTPException(422, "that isn't a Slack incoming-webhook URL (https://hooks.slack.com/...)")
        with db.tx(conn):
            events.set_secret(conn, c.tenant_id, body.name, body.value or None)
            c.log("integrations.secret", c.tenant_id, {"name": body.name, "set": bool(body.value)})
        return {"ok": True}

    @app.post("/api/integrations/{name}/generate-secret")
    def generate_secret(name: str, c: Ctx = Depends(ctx)):
        """Generate a signing secret for an inbound tracker webhook or an outbound event webhook."""
        c.require("integrations.manage")
        hook_ids = {h["id"] for h in c.cfg["integrations"]["webhooks"]}
        key = f"{name}_webhook_secret" if name in config.TRACKERS else (
            f"webhook:{name}" if name in hook_ids else None)
        if not key:
            raise HTTPException(404, "no such tracker or webhook")
        value = secrets.token_urlsafe(32)
        with db.tx(conn):
            events.set_secret(conn, c.tenant_id, key, value)
            c.log("integrations.secret", c.tenant_id, {"name": key, "set": True})
        return {"secret": value, "note": "shown once; paste it into the other side's webhook settings"}

    @app.post("/api/integrations/slack/test")
    def slack_test(c: Ctx = Depends(ctx)):
        c.require("integrations.manage")
        url = events.secret(conn, c.tenant_id, "slack_webhook_url")
        if not url:
            raise HTTPException(409, "add the Slack webhook URL first")
        code, resp = events.http_json(url, {"text": f":wave: {c.cfg['branding']['product_name']} is connected."})
        if code >= 300:
            raise HTTPException(502, f"Slack returned {code}: {resp}")
        return {"ok": True}

    @app.post("/api/digest")
    def send_digest(c: Ctx = Depends(ctx)):
        c.require("integrations.manage")
        if not c.cfg["integrations"]["slack"]["enabled"]:
            raise HTTPException(409, "Slack isn't enabled")
        with db.tx(conn):
            events.enqueue(conn, c.tenant_id, "slack", {"event": "digest",
                                                        "data": {"text": events.digest_text(conn, c.tenant_id, c.cfg)}})
        return {"queued": True}

    @app.get("/api/digest/preview")
    def digest_preview(c: Ctx = Depends(ctx)):
        c.require("integrations.manage")
        return {"text": events.digest_text(conn, c.tenant_id, c.cfg)}

    class SyncIn(BaseModel):
        provider: str | None = None
        target: str = ""

    @app.put("/api/deployments/{dep_id}/sync")
    def set_sync(dep_id: str, body: SyncIn, c: Ctx = Depends(ctx)):
        dep = c.deployment(dep_id)
        c.require_on("deployment.staff", dep_id)
        if body.provider:
            if body.provider not in config.TRACKERS:
                raise HTTPException(422, "provider must be github, linear or jira")
            if not trackers.available(conn, c.tenant_id, c.cfg, body.provider):
                raise HTTPException(409, f"{body.provider.title()} isn't connected for this workspace")
            if not body.target.strip():
                raise HTTPException(422, {"github": "repo as owner/name", "linear": "team ID",
                                          "jira": "project key"}[body.provider] + " is required")
            sync = {"provider": body.provider, "target": body.target.strip()}
            from .connect import core as cc
            cx = cc.active(conn, c.tenant_id, body.provider)
            if cx and sync["target"] not in (cx.extra.get("targets") or []):
                # The install acts with its installer's access. A repo, team or project it hasn't been used with
                # yet is an admin's call; after that, anyone who staffs a deployment can link to it.
                if not c.can("integrations.manage"):
                    raise HTTPException(403, f"{body.provider.title()} hasn't been linked to {sync['target']} yet;"
                                             " ask someone who manages integrations to link it first")
                with db.tx(conn):
                    cc.save(conn, cx, extra={**cx.extra, "targets": sorted({*(cx.extra.get("targets") or []),
                                                                             sync["target"]})})
        else:
            sync = {}
        with db.tx(conn):
            conn.execute("UPDATE deployments SET sync_json=? WHERE id=?", (json.dumps(sync), dep_id))
            c.log("deployment.sync", dep_id, sync or {"provider": None})
            if sync:  # bring existing tasks across
                dep = conn.execute("SELECT * FROM deployments WHERE id=?", (dep_id,)).fetchone()
                for t in list(conn.execute("SELECT id FROM tasks WHERE deployment_id=?", (dep_id,))):
                    trackers.queue_push(conn, c.tenant_id, c.cfg, dep, t["id"])
        hook = None
        if sync:  # with a one-click install, subscribe to changes on that repo/project too
            from .connect import trackers as live
            hook = live.ensure_webhook(conn, c.tenant_id, sync["provider"], sync["target"])
        return {"sync": sync, "webhook": hook}

    @app.get("/api/deployments/{dep_id}/links")
    def task_links(dep_id: str, c: Ctx = Depends(ctx)):
        c.deployment(dep_id)
        dep = conn.execute("SELECT sync_json FROM deployments WHERE id=?", (dep_id,)).fetchone()
        rows = conn.execute("SELECT l.* FROM task_links l JOIN tasks t ON t.id=l.task_id WHERE t.deployment_id=?",
                            (dep_id,)).fetchall()
        return {"sync": json.loads(dep["sync_json"] or "{}"),
                "links": {r["task_id"]: {"provider": r["provider"], "external_id": r["external_id"], "url": r["url"]}
                          for r in rows}}

    @app.post("/integrations/{slug}/{provider}/webhook", include_in_schema=False)
    async def inbound(slug: str, provider: str, request: Request):
        t = conn.execute("SELECT * FROM tenants WHERE slug=?", (slug,)).fetchone()
        if not t or provider not in trackers.ADAPTERS:
            raise HTTPException(404, "not found")
        secret = events.secret(conn, t["id"], f"{provider}_webhook_secret")
        body = await request.body()
        headers = {k.lower(): v for k, v in request.headers.items()}
        if not secret or not trackers.ADAPTERS[provider].verify(secret, headers, body):
            raise HTTPException(401, "bad signature")
        try:
            payload = json.loads(body)
        except ValueError:
            raise HTTPException(400, "not JSON")
        cfg = config.upgrade(json.loads(t["config_json"]))
        changes = trackers.ADAPTERS[provider].parse(headers, payload)
        changed = trackers.apply_inbound(conn, t["id"], provider, changes, cfg)
        return {"updated": changed}

    # ----------------------------------------------------------------- import

    class ImportIn(BaseModel):
        kind: str
        csv: str = Field(min_length=1, max_length=2_000_000)

    @app.post("/api/import")
    def do_import(body: ImportIn, c: Ctx = Depends(ctx)):
        rows = [{(k or "").strip().lower(): (v or "").strip() for k, v in r.items()}
                for r in csv.DictReader(io.StringIO(body.csv.strip()))]
        if not rows:
            raise HTTPException(422, "no rows")
        people = {r["email"].lower(): r for r in conn.execute("SELECT * FROM users WHERE tenant_id=?", (c.tenant_id,))}
        created, errors = 0, []
        if body.kind == "deployments":
            c.require("deployment.create")
            need = {"customer", "deployment"}
            if need - set(rows[0]):
                raise HTTPException(422, f"missing column(s): {', '.join(sorted(need - set(rows[0])))}")
            keys = config.stage_keys(c.cfg)
            fdefs = {f["key"] for f in c.cfg["fields"]["deployment"]}
            with db.tx(conn):
                for i, r in enumerate(rows, 2):
                    stage = r.get("stage", "").lower() or keys[0]
                    stage = next((s["key"] for s in c.cfg["stages"] if stage in (s["key"], s["name"].lower())), None)
                    health = (r.get("health") or "on_track").lower().replace(" ", "_")
                    lead = people.get(r.get("lead_email", "").lower()) if r.get("lead_email") else None
                    if not r["customer"] or not r["deployment"]:
                        errors.append(f"row {i}: customer and deployment are required"); continue
                    if not stage:
                        errors.append(f"row {i}: unknown stage {r.get('stage')!r}"); continue
                    if health not in ("on_track", "at_risk", "blocked"):
                        errors.append(f"row {i}: health must be on track, at risk or blocked"); continue
                    if r.get("lead_email") and not lead:
                        errors.append(f"row {i}: nobody in the workspace with email {r['lead_email']}"); continue
                    try:
                        fields = config.check_fields(c.cfg, "deployment", {k: v for k, v in r.items() if k in fdefs})
                    except config.ConfigError as e:
                        errors.append(f"row {i}: {e}"); continue
                    cust = conn.execute("SELECT id FROM customers WHERE tenant_id=? AND lower(name)=?",
                                        (c.tenant_id, r["customer"].lower())).fetchone()
                    ts = audit.now()
                    if cust:
                        cid = cust["id"]
                    else:
                        cid = "cus_" + secrets.token_hex(6)
                        conn.execute("INSERT INTO customers (id, tenant_id, name, industry, fields_json, created_at)"
                                     " VALUES (?,?,?,?,?,?)", (cid, c.tenant_id, r["customer"], r.get("industry", ""),
                                                             "{}", ts))
                    did = "dep_" + secrets.token_hex(6)
                    conn.execute("INSERT INTO deployments (id, tenant_id, customer_id, name, stage, health, lead_id,"
                                 " fields_json, staffing_req, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                                 (did, c.tenant_id, cid, r["deployment"], stage, health, lead["id"] if lead else None,
                                  json.dumps(fields), "", ts, ts))
                    for uid in sorted({lead["id"] if lead else None, c.uid} - {None}):
                        conn.execute("INSERT INTO deployment_members (deployment_id, user_id) VALUES (?,?)"
                                     " ON CONFLICT DO NOTHING", (did, uid))
                    conn.execute("INSERT INTO stage_events (tenant_id, deployment_id, from_stage, to_stage, actor_id,"
                                 " note, at) VALUES (?,?,?,?,?,?,?)", (c.tenant_id, did, None, stage, c.uid, "imported", ts))
                    c.log("deployment.create", did, {"name": r["deployment"], "stage": stage, "via": "import"})
                    created += 1
        elif body.kind == "tasks":
            need = {"deployment", "title"}
            if need - set(rows[0]):
                raise HTTPException(422, f"missing column(s): {', '.join(sorted(need - set(rows[0])))}")
            deps = {r["id"]: r for r in d.visible_deployments(c)}
            by_name = {r["name"].lower(): r for r in deps.values()}
            with db.tx(conn):
                for i, r in enumerate(rows, 2):
                    dep = deps.get(r["deployment"]) or by_name.get(r["deployment"].lower())
                    if not dep:
                        errors.append(f"row {i}: no deployment {r['deployment']!r} you can see"); continue
                    if not c.can_on("task.create", dep["id"]):
                        errors.append(f"row {i}: you can't create tasks on {dep['name']}"); continue
                    a = people.get(r.get("assignee_email", "").lower()) if r.get("assignee_email") else None
                    if r.get("assignee_email") and not a:
                        errors.append(f"row {i}: nobody with email {r['assignee_email']}"); continue
                    if a and a["id"] != c.uid and not c.can_on("task.assign", dep["id"]):
                        errors.append(f"row {i}: you can't assign tasks on {dep['name']}"); continue
                    status = (r.get("status") or "open").lower().replace(" ", "_")
                    if status not in ("open", "in_progress", "blocked", "done"):
                        errors.append(f"row {i}: bad status {r.get('status')!r}"); continue
                    vis = "shared" if r.get("share", "").lower() in ("yes", "y", "true", "1") else "internal"
                    if a and a["role"] and c.cfg["permissions"].get("task.view_internal", {}).get(a["role"]) is None:
                        vis = "shared"
                    if vis == "shared" and not c.can_on("customer.share", dep["id"]):
                        errors.append(f"row {i}: you can't share tasks with the customer"); continue
                    ts = audit.now()
                    tid = "tsk_" + secrets.token_hex(6)
                    conn.execute("INSERT INTO tasks (id, tenant_id, deployment_id, stage, title, assignee_id, status,"
                                 " due, created_by, created_at, updated_at, visibility) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                                 (tid, c.tenant_id, dep["id"], dep["stage"], r["title"][:200], a["id"] if a else c.uid,
                                  status, r.get("due") or None, c.uid, ts, ts, vis))
                    c.log("task.create", tid, {"deployment": dep["id"], "title": r["title"][:200], "via": "import"})
                    if status == "blocked":
                        from . import ops
                        ops.on_task_status(conn, c.tenant_id, c.tenant_name, c.cfg,
                                           {"id": tid, "deployment_id": dep["id"], "title": r["title"][:200],
                                            "stage": dep["stage"], "assignee_id": a["id"] if a else c.uid,
                                            "status": "open"}, "blocked")
                    trackers.queue_push(conn, c.tenant_id, c.cfg, dep, tid)
                    created += 1
        elif body.kind == "people":
            c.require("people.manage")
            if {"name", "email"} - set(rows[0]):
                raise HTTPException(422, f"missing column(s): {', '.join(sorted({'name', 'email'} - set(rows[0])))}")
            from .app import token_hash
            roles = {r["key"]: r["key"] for r in c.cfg["roles"]} | {r["name"].lower(): r["key"] for r in c.cfg["roles"]}
            default_role = next((r["key"] for r in c.cfg["roles"] if r["key"] == "fde"), c.cfg["roles"][0]["key"])
            seen = set()
            with db.tx(conn):
                for i, r in enumerate(rows, 2):
                    email = r.get("email", "").strip().lower()
                    if not r.get("name") or "@" not in email:
                        errors.append(f"row {i}: name and a valid email are required"); continue
                    if email in people or email in seen:
                        errors.append(f"row {i}: {email} is already in the workspace"); continue
                    role = roles.get((r.get("role") or "").strip().lower()) if r.get("role") else default_role
                    if not role:
                        errors.append(f"row {i}: unknown role {r.get('role')!r}"); continue
                    try:
                        hours = float(r.get("weekly_hours") or 40)
                        assert 0 <= hours <= 80
                    except (ValueError, AssertionError):
                        errors.append(f"row {i}: weekly_hours must be 0-80"); continue
                    uid = "usr_" + secrets.token_hex(6)
                    conn.execute("INSERT INTO users (id, tenant_id, name, email, role, manager_id, token_hash, profile_json,"
                                 " created_at, weekly_hours) VALUES (?,?,?,?,?,?,?,?,?,?)",
                                 (uid, c.tenant_id, r["name"][:120], email, role, None,
                                  token_hash("invited:" + secrets.token_hex(16)), "{}", audit.now(), hours))
                    seen.add(email)
                    created += 1
                if created:
                    c.log("people.import", c.tenant_id, {"added": created})
            from . import beta
            ways = [{"github": "GitHub", "google": "Google"}.get(w, w) for w in beta.configured()]
            note = (f"They sign in at {events.public_url()} with {' or '.join(ways)} using the email you listed."
                    if ways else "Add sign-in (GitHub, Google or company SSO) so they can get in.")
            return {"created": created, "errors": errors, "note": note}
        else:
            from . import ops
            created, errors = ops.import_rows(conn, c, body.kind, rows, people, d.visible_deployments(c))
        return {"created": created, "errors": errors}

    # -------------------------------------------------------- personal tokens

    class TokenIn(BaseModel):
        name: str = Field(min_length=1, max_length=80)

    @app.get("/api/me/tokens")
    def my_tokens(c: Ctx = Depends(ctx)):
        rows = conn.execute("SELECT token_hash, name, created_at, last_used_at FROM personal_tokens WHERE user_id=?"
                            " ORDER BY created_at DESC", (c.uid,)).fetchall()
        return [{"id": r["token_hash"][:12], "name": r["name"], "created_at": r["created_at"],
                 "last_used_at": r["last_used_at"]} for r in rows]

    @app.post("/api/me/tokens", status_code=201)
    def new_token(body: TokenIn, c: Ctx = Depends(ctx)):
        if c.via == "personal_token":
            raise HTTPException(403, "sign in to the console to create tokens")
        tok = "fwp_" + secrets.token_urlsafe(32)
        th = hashlib.sha256(tok.encode()).hexdigest()
        with db.tx(conn):
            conn.execute("INSERT INTO personal_tokens (token_hash, user_id, tenant_id, name, created_at)"
                         " VALUES (?,?,?,?,?)", (th, c.uid, c.tenant_id, body.name, audit.now()))
            c.log("token.create", c.uid, {"name": body.name, "id": th[:12]})
        return {"id": th[:12], "token": tok, "note": "shown once"}

    @app.delete("/api/me/tokens/{tid}")
    def revoke_token(tid: str, c: Ctx = Depends(ctx)):
        if len(tid) != 12:
            raise HTTPException(404, "token not found")
        with db.tx(conn):
            n = conn.execute("DELETE FROM personal_tokens WHERE user_id=? AND token_hash LIKE ?",
                             (c.uid, tid + "%")).rowcount
            if n:
                c.log("token.revoke", c.uid, {"id": tid})
        if not n:
            raise HTTPException(404, "token not found")
        return {"ok": True}

    # --------------------------------------------------------------- operator

    @app.get("/api/operator/metrics", include_in_schema=False)
    def operator_metrics(x_operator_token: str = Header(default="")):
        want = os.environ.get("FIELDWORK_OPERATOR_TOKEN", "")
        if not want or not hmac.compare_digest(want, x_operator_token):
            raise HTTPException(404, "not found")
        now = _now()
        out = []
        for t in list(conn.execute("SELECT id, name, created_at FROM tenants ORDER BY created_at")):
            def count(sql, since):
                return conn.execute(sql, (t["id"], since.isoformat() if isinstance(since, datetime) else since)).fetchone()["n"]
            w, m = now - timedelta(days=7), now - timedelta(days=28)
            out.append({
                "workspace": t["name"], "created_at": t["created_at"],
                "people": conn.execute("SELECT COUNT(*) n FROM users WHERE tenant_id=?", (t["id"],)).fetchone()["n"],
                "deployments": conn.execute("SELECT COUNT(*) n FROM deployments WHERE tenant_id=?", (t["id"],)).fetchone()["n"],
                "active_people_7d": count("SELECT COUNT(DISTINCT actor_id) n FROM audit WHERE tenant_id=? AND at>=?"
                                          " AND actor_id NOT LIKE 'engine:%' AND actor_id NOT LIKE 'tracker:%'",
                                          w.strftime("%Y-%m-%dT%H:%M:%S")),
                "engine_runs_7d": count("SELECT COUNT(*) n FROM findings WHERE tenant_id=? AND created_at>=?",
                                        w.strftime("%Y-%m-%dT%H:%M:%S")),
                "engine_runs_28d": count("SELECT COUNT(*) n FROM findings WHERE tenant_id=? AND created_at>=?",
                                         m.strftime("%Y-%m-%dT%H:%M:%S")),
                "custom_engines": len(config.upgrade(json.loads(conn.execute(
                    "SELECT config_json FROM tenants WHERE id=?", (t["id"],)).fetchone()["config_json"])).get("engines", [])),
                "writes_7d": count("SELECT COUNT(*) n FROM audit WHERE tenant_id=? AND at>=?",
                                   w.strftime("%Y-%m-%dT%H:%M:%S")),
            })
        active = [x for x in out if x["writes_7d"]]
        return {"workspaces": len(out), "active_workspaces_7d": len(active), "by_workspace": out}

    # ------------------------------------------------------------ access gate

    gate_pw = os.environ.get("FIELDWORK_ACCESS_PASSWORD", "")
    if gate_pw:
        cookie_val = hmac.new(gate_pw.encode(), b"fieldwork-gate", hashlib.sha256).hexdigest()
        # Machine-to-machine paths carry their own signature checks; the gate is for people.
        open_paths = ("/api/ingest/", "/integrations/", "/hooks/", "/oauth/", "/mcp", "/api/health", "/gate",
                      "/api/operator/")

        @app.middleware("http")
        async def gate(request: Request, call_next):
            p = request.url.path
            if (p.startswith(open_paths) or request.headers.get("authorization")
                    or hmac.compare_digest(request.cookies.get("fw_gate", ""), cookie_val)):
                return await call_next(request)
            if p.startswith("/api/"):
                return JSONResponse({"detail": "this site is private"}, status_code=401)
            return RedirectResponse("/gate", status_code=302)

        @app.get("/gate", include_in_schema=False)
        def gate_form(error: str = ""):
            return HTMLResponse(GATE_HTML.replace("{error}", "Wrong password." if error else ""))

        @app.post("/gate", include_in_schema=False)
        async def gate_submit(request: Request):
            form = (await request.body()).decode()
            pw = dict(x.split("=", 1) for x in form.split("&") if "=" in x).get("password", "")
            from urllib.parse import unquote_plus
            if hmac.compare_digest(unquote_plus(pw), gate_pw):
                r = RedirectResponse("/", status_code=303)
                r.set_cookie("fw_gate", cookie_val, httponly=True, samesite="lax", max_age=60 * 60 * 24 * 30,
                             secure=events.public_url().startswith("https"))
                return r
            return RedirectResponse("/gate?error=1", status_code=303)


GATE_HTML = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Private</title><style>
:root{--bg:#0e1014;--panel:#12151a;--line:#303642;--ink:#e8e6df;--dim:#9aa0a8;--accent:#e8b25c}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 system-ui,sans-serif;display:grid;place-items:center;min-height:100vh;padding:16px;box-sizing:border-box}
form{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:24px;width:100%;max-width:360px;display:grid;gap:12px}
input{font:inherit;color:var(--ink);background:var(--bg);border:1px solid var(--line);border-radius:6px;padding:9px}
button{font:inherit;font-weight:600;background:var(--accent);color:#1a1307;border:0;border-radius:6px;padding:9px;cursor:pointer}
p{margin:0;color:var(--dim)} .err{color:#e07a6f}</style></head><body>
<form method="post" action="/gate"><b>This site is private</b><p>Enter the access password.</p>
<input type="password" name="password" autofocus aria-label="Password"><span class="err">{error}</span><button>Continue</button></form></body></html>"""
