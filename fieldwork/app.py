"""Fieldwork API.

Every request is authenticated by bearer token, resolved to one user in one
tenant. Every query is filtered by that tenant. Every write is recorded in the
tenant's hash-chained audit trail inside the same transaction.

Role checks read the tenant's own permissions map (config.py), so what an FDE
or manager may do is the customer's decision, not ours.
"""


import hashlib
import json
import secrets
import sqlite3
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from . import audit, config, db, engines

FRONTEND = Path(__file__).resolve().parent.parent / "frontend"


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(6)}"


def jloads(s: str | None) -> Any:
    return json.loads(s) if s else {}


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

    def can(self, action: str) -> bool:
        return self.role in self.cfg["permissions"].get(action, [])

    def require(self, action: str) -> None:
        if not self.can(action):
            raise HTTPException(403, f"your role ({self.role}) can't {config.ACTIONS[action].lower()}")

    def is_member(self, dep_id: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM deployment_members WHERE deployment_id=? AND user_id=?",
            (dep_id, self.uid)).fetchone() is not None

    def deployment(self, dep_id: str) -> sqlite3.Row:
        """Fetch a deployment this user may see, or 404 (never 403: don't leak existence)."""
        row = self.conn.execute("SELECT * FROM deployments WHERE id=? AND tenant_id=?",
                                (dep_id, self.tenant_id)).fetchone()
        if not row or not (self.can("deployment.read_all") or self.is_member(dep_id)):
            raise HTTPException(404, "deployment not found")
        return row

    def log(self, action: str, subject: str, detail: dict | None = None) -> None:
        audit.record(self.conn, self.tenant_id, self.uid, action, subject, detail)


