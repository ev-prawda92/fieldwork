"""Fieldwork API.

Every request is authenticated by bearer token, resolved to one user in one
tenant. Every query is filtered by that tenant. Every write is recorded in the
tenant's hash-chained audit trail inside the same transaction.

Authorization reads the tenant's own permission map (config.py). Each grant
has a scope: "all" (every deployment in the workspace) or "own" (only
deployments the person is staffed on). Nothing about any role is hardcoded
here; what an Engagement Manager or FDE may do is the customer's decision.
"""

import hashlib
import json
import os
import secrets
import sqlite3
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from . import audit, config, db, engines, plugins

FRONTEND = Path(__file__).resolve().parent.parent / "frontend"


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(6)}"


def jloads(s: str | None) -> Any:
    return json.loads(s) if s else {}


def allow_private_engines() -> bool:
    return os.environ.get("FIELDWORK_ALLOW_PRIVATE_ENGINES") == "1"


# ------------------------------------------------------------------ context

class Ctx:
    def __init__(self, conn: sqlite3.Connection, user: sqlite3.Row, tenant: sqlite3.Row):
        self.conn = conn
        self.user = user
        self.tenant_id = tenant["id"]
        self.tenant_name = tenant["name"]
        self.cfg = json.loads(tenant["config_json"])

    @property
    def uid(self) -> str:
        return self.user["id"]

    @property
    def role(self) -> str:
        return self.user["role"]

    def scope(self, action: str) -> str | None:
        return self.cfg["permissions"].get(action, {}).get(self.role)

    def can(self, action: str) -> bool:
        """Has the permission at any scope."""
        return self.scope(action) is not None

    def is_member(self, dep_id: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM deployment_members WHERE deployment_id=? AND user_id=?",
            (dep_id, self.uid)).fetchone() is not None

    def can_on(self, action: str, dep_id: str) -> bool:
        s = self.scope(action)
        return s == "all" or (s == "own" and self.is_member(dep_id))

    def require(self, action: str) -> None:
        if not self.can(action):
            raise HTTPException(403, f"your role can't {config.ACTIONS[action].lower()}")

    def require_on(self, action: str, dep_id: str) -> None:
        if not self.can_on(action, dep_id):
            s = self.scope(action)
            why = " on deployments you're not staffed on" if s == "own" else ""
            raise HTTPException(403, f"your role can't {config.ACTIONS[action].lower()}{why}")

    def deployment(self, dep_id: str) -> sqlite3.Row:
        """Fetch a deployment this user may see, or 404 (never 403: don't leak existence)."""
        row = self.conn.execute("SELECT * FROM deployments WHERE id=? AND tenant_id=?",
                                (dep_id, self.tenant_id)).fetchone()
        if not row or not self.can_on("deployment.view", dep_id):
            raise HTTPException(404, "deployment not found")
        return row

    def log(self, action: str, subject: str, detail: dict | None = None, actor: str | None = None) -> None:
        audit.record(self.conn, self.tenant_id, actor or self.uid, action, subject, detail)


def custom_engine(cfg: dict, key: str) -> dict | None:
    return next((e for e in cfg.get("engines", []) if e["key"] == key), None)


def engine_catalog(cfg: dict) -> dict:
    cat = {k: {**v, "key": k, "builtin": True} for k, v in engines.REGISTRY.items()}
    for e in cfg.get("engines", []):
        cat[e["key"]] = {**e, "status": "live", "builtin": False}
    return cat