def create_app(db_file: str | None = None) -> FastAPI:
    conn = db.connect(db_file)
    db.init(conn)
    app = FastAPI(title="Fieldwork", version="0.1.0",
                  description="Operating platform for forward-deployed engineering teams")
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
        import os
        if os.environ.get("FIELDWORK_DEMO") != "1":
            raise HTTPException(404, "not found")
        from .seed import DEMO_TOKENS
        return [
            {"label": "Dana Whitfield · Director", "token": DEMO_TOKENS["director"]},
            {"label": "Marcus Hale · FDE Manager", "token": DEMO_TOKENS["manager"]},
            {"label": "Maya Chen · FDE", "token": DEMO_TOKENS["fde"]},
            {"label": "Jordan Reyes · FDE", "token": DEMO_TOKENS["fde2"]},
        ]

    @app.get("/api/demo/sample-csv")
    def demo_csv():
        import os
        if os.environ.get("FIELDWORK_DEMO") != "1":
            raise HTTPException(404, "not found")
        from .seed import northfield_adoption_csv
        return {"csv": northfield_adoption_csv(), "metric": "minutes_per_exception", "baseline": 6}

    @app.get("/api/me")
    def me(c: Ctx = Depends(ctx)):
        return {
            "user": {"id": c.uid, "name": c.user["name"], "email": c.user["email"], "role": c.role,
                     "role_label": c.cfg["labels"][c.role]},
            "tenant": {"id": c.tenant_id, "name": c.tenant_name},
            "can": sorted(a for a in config.ACTIONS if c.can(a)),
        }

    # -------------------------------------------------------------- config

    @app.get("/api/config")
    def get_config(c: Ctx = Depends(ctx)):
        return {"config": c.cfg, "actions": config.ACTIONS, "engines": engines.REGISTRY,
                "roles": list(config.ROLES), "locked": config.LOCKED}

    @app.put("/api/config")
    def put_config(body: dict, c: Ctx = Depends(ctx)):
        c.require("config.edit")
        new = config.validate(body)
        # A stage can't be deleted while a deployment is sitting in it.
        keys = set(config.stage_keys(new))
        stuck = conn.execute(
            "SELECT stage, COUNT(*) n FROM deployments WHERE tenant_id=? GROUP BY stage",
            (c.tenant_id,)).fetchall()
        orphaned = [r["stage"] for r in stuck if r["stage"] not in keys]
        if orphaned:
            raise HTTPException(409, f"can't remove stage(s) with live deployments: {', '.join(orphaned)}")
        with db.tx(conn):
            conn.execute("UPDATE tenants SET config_json=? WHERE id=?",
                         (json.dumps(new), c.tenant_id))
            c.log("config.update", c.tenant_id, {"stages": config.stage_keys(new)})
        return {"config": new}

    # -------------------------------------------------------------- people

    @app.get("/api/people")
    def people(c: Ctx = Depends(ctx)):
        c.require("people.read")
        rows = conn.execute(
            """SELECT u.id, u.name, u.email, u.role, u.manager_id,
                      (SELECT COUNT(*) FROM tasks t WHERE t.assignee_id=u.id AND t.status!='done') open_tasks,
                      (SELECT COUNT(*) FROM deployment_members m JOIN deployments d ON d.id=m.deployment_id
                        WHERE m.user_id=u.id) deployments
               FROM users u WHERE u.tenant_id=? ORDER BY u.role DESC, u.name""",
            (c.tenant_id,)).fetchall()
        return [dict(r) for r in rows]

    # ----------------------------------------------------------- customers

    class CustomerIn(BaseModel):
        name: str = Field(min_length=1, max_length=120)
        industry: str = ""
        fields: dict = {}

    @app.get("/api/customers")
    def customers(c: Ctx = Depends(ctx)):
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

    @app.get("/api/deployments")
    def deployments(c: Ctx = Depends(ctx)):
        if c.can("deployment.read_all"):
            rows = conn.execute("SELECT * FROM deployments WHERE tenant_id=? ORDER BY updated_at DESC",
                                (c.tenant_id,)).fetchall()
        else:
            rows = conn.execute(
                "SELECT d.* FROM deployments d JOIN deployment_members m ON m.deployment_id=d.id"
                " WHERE d.tenant_id=? AND m.user_id=? ORDER BY d.updated_at DESC",
                (c.tenant_id, c.uid)).fetchall()
        return [dep_out(r) for r in rows]

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
            conn.execute(
                "INSERT INTO deployments VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (did, c.tenant_id, body.customer_id, body.name, first, "on_track",
                 body.lead_id, json.dumps(fields), body.staffing_req, ts, ts))
            if body.lead_id:
                conn.execute("INSERT INTO deployment_members VALUES (?,?)", (did, body.lead_id))
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
        return out

    class AdvanceIn(BaseModel):
        to_stage: str
        note: str = Field(default="", max_length=500)

    @app.post("/api/deployments/{dep_id}/advance")
    def advance(dep_id: str, body: AdvanceIn, c: Ctx = Depends(ctx)):
        c.require("deployment.advance")
        r = c.deployment(dep_id)
        keys = config.stage_keys(c.cfg)
        if body.to_stage not in keys:
            raise HTTPException(422, f"unknown stage {body.to_stage!r}")
        if body.to_stage == r["stage"]:
            raise HTTPException(409, "already in that stage")
        # FDEs move one step at a time; managers and directors may jump or roll back.
        if c.role == "fde" and keys.index(body.to_stage) - keys.index(r["stage"]) != 1:
            raise HTTPException(403, "FDEs advance one stage at a time")
        if keys.index(body.to_stage) < keys.index(r["stage"]) and not body.note.strip():
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
        if not (c.can("deployment.create") or c.is_member(dep_id)):
            raise HTTPException(403, "only the deployment team or managers can edit it")
        changes: dict = {}
        if body.health is not None:
            if body.health not in ("on_track", "at_risk", "blocked"):
                raise HTTPException(422, "health must be on_track, at_risk or blocked")
            changes["health"] = body.health
        if body.fields is not None:
            merged = {**jloads(r["fields_json"]), **body.fields}
            changes["fields_json"] = json.dumps(config.check_fields(c.cfg, "deployment", merged))
        if body.staffing_req is not None:
            c.require("deployment.staff")
            changes["staffing_req"] = body.staffing_req[:8000]
        if not changes:
            return {"changed": []}
        changes["updated_at"] = audit.now()
        with db.tx(conn):
            conn.execute(f"UPDATE deployments SET {', '.join(k + '=?' for k in changes)} WHERE id=?",
                         (*changes.values(), dep_id))
            c.log("deployment.update", dep_id,
                  {k: v for k, v in changes.items() if k not in ("updated_at", "staffing_req")}
                  | ({"staffing_req": "edited"} if "staffing_req" in changes else {}))
        return {"changed": [k for k in changes if k != "updated_at"]}

    class MemberIn(BaseModel):
        user_id: str

    @app.post("/api/deployments/{dep_id}/members")
    def add_member(dep_id: str, body: MemberIn, c: Ctx = Depends(ctx)):
        c.require("deployment.staff")
        c.deployment(dep_id)
        tenant_user(c, body.user_id)
        with db.tx(conn):
            conn.execute("INSERT OR IGNORE INTO deployment_members VALUES (?,?)", (dep_id, body.user_id))
            c.log("deployment.staff", dep_id, {"added": body.user_id})
        return {"ok": True}

    @app.delete("/api/deployments/{dep_id}/members/{user_id}")
    def remove_member(dep_id: str, user_id: str, c: Ctx = Depends(ctx)):
        c.require("deployment.staff")
        c.deployment(dep_id)
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
        q = "SELECT * FROM tasks WHERE tenant_id=?"
        args: list = [c.tenant_id]
        if deployment_id:
            c.deployment(deployment_id)
            q += " AND deployment_id=?"; args.append(deployment_id)
        if mine or not c.can("deployment.read_all"):
            if mine:
                q += " AND assignee_id=?"; args.append(c.uid)
            else:
                q += (" AND (assignee_id=? OR deployment_id IN "
                      "(SELECT deployment_id FROM deployment_members WHERE user_id=?))")
                args += [c.uid, c.uid]
        q += " ORDER BY CASE status WHEN 'blocked' THEN 0 WHEN 'in_progress' THEN 1 WHEN 'open' THEN 2 ELSE 3 END, due"
        return [task_out(r) for r in conn.execute(q, args).fetchall()]

    class TaskIn(BaseModel):
        deployment_id: str
        title: str = Field(min_length=1, max_length=200)
        stage: str | None = None
        assignee_id: str | None = None
        due: str | None = None

    @app.post("/api/tasks", status_code=201)
    def add_task(body: TaskIn, c: Ctx = Depends(ctx)):
        c.require("task.create")
        dep = c.deployment(body.deployment_id)
        if c.role == "fde" and not c.is_member(body.deployment_id):
            raise HTTPException(403, "FDEs create tasks on their own deployments")
        assignee = body.assignee_id or c.uid
        if assignee != c.uid:
            c.require("task.assign")
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
        if t["assignee_id"] != c.uid and not c.can("task.assign"):
            raise HTTPException(403, "you can only update your own tasks")
        changes: dict = {}
        if body.status is not None:
            if body.status not in ("open", "in_progress", "blocked", "done"):
                raise HTTPException(422, "bad status")
            changes["status"] = body.status
        if body.assignee_id is not None:
            c.require("task.assign")
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
        return engines.REGISTRY

    def engine_gate(c: Ctx, dep_id: str) -> sqlite3.Row:
        c.require("engine.run")
        dep = c.deployment(dep_id)
        if c.role == "fde" and not c.is_member(dep_id):
            raise HTTPException(403, "FDEs run engines on their own deployments")
        return dep

    def save_finding(c: Ctx, dep_id: str, engine: str, title: str, result: dict) -> str:
        fid = new_id("fnd")
        digest = "sha256:" + hashlib.sha256(audit.canonical(result).encode()).hexdigest()
        with db.tx(conn):
            conn.execute("INSERT INTO findings VALUES (?,?,?,?,?,?,?,?,?,?)",
                         (fid, c.tenant_id, dep_id, engine, title, json.dumps(result),
                          None, None, c.uid, audit.now()))
            c.log("engine.run", fid, {"engine": engine, "deployment": dep_id,
                                      "title": title, "result_digest": digest})
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
        c.require("bench.match")
        dep = engine_gate(c, dep_id)
        people_rows = conn.execute(
            "SELECT id, name, profile_json FROM users WHERE tenant_id=? AND role='fde'",
            (c.tenant_id,)).fetchall()
        people = [{"id": p["id"], "name": p["name"], "profile": jloads(p["profile_json"])}
                  for p in people_rows]
        result = engines.run_threshold(dep["staffing_req"], dep["name"], people)
        fid = save_finding(c, dep_id, "threshold", f"Bench match · {dep['name']}", result)
        return {"finding_id": fid, "result": result}

    @app.get("/api/deployments/{dep_id}/findings")
    def findings(dep_id: str, c: Ctx = Depends(ctx)):
        c.deployment(dep_id)
        rows = conn.execute(
            "SELECT f.*, u.name created_by_name, v.name confirmed_by_name FROM findings f"
            " JOIN users u ON u.id=f.created_by LEFT JOIN users v ON v.id=f.confirmed_by"
            " WHERE f.deployment_id=? AND f.tenant_id=? ORDER BY f.created_at DESC",
            (dep_id, c.tenant_id)).fetchall()
        return [{**{k: r[k] for k in r.keys() if k != "result_json"},
                 "result": jloads(r["result_json"])} for r in rows]

    @app.post("/api/findings/{fid}/confirm")
    def confirm(fid: str, c: Ctx = Depends(ctx)):
        c.require("finding.confirm")
        f = conn.execute("SELECT * FROM findings WHERE id=? AND tenant_id=?",
                         (fid, c.tenant_id)).fetchone()
        if not f:
            raise HTTPException(404, "finding not found")
        c.deployment(f["deployment_id"])
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
        deps = deployments(c)
        stage_names = {s["key"]: s["name"] for s in c.cfg["stages"]}
        by_stage = {s["key"]: 0 for s in c.cfg["stages"]}
        for d in deps:
            by_stage[d["stage"]] = by_stage.get(d["stage"], 0) + 1
        my_tasks = [t for t in tasks(mine=True, deployment_id=None, c=c) if t["status"] != "done"]
        out = {
            "deployments": len(deps),
            "health": {h: sum(1 for d in deps if d["health"] == h) for h in ("on_track", "at_risk", "blocked")},
            "by_stage": [{"key": k, "name": stage_names.get(k, k), "count": n} for k, n in by_stage.items()],
            "my_open_tasks": len(my_tasks),
            "my_blocked": sum(1 for t in my_tasks if t["status"] == "blocked"),
        }
        if c.can("people.read"):
            ppl = people(c)
            out["team"] = [p for p in ppl if p["role"] == "fde"]
            out["unconfirmed_findings"] = conn.execute(
                "SELECT COUNT(*) FROM findings WHERE tenant_id=? AND confirmed_by IS NULL",
                (c.tenant_id,)).fetchone()[0]
        return out

    # --------------------------------------------------------------- audit

    @app.get("/api/audit")
    def audit_log(limit: int = 100, c: Ctx = Depends(ctx)):
        c.require("audit.read")
        rows = conn.execute(
            "SELECT a.seq, a.at, a.action, a.subject, a.detail_json, a.hash, a.prev_hash, u.name actor"
            " FROM audit a LEFT JOIN users u ON u.id=a.actor_id WHERE a.tenant_id=?"
            " ORDER BY a.seq DESC LIMIT ?", (c.tenant_id, max(1, min(limit, 500)))).fetchall()
        return [{**{k: r[k] for k in r.keys() if k != "detail_json"},
                 "detail": jloads(r["detail_json"])} for r in rows]

    @app.get("/api/audit/verify")
    def audit_verify(c: Ctx = Depends(ctx)):
        c.require("audit.verify")
        return audit.verify(conn, c.tenant_id)

    return app