def create_app(db_file: str | None = None) -> FastAPI:
    conn = db.connect(db_file)
    db.init(conn)
    app = FastAPI(title="Fieldwork", version="0.2.0",
                  description="The platform deployment teams build their methodology on")
    app.state.conn = conn

    def ctx(authorization: str = Header(default="")) -> Ctx:
        if not authorization.lower().startswith("bearer "):
            raise HTTPException(401, "missing bearer token")
        tok = authorization.split(" ", 1)[1].strip()
        user = conn.execute("SELECT * FROM users WHERE token_hash=?", (token_hash(tok),)).fetchone()
        if not user:
            raise HTTPException(401, "invalid token")
        tenant = conn.execute("SELECT * FROM tenants WHERE id=?", (user["tenant_id"],)).fetchone()
        return Ctx(conn, user, tenant)

    @app.exception_handler(config.ConfigError)
    async def _cfg_err(_: Request, exc: config.ConfigError):
        return JSONResponse({"detail": str(exc)}, status_code=422)

    @app.exception_handler(engines.EngineError)
    async def _eng_err(_: Request, exc: engines.EngineError):
        return JSONResponse({"detail": str(exc)}, status_code=422)

    @app.exception_handler(plugins.PluginError)
    async def _plug_err(_: Request, exc: plugins.PluginError):
        return JSONResponse({"detail": str(exc)}, status_code=502)

    # --------------------------------------------------------------- shell

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(FRONTEND / "index.html")

    @app.get("/api/health")
    def health():
        return {"ok": True}

    @app.get("/api/demo")
    def demo_logins():
        """Sign-in shortcuts for the demo workspace. Off unless FIELDWORK_DEMO=1."""
        if os.environ.get("FIELDWORK_DEMO") != "1":
            raise HTTPException(404, "not found")
        from .seed import DEMO_LOGINS
        return DEMO_LOGINS

    @app.get("/api/demo/sample-csv")
    def demo_csv():
        if os.environ.get("FIELDWORK_DEMO") != "1":
            raise HTTPException(404, "not found")
        from .seed import northfield_adoption_csv
        return {"csv": northfield_adoption_csv(), "metric": "minutes_per_exception", "baseline": 6}

    @app.get("/api/demo/sample-input/{engine_key}")
    def demo_input(engine_key: str):
        if os.environ.get("FIELDWORK_DEMO") != "1":
            raise HTTPException(404, "not found")
        from .seed import READINESS_CHECKLIST
        if engine_key != "readiness":
            raise HTTPException(404, "no sample for this engine")
        return {"input": READINESS_CHECKLIST}

    @app.get("/api/me")
    def me(c: Ctx = Depends(ctx)):
        return {
            "user": {"id": c.uid, "name": c.user["name"], "email": c.user["email"], "role": c.role,
                     "role_name": config.role_name(c.cfg, c.role)},
            "tenant": {"id": c.tenant_id, "name": c.tenant_name},
            "branding": c.cfg["branding"],
            "can": {a: c.scope(a) for a in config.ACTIONS if c.can(a)},
            "home": c.cfg["views"].get(c.role, []),
        }

    # -------------------------------------------------------------- config

    @app.get("/api/config")
    def get_config(c: Ctx = Depends(ctx)):
        return {"config": c.cfg, "actions": config.ACTIONS, "engines": engine_catalog(c.cfg),
                "widgets": config.WIDGETS, "workspace_actions": sorted(config.WORKSPACE_ACTIONS),
                "defaults": config.default()}

    @app.put("/api/config")
    def put_config(body: dict, c: Ctx = Depends(ctx)):
        c.require("config.edit")
        new = config.validate(body, allow_http_engines=allow_private_engines(),
                              known_urls=frozenset(e.get("url") for e in c.cfg.get("engines", [])))
        # Custom engines are managed through /api/engines, not here.
        if [e["key"] for e in new["engines"]] != [e["key"] for e in c.cfg.get("engines", [])] or \
                any(e != o for e, o in zip(new["engines"], c.cfg.get("engines", []))):
            c.require("engine.manage")
        if new["permissions"]["config.edit"].get(c.role) != "all":
            raise HTTPException(422, "you'd lose access to settings; keep config.edit on your own role")
        role_keys = {r["key"] for r in new["roles"]}
        in_use = {r["role"] for r in conn.execute(
            "SELECT DISTINCT role FROM users WHERE tenant_id=?", (c.tenant_id,)).fetchall()}
        if in_use - role_keys:
            raise HTTPException(409, f"people still hold role(s): {', '.join(sorted(in_use - role_keys))}; "
                                     "move them first")
        keys = set(config.stage_keys(new))
        live = {r["stage"] for r in conn.execute(
            "SELECT DISTINCT stage FROM deployments WHERE tenant_id=?", (c.tenant_id,)).fetchall()}
        if live - keys:
            raise HTTPException(409, f"can't remove stage(s) with live deployments: {', '.join(sorted(live - keys))}")
        removed_engines = {e["key"] for e in c.cfg.get("engines", [])} - {e["key"] for e in new["engines"]}
        with db.tx(conn):
            conn.execute("UPDATE tenants SET config_json=? WHERE id=?", (json.dumps(new), c.tenant_id))
            for k in removed_engines:
                conn.execute("DELETE FROM engine_credentials WHERE tenant_id=? AND engine_key=?",
                             (c.tenant_id, k))
            c.log("config.update", c.tenant_id, {
                "roles": sorted(role_keys), "stages": config.stage_keys(new),
                "branding": new["branding"]["product_name"],
                "engines_removed": sorted(removed_engines)})
        return {"config": new}

    # -------------------------------------------------------------- people

    @app.get("/api/people")
    def people(c: Ctx = Depends(ctx)):
        c.require("people.read")
        rows = conn.execute(
            """SELECT u.id, u.name, u.email, u.role,
                      (SELECT COUNT(*) FROM tasks t WHERE t.assignee_id=u.id AND t.status!='done') open_tasks,
                      (SELECT COUNT(*) FROM deployment_members m WHERE m.user_id=u.id) deployments
               FROM users u WHERE u.tenant_id=? ORDER BY u.name""",
            (c.tenant_id,)).fetchall()
        return [{**dict(r), "role_name": config.role_name(c.cfg, r["role"])} for r in rows]

    class PersonIn(BaseModel):
        name: str = Field(min_length=1, max_length=120)
        email: str = Field(min_length=3, max_length=200)
        role: str

    @app.post("/api/people", status_code=201)
    def add_person(body: PersonIn, c: Ctx = Depends(ctx)):
        c.require("people.manage")
        if body.role not in {r["key"] for r in c.cfg["roles"]}:
            raise HTTPException(422, f"unknown role {body.role!r}")
        uid = new_id("usr")
        tok = "fwu_" + secrets.token_urlsafe(24)
        with db.tx(conn):
            conn.execute("INSERT INTO users VALUES (?,?,?,?,?,?,?,?,?)",
                         (uid, c.tenant_id, body.name, body.email, body.role, None,
                          token_hash(tok), "{}", audit.now()))
            c.log("people.add", uid, {"name": body.name, "role": body.role})
        return {"id": uid, "token": tok, "note": "shown once; share it with them securely"}

    class PersonPatch(BaseModel):
        role: str

    @app.patch("/api/people/{user_id}")
    def change_role(user_id: str, body: PersonPatch, c: Ctx = Depends(ctx)):
        c.require("people.manage")
        u = conn.execute("SELECT * FROM users WHERE id=? AND tenant_id=?", (user_id, c.tenant_id)).fetchone()
        if not u:
            raise HTTPException(404, "person not found")
        if body.role not in {r["key"] for r in c.cfg["roles"]}:
            raise HTTPException(422, f"unknown role {body.role!r}")
        if user_id == c.uid and c.cfg["permissions"]["config.edit"].get(body.role) != "all":
            raise HTTPException(422, "that would remove your own access to settings")
        with db.tx(conn):
            conn.execute("UPDATE users SET role=? WHERE id=?", (body.role, user_id))
            c.log("people.role", user_id, {"from": u["role"], "to": body.role})
        return {"ok": True}

    # ----------------------------------------------------------- customers

    class CustomerIn(BaseModel):
        name: str = Field(min_length=1, max_length=120)
        industry: str = ""
        fields: dict = {}

    @app.get("/api/customers")
    def customers(c: Ctx = Depends(ctx)):
        if c.scope("deployment.view") != "all" and not c.can("deployment.create"):
            rows = conn.execute(
                "SELECT DISTINCT cu.* FROM customers cu JOIN deployments d ON d.customer_id=cu.id"
                " JOIN deployment_members m ON m.deployment_id=d.id"
                " WHERE cu.tenant_id=? AND m.user_id=? ORDER BY cu.name", (c.tenant_id, c.uid)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM customers WHERE tenant_id=? ORDER BY name",
                                (c.tenant_id,)).fetchall()
        return [{**dict(r), "fields": jloads(r["fields_json"])} for r in rows]

    @app.post("/api/customers", status_code=201)
    def add_customer(body: CustomerIn, c: Ctx = Depends(ctx)):
        c.require("deployment.create")
        fields = config.check_fields(c.cfg, "customer", body.fields)
        cid = new_id("cus")
        with db.tx(conn):
            conn.execute("INSERT INTO customers VALUES (?,?,?,?,?,?)",
                         (cid, c.tenant_id, body.name, body.industry, json.dumps(fields), audit.now()))
            c.log("customer.create", cid, {"name": body.name})
        return {"id": cid}

    # --------------------------------------------------------- deployments

    def dep_out(r: sqlite3.Row) -> dict:
        members = conn.execute(
            "SELECT u.id, u.name, u.role FROM deployment_members m JOIN users u ON u.id=m.user_id"
            " WHERE m.deployment_id=? ORDER BY u.name", (r["id"],)).fetchall()
        cust = conn.execute("SELECT name FROM customers WHERE id=?", (r["customer_id"],)).fetchone()
        counts = conn.execute(
            "SELECT status, COUNT(*) n FROM tasks WHERE deployment_id=? GROUP BY status",
            (r["id"],)).fetchall()
        return {
            "id": r["id"], "name": r["name"], "customer_id": r["customer_id"],
            "customer": cust["name"] if cust else "", "stage": r["stage"], "health": r["health"],
            "lead_id": r["lead_id"], "fields": jloads(r["fields_json"]),
            "staffing_req": r["staffing_req"], "members": [dict(m) for m in members],
            "tasks": {x["status"]: x["n"] for x in counts},
            "updated_at": r["updated_at"],
        }

    def visible_deployments(c: Ctx) -> list[sqlite3.Row]:
        s = c.scope("deployment.view")
        if s == "all":
            return conn.execute("SELECT * FROM deployments WHERE tenant_id=? ORDER BY updated_at DESC",
                                (c.tenant_id,)).fetchall()
        if s == "own":
            return conn.execute(
                "SELECT d.* FROM deployments d JOIN deployment_members m ON m.deployment_id=d.id"
                " WHERE d.tenant_id=? AND m.user_id=? ORDER BY d.updated_at DESC",
                (c.tenant_id, c.uid)).fetchall()
        return []

    @app.get("/api/deployments")
    def deployments(c: Ctx = Depends(ctx)):
        return [dep_out(r) for r in visible_deployments(c)]

    class DeploymentIn(BaseModel):
        customer_id: str
        name: str = Field(min_length=1, max_length=120)
        lead_id: str | None = None
        fields: dict = {}
        staffing_req: str = ""

    def tenant_user(c: Ctx, uid: str) -> sqlite3.Row:
        u = conn.execute("SELECT * FROM users WHERE id=? AND tenant_id=?", (uid, c.tenant_id)).fetchone()
        if not u:
            raise HTTPException(422, f"no such person in this workspace: {uid}")
        return u

    @app.post("/api/deployments", status_code=201)
    def add_deployment(body: DeploymentIn, c: Ctx = Depends(ctx)):
        c.require("deployment.create")
        if not conn.execute("SELECT 1 FROM customers WHERE id=? AND tenant_id=?",
                            (body.customer_id, c.tenant_id)).fetchone():
            raise HTTPException(422, "unknown customer")
        if body.lead_id:
            tenant_user(c, body.lead_id)
        fields = config.check_fields(c.cfg, "deployment", body.fields)
        did = new_id("dep")
        first = c.cfg["stages"][0]["key"]
        ts = audit.now()
        with db.tx(conn):
            conn.execute("INSERT INTO deployments VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                         (did, c.tenant_id, body.customer_id, body.name, first, "on_track",
                          body.lead_id, json.dumps(fields), body.staffing_req, ts, ts))
            # The creator is staffed on it too, so "own"-scoped creators can run it.
            for uid in {body.lead_id, c.uid} - {None}:
                conn.execute("INSERT OR IGNORE INTO deployment_members VALUES (?,?)", (did, uid))
            conn.execute("INSERT INTO stage_events (tenant_id, deployment_id, from_stage, to_stage,"
                         " actor_id, note, at) VALUES (?,?,?,?,?,?,?)",
                         (c.tenant_id, did, None, first, c.uid, "opened", ts))
            c.log("deployment.create", did, {"name": body.name, "stage": first})
        return {"id": did}

    @app.get("/api/deployments/{dep_id}")
    def deployment(dep_id: str, c: Ctx = Depends(ctx)):
        r = c.deployment(dep_id)
        out = dep_out(r)
        out["history"] = [dict(e) for e in conn.execute(
            "SELECT e.from_stage, e.to_stage, e.note, e.at, u.name actor FROM stage_events e"
            " JOIN users u ON u.id=e.actor_id WHERE e.deployment_id=? ORDER BY e.id",
            (dep_id,)).fetchall()]
        out["you_can"] = sorted(a for a in config.ACTIONS
                                if a not in config.WORKSPACE_ACTIONS and c.can_on(a, dep_id))
        return out

    class AdvanceIn(BaseModel):
        to_stage: str
        note: str = Field(default="", max_length=500)

    @app.post("/api/deployments/{dep_id}/advance")
    def advance(dep_id: str, body: AdvanceIn, c: Ctx = Depends(ctx)):
        r = c.deployment(dep_id)
        c.require_on("deployment.advance", dep_id)
        keys = config.stage_keys(c.cfg)
        if body.to_stage not in keys:
            raise HTTPException(422, f"unknown stage {body.to_stage!r}")
        if body.to_stage == r["stage"]:
            raise HTTPException(409, "already in that stage")
        step = keys.index(body.to_stage) - keys.index(r["stage"])
        if step != 1:
            c.require_on("deployment.jump", dep_id)
        if step < 0 and not body.note.strip():
            raise HTTPException(422, "moving a deployment backward needs a note")
        ts = audit.now()
        with db.tx(conn):
            conn.execute("UPDATE deployments SET stage=?, updated_at=? WHERE id=?",
                         (body.to_stage, ts, dep_id))
            conn.execute("INSERT INTO stage_events (tenant_id, deployment_id, from_stage, to_stage,"
                         " actor_id, note, at) VALUES (?,?,?,?,?,?,?)",
                         (c.tenant_id, dep_id, r["stage"], body.to_stage, c.uid, body.note, ts))
            c.log("deployment.advance", dep_id,
                  {"from": r["stage"], "to": body.to_stage, "note": body.note})
        return {"stage": body.to_stage}

    class PatchDeployment(BaseModel):
        health: str | None = None
        fields: dict | None = None
        staffing_req: str | None = None

    @app.patch("/api/deployments/{dep_id}")
    def patch_deployment(dep_id: str, body: PatchDeployment, c: Ctx = Depends(ctx)):
        r = c.deployment(dep_id)
        changes: dict = {}
        if body.health is not None or body.fields is not None:
            c.require_on("deployment.edit", dep_id)
        if body.health is not None:
            if body.health not in ("on_track", "at_risk", "blocked"):
                raise HTTPException(422, "health must be on_track, at_risk or blocked")
            changes["health"] = body.health
        if body.fields is not None:
            merged = {**jloads(r["fields_json"]), **body.fields}
            changes["fields_json"] = json.dumps(config.check_fields(c.cfg, "deployment", merged))
        if body.staffing_req is not None:
            c.require_on("deployment.staff", dep_id)
            changes["staffing_req"] = body.staffing_req[:8000]
        if not changes:
            return {"changed": []}
        changes["updated_at"] = audit.now()
        detail = {k: v for k, v in changes.items() if k not in ("updated_at", "staffing_req")}
        if "staffing_req" in changes:
            detail["staffing_req"] = "edited"
        with db.tx(conn):
            conn.execute(f"UPDATE deployments SET {', '.join(k + '=?' for k in changes)} WHERE id=?",
                         (*changes.values(), dep_id))
            c.log("deployment.update", dep_id, detail)
        return {"changed": [k for k in changes if k != "updated_at"]}

    class MemberIn(BaseModel):
        user_id: str

    @app.post("/api/deployments/{dep_id}/members")
    def add_member(dep_id: str, body: MemberIn, c: Ctx = Depends(ctx)):
        c.deployment(dep_id)
        c.require_on("deployment.staff", dep_id)
        tenant_user(c, body.user_id)
        with db.tx(conn):
            conn.execute("INSERT OR IGNORE INTO deployment_members VALUES (?,?)", (dep_id, body.user_id))
            c.log("deployment.staff", dep_id, {"added": body.user_id})
        return {"ok": True}

    @app.delete("/api/deployments/{dep_id}/members/{user_id}")
    def remove_member(dep_id: str, user_id: str, c: Ctx = Depends(ctx)):
        c.deployment(dep_id)
        c.require_on("deployment.staff", dep_id)
        with db.tx(conn):
            conn.execute("DELETE FROM deployment_members WHERE deployment_id=? AND user_id=?",
                         (dep_id, user_id))
            c.log("deployment.staff", dep_id, {"removed": user_id})
        return {"ok": True}

    # --------------------------------------------------------------- tasks

    def task_out(r: sqlite3.Row) -> dict:
        d = dict(r)
        who = conn.execute("SELECT name FROM users WHERE id=?", (r["assignee_id"],)).fetchone() \
            if r["assignee_id"] else None
        dep = conn.execute("SELECT name FROM deployments WHERE id=?", (r["deployment_id"],)).fetchone()
        d["assignee"] = who["name"] if who else None
        d["deployment"] = dep["name"] if dep else ""
        return d

    @app.get("/api/tasks")
    def tasks(mine: bool = False, deployment_id: str | None = None, c: Ctx = Depends(ctx)):
        visible = [r["id"] for r in visible_deployments(c)]
        if deployment_id:
            c.deployment(deployment_id)
            visible = [deployment_id]
        if not visible:
            return []
        q = f"SELECT * FROM tasks WHERE tenant_id=? AND deployment_id IN ({','.join('?' * len(visible))})"
        args: list = [c.tenant_id, *visible]
        if mine:
            q += " AND assignee_id=?"
            args.append(c.uid)
        q += (" ORDER BY CASE status WHEN 'blocked' THEN 0 WHEN 'in_progress' THEN 1"
              " WHEN 'open' THEN 2 ELSE 3 END, due")
        return [task_out(r) for r in conn.execute(q, args).fetchall()]

    class TaskIn(BaseModel):
        deployment_id: str
        title: str = Field(min_length=1, max_length=200)
        stage: str | None = None
        assignee_id: str | None = None
        due: str | None = None

    @app.post("/api/tasks", status_code=201)
    def add_task(body: TaskIn, c: Ctx = Depends(ctx)):
        dep = c.deployment(body.deployment_id)
        c.require_on("task.create", body.deployment_id)
        assignee = body.assignee_id or c.uid
        if assignee != c.uid:
            c.require_on("task.assign", body.deployment_id)
        tenant_user(c, assignee)
        stage = body.stage or dep["stage"]
        if stage not in config.stage_keys(c.cfg):
            raise HTTPException(422, f"unknown stage {stage!r}")
        tid = new_id("tsk")
        ts = audit.now()
        with db.tx(conn):
            conn.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                         (tid, c.tenant_id, body.deployment_id, stage, body.title, assignee,
                          "open", body.due, c.uid, ts, ts))
            c.log("task.create", tid, {"deployment": body.deployment_id, "assignee": assignee,
                                       "title": body.title})
        return {"id": tid}

    class TaskPatch(BaseModel):
        status: str | None = None
        assignee_id: str | None = None
        due: str | None = None

    @app.patch("/api/tasks/{task_id}")
    def patch_task(task_id: str, body: TaskPatch, c: Ctx = Depends(ctx)):
        t = conn.execute("SELECT * FROM tasks WHERE id=? AND tenant_id=?",
                         (task_id, c.tenant_id)).fetchone()
        if not t:
            raise HTTPException(404, "task not found")
        c.deployment(t["deployment_id"])
        if t["assignee_id"] != c.uid:
            c.require_on("task.update_any", t["deployment_id"])
        changes: dict = {}
        if body.status is not None:
            if body.status not in ("open", "in_progress", "blocked", "done"):
                raise HTTPException(422, "bad status")
            changes["status"] = body.status
        if body.assignee_id is not None:
            c.require_on("task.assign", t["deployment_id"])
            tenant_user(c, body.assignee_id)
            changes["assignee_id"] = body.assignee_id
        if body.due is not None:
            changes["due"] = body.due or None
        if not changes:
            return {"changed": []}
        changes["updated_at"] = audit.now()
        with db.tx(conn):
            conn.execute(f"UPDATE tasks SET {', '.join(k + '=?' for k in changes)} WHERE id=?",
                         (*changes.values(), task_id))
            c.log("task.update", task_id, {k: v for k, v in changes.items() if k != "updated_at"})
        return {"changed": [k for k in changes if k != "updated_at"]}

    # ------------------------------------------------------------- engines

    @app.get("/api/engines")
    def engine_list(c: Ctx = Depends(ctx)):
        cat = engine_catalog(c.cfg)
        creds = {r["engine_key"]: r for r in conn.execute(
            "SELECT engine_key, secret, token_hash FROM engine_credentials WHERE tenant_id=?",
            (c.tenant_id,)).fetchall()}
        for k, e in cat.items():
            if not e["builtin"]:
                e["credential_set"] = k in creds
        return cat

    class EngineIn(BaseModel):
        key: str
        kind: str
        name: str
        does: str = ""
        input_hint: str = ""
        url: str | None = None

    @app.post("/api/engines", status_code=201)
    def register_engine(body: EngineIn, c: Ctx = Depends(ctx)):
        """Register a team's own script or service. Returns its credential once."""
        c.require("engine.manage")
        cfg = json.loads(json.dumps(c.cfg))
        cfg["engines"] = [*cfg.get("engines", []), body.model_dump(exclude_none=True)]
        new = config.validate(cfg, allow_http_engines=allow_private_engines(),
                              known_urls=frozenset(e.get("url") for e in c.cfg.get("engines", [])))
        cred = issue_credential(c, new, body.key, register=True)
        with db.tx(conn):
            conn.execute("UPDATE tenants SET config_json=? WHERE id=?", (json.dumps(new), c.tenant_id))
            store_credential(c, body.key, cred)
            c.log("engine.register", body.key, {"kind": body.kind, "name": body.name,
                                                "url": body.url or None})
        return {"engine": custom_engine(new, body.key), **cred["public"]}

    def issue_credential(c: Ctx, cfg: dict, key: str, register: bool = False) -> dict:
        e = custom_engine(cfg, key)
        if not e:
            raise HTTPException(404, "no such custom engine")
        if e["kind"] == "webhook":
            s = "fws_" + secrets.token_urlsafe(32)
            return {"secret": s, "token_hash": None,
                    "public": {"signing_secret": s,
                               "note": "shown once; your service verifies X-Fieldwork-Signature with it"}}
        t = "fwe_" + secrets.token_urlsafe(32)
        return {"secret": None, "token_hash": token_hash(t),
                "public": {"engine_token": t,
                           "note": "shown once; your script posts to /api/ingest/findings with it"}}

    def store_credential(c: Ctx, key: str, cred: dict) -> None:
        conn.execute("INSERT OR REPLACE INTO engine_credentials VALUES (?,?,?,?,?)",
                     (c.tenant_id, key, cred["secret"], cred["token_hash"], audit.now()))

    @app.post("/api/engines/{key}/rotate")
    def rotate_engine(key: str, c: Ctx = Depends(ctx)):
        c.require("engine.manage")
        cred = issue_credential(c, c.cfg, key)
        with db.tx(conn):
            store_credential(c, key, cred)
            c.log("engine.rotate", key, {})
        return cred["public"]

    def engine_gate(c: Ctx, dep_id: str) -> sqlite3.Row:
        dep = c.deployment(dep_id)
        c.require_on("engine.run", dep_id)
        return dep

    def save_finding(c: Ctx, dep_id: str, engine: str, title: str, result: dict,
                     actor: str | None = None) -> str:
        fid = new_id("fnd")
        digest = "sha256:" + hashlib.sha256(audit.canonical(result).encode()).hexdigest()
        with db.tx(conn):
            conn.execute("INSERT INTO findings VALUES (?,?,?,?,?,?,?,?,?,?)",
                         (fid, c.tenant_id, dep_id, engine, title, json.dumps(result),
                          None, None, actor or c.uid, audit.now()))
            c.log("engine.run", fid, {"engine": engine, "deployment": dep_id,
                                      "title": title, "result_digest": digest}, actor=actor)
        return fid

    class SenderoIn(BaseModel):
        title: str = Field(min_length=1, max_length=200)
        csv: str = Field(min_length=1, max_length=2_000_000)
        metric: str
        baseline: float | None = None
        higher_is_worse: bool = True
        capability: list[str] | None = None
        config: list[str] | None = None

    @app.post("/api/deployments/{dep_id}/engines/sendero")
    def run_sendero(dep_id: str, body: SenderoIn, c: Ctx = Depends(ctx)):
        engine_gate(c, dep_id)
        rows = engines.parse_csv(body.csv)
        result = engines.run_sendero(rows, body.metric, body.baseline, body.higher_is_worse,
                                     body.capability, body.config)
        fid = save_finding(c, dep_id, "sendero", body.title, result)
        return {"finding_id": fid, "result": result}

    @app.post("/api/deployments/{dep_id}/engines/threshold")
    def run_threshold(dep_id: str, c: Ctx = Depends(ctx)):
        dep = c.deployment(dep_id)
        c.require_on("bench.match", dep_id)
        rows = conn.execute("SELECT id, name, profile_json FROM users WHERE tenant_id=?",
                            (c.tenant_id,)).fetchall()
        pool = [{"id": p["id"], "name": p["name"], "profile": jloads(p["profile_json"])}
                for p in rows if jloads(p["profile_json"]).get("evidence")]
        if not pool:
            raise HTTPException(422, "nobody in the workspace has a bench profile yet")
        result = engines.run_threshold(dep["staffing_req"], dep["name"], pool)
        fid = save_finding(c, dep_id, "threshold", f"Bench match · {dep['name']}", result)
        return {"finding_id": fid, "result": result}

    class CustomRunIn(BaseModel):
        title: str = Field(default="", max_length=200)
        input: str = Field(default="", max_length=200_000)

    @app.post("/api/deployments/{dep_id}/engines/custom/{key}")
    def run_custom(dep_id: str, key: str, body: CustomRunIn, c: Ctx = Depends(ctx)):
        dep = engine_gate(c, dep_id)
        e = custom_engine(c.cfg, key)
        if not e:
            raise HTTPException(404, "no such engine")
        if e["kind"] != "webhook":
            raise HTTPException(409, "this engine pushes its results in; it can't be run from here")
        cred = conn.execute("SELECT secret FROM engine_credentials WHERE tenant_id=? AND engine_key=?",
                            (c.tenant_id, key)).fetchone()
        if not cred or not cred["secret"]:
            raise HTTPException(409, "engine has no signing secret; rotate its credential")
        run_id = new_id("run")
        payload = {"engine": key, "run_id": run_id, "input": body.input,
                   "requested_by": {"id": c.uid, "name": c.user["name"], "role": c.role},
                   "deployment": {k: v for k, v in dep_out(dep).items() if k != "staffing_req"},
                   "workspace": c.tenant_id}
        result = plugins.call_webhook(e["url"], cred["secret"], payload, allow_private_engines())
        title = body.title or f"{e['name']} · {dep['name']}"
        fid = save_finding(c, dep_id, key, title, result)
        return {"finding_id": fid, "result": result}

    class IngestIn(BaseModel):
        deployment_id: str
        title: str = Field(min_length=1, max_length=200)
        summary: str
        status: str = "info"
        result: dict | list = {}

    @app.post("/api/ingest/findings", status_code=201)
    def ingest(body: IngestIn, authorization: str = Header(default="")):
        """For push engines: a team's script posts its results in with its engine token."""
        tok = authorization.split(" ", 1)[1].strip() if authorization.lower().startswith("bearer ") else ""
        cred = conn.execute("SELECT * FROM engine_credentials WHERE token_hash=?",
                            (token_hash(tok),)).fetchone() if tok.startswith("fwe_") else None
        if not cred:
            raise HTTPException(401, "invalid engine token")
        tenant = conn.execute("SELECT * FROM tenants WHERE id=?", (cred["tenant_id"],)).fetchone()
        cfg = json.loads(tenant["config_json"])
        e = custom_engine(cfg, cred["engine_key"])
        if not e or e["kind"] != "push":
            raise HTTPException(401, "engine no longer registered")
        if not conn.execute("SELECT 1 FROM deployments WHERE id=? AND tenant_id=?",
                            (body.deployment_id, cred["tenant_id"])).fetchone():
            raise HTTPException(404, "deployment not found")
        result = plugins.normalize_result({"summary": body.summary, "status": body.status,
                                           "result": body.result})

        class _EngineCtx(Ctx):
            def __init__(self):
                self.conn, self.tenant_id, self.cfg = conn, cred["tenant_id"], cfg
                self.user = {"id": "engine:" + e["key"], "role": None, "name": e["name"]}

        fid = save_finding(_EngineCtx(), body.deployment_id, e["key"], body.title, result,
                           actor="engine:" + e["key"])
        return {"finding_id": fid}

    @app.get("/api/deployments/{dep_id}/findings")
    def findings(dep_id: str, c: Ctx = Depends(ctx)):
        c.deployment(dep_id)
        rows = conn.execute(
            "SELECT f.*, COALESCE(u.name, f.created_by) created_by_name, v.name confirmed_by_name"
            " FROM findings f LEFT JOIN users u ON u.id=f.created_by LEFT JOIN users v ON v.id=f.confirmed_by"
            " WHERE f.deployment_id=? AND f.tenant_id=? ORDER BY f.created_at DESC",
            (dep_id, c.tenant_id)).fetchall()
        return [{**{k: r[k] for k in r.keys() if k != "result_json"},
                 "result": jloads(r["result_json"])} for r in rows]

    @app.post("/api/findings/{fid}/confirm")
    def confirm(fid: str, c: Ctx = Depends(ctx)):
        f = conn.execute("SELECT * FROM findings WHERE id=? AND tenant_id=?",
                         (fid, c.tenant_id)).fetchone()
        if not f:
            raise HTTPException(404, "finding not found")
        c.deployment(f["deployment_id"])
        c.require_on("finding.confirm", f["deployment_id"])
        if f["confirmed_by"]:
            raise HTTPException(409, "already confirmed")
        if f["created_by"] == c.uid:
            raise HTTPException(403, "a finding is confirmed by someone other than the person who ran it")
        with db.tx(conn):
            conn.execute("UPDATE findings SET confirmed_by=?, confirmed_at=? WHERE id=?",
                         (c.uid, audit.now(), fid))
            c.log("finding.confirm", fid, {"engine": f["engine"], "deployment": f["deployment_id"]})
        return {"ok": True}

    # ----------------------------------------------------------- dashboard

    @app.get("/api/dashboard")
    def dashboard(c: Ctx = Depends(ctx)):
        widgets = c.cfg["views"].get(c.role, [])
        deps = [dep_out(r) for r in visible_deployments(c)]
        out: dict = {"widgets": widgets}
        if "kpis" in widgets:
            out["kpis"] = {"deployments": len(deps),
                           **{h: sum(1 for d in deps if d["health"] == h)
                              for h in ("on_track", "at_risk", "blocked")}}
        if "chain" in widgets:
            out["chain"] = deps
        if "my_tasks" in widgets:
            out["my_tasks"] = [t for t in tasks(mine=True, deployment_id=None, c=c) if t["status"] != "done"]
        if "team" in widgets and c.can("people.read"):
            out["team"] = [p for p in people(c) if p["deployments"] or p["open_tasks"]]
        if "findings" in widgets:
            ids = [d["id"] for d in deps]
            rows = conn.execute(
                f"SELECT f.id, f.title, f.engine, f.deployment_id, f.created_at, d.name deployment"
                f" FROM findings f JOIN deployments d ON d.id=f.deployment_id"
                f" WHERE f.tenant_id=? AND f.confirmed_by IS NULL AND f.deployment_id IN"
                f" ({','.join('?' * len(ids)) or 'NULL'}) ORDER BY f.created_at DESC LIMIT 20",
                (c.tenant_id, *ids)).fetchall() if ids else []
            out["findings"] = [dict(r) for r in rows]
        return out

    # --------------------------------------------------------------- audit

    @app.get("/api/audit")
    def audit_log(limit: int = 100, c: Ctx = Depends(ctx)):
        c.require("audit.read")
        rows = conn.execute(
            "SELECT a.seq, a.at, a.action, a.subject, a.detail_json, a.hash, a.prev_hash,"
            " COALESCE(u.name, a.actor_id) actor"
            " FROM audit a LEFT JOIN users u ON u.id=a.actor_id WHERE a.tenant_id=?"
            " ORDER BY a.seq DESC LIMIT ?", (c.tenant_id, max(1, min(limit, 500)))).fetchall()
        return [{**{k: r[k] for k in r.keys() if k != "detail_json"},
                 "detail": jloads(r["detail_json"])} for r in rows]

    @app.get("/api/audit/verify")
    def audit_verify(c: Ctx = Depends(ctx)):
        c.require("audit.verify")
        return audit.verify(conn, c.tenant_id)

    return app
