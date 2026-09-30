"""Fieldwork API.

Every request is authenticated (a personal access token, or a session from
company sign-in), resolved to one person in one workspace. Every query is
filtered by that workspace. Every write is recorded in the workspace's
hash-chained audit trail inside the same transaction.

Authorization reads the workspace's own permission map (config.py). Each grant
has a scope: "all" (every deployment in the workspace) or "own" (only
deployments the person is staffed on). Nothing about any role is hardcoded
here; what an Engagement Manager, FDE or customer may do is the customer's
decision.
"""

import hashlib
import json
import os
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

from . import audit, config, crypto, db, engines, events, ops, plugins, sso, trackers
from .engines import stages

FRONTEND = Path(__file__).resolve().parent / "static"
VISIBILITY = ("internal", "shared")


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(6)}"


def jloads(s: str | None) -> Any:
    return json.loads(s) if s else {}


def allow_private_engines() -> bool:
    return os.environ.get("FIELDWORK_ALLOW_PRIVATE_ENGINES") == "1"


def demo_on() -> bool:
    return os.environ.get("FIELDWORK_DEMO") == "1"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ------------------------------------------------------------------ context

class Ctx:
    def __init__(self, conn: db.DB, user, tenant, via: str = "token"):
        self.conn = conn
        self.user = user
        self.tenant_id = tenant["id"]
        self.tenant_name = tenant["name"]
        self.tenant_slug = tenant["slug"]
        self.cfg = config.upgrade(json.loads(tenant["config_json"]))
        self.via = via

    @property
    def uid(self) -> str:
        return self.user["id"]

    @property
    def role(self) -> str:
        return self.user["role"]

    def scope(self, action: str) -> str | None:
        return self.cfg["permissions"].get(action, {}).get(self.role)

    def can(self, action: str) -> bool:
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
            why = " on deployments you're not staffed on" if self.scope(action) == "own" else ""
            raise HTTPException(403, f"your role can't {config.ACTIONS[action].lower()}{why}")

    def deployment(self, dep_id: str):
        """Fetch a deployment this person may see, or 404 (never 403: don't leak existence)."""
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
    cat = {k: {**v, "key": k, "builtin": True, "kind": "stage" if k in stages.STAGE_ENGINES else "builtin"}
           for k, v in engines.REGISTRY.items()}
    for e in cfg.get("engines", []):
        cat[e["key"]] = {**e, "status": "live", "builtin": False}
    return cat


def create_app(db_url: str | None = None, background: bool = False) -> FastAPI:
    """background=True (used by `serve`) starts the outbox worker and, in demo mode, the demo reset."""
    conn = db.connect(db_url)
    db.init(conn)
    app = FastAPI(title="Fieldwork", version="0.8.0",
                  description="The platform deployment teams build their methodology on")
    app.state.conn = conn

    def load_tenant(tid: str):
        return conn.execute("SELECT * FROM tenants WHERE id=?", (tid,)).fetchone()

    def emit(c: "Ctx", event: str, dep_id: str | None = None, **data) -> None:
        """Queue notifications for an event. Call inside the change's transaction."""
        if dep_id:
            d = conn.execute("SELECT name FROM deployments WHERE id=?", (dep_id,)).fetchone()
            data = {"deployment_id": dep_id, "deployment": d["name"] if d else "", **data}
        data.setdefault("actor", c.user["name"])
        events.emit(conn, c.cfg, c.tenant_id, event, data)

    def uname(uid: str | None) -> str | None:
        r = conn.execute("SELECT name FROM users WHERE id=?", (uid,)).fetchone() if uid else None
        return r["name"] if r else None

    def ctx(authorization: str = Header(default="")) -> Ctx:
        if not authorization.lower().startswith("bearer "):
            raise HTTPException(401, "missing bearer token")
        tok = authorization.split(" ", 1)[1].strip()
        th = token_hash(tok)
        user = conn.execute("SELECT * FROM users WHERE token_hash=?", (th,)).fetchone()
        via = "token"
        if not user and tok.startswith("fwp_"):
            pt = conn.execute("SELECT * FROM personal_tokens WHERE token_hash=?", (th,)).fetchone()
            if not pt:
                raise HTTPException(401, "invalid or revoked token")
            user = conn.execute("SELECT * FROM users WHERE id=?", (pt["user_id"],)).fetchone()
            via = "personal_token"
            if not pt["last_used_at"] or pt["last_used_at"] < (utcnow() - timedelta(minutes=5)).isoformat():
                with db.tx(conn):
                    conn.execute("UPDATE personal_tokens SET last_used_at=? WHERE token_hash=?",
                                 (utcnow().isoformat(), th))
        elif not user:
            s = conn.execute("SELECT * FROM sessions WHERE token_hash=?", (th,)).fetchone()
            if not s or s["expires_at"] <= utcnow().isoformat():
                raise HTTPException(401, "invalid or expired sign-in")
            user = conn.execute("SELECT * FROM users WHERE id=?", (s["user_id"],)).fetchone()
            via = "sso"
        if not user:
            raise HTTPException(401, "invalid sign-in")
        if not user["active"]:
            raise HTTPException(401, "this account has been deactivated")
        c = Ctx(conn, user, load_tenant(user["tenant_id"]), via)
        # When a workspace requires company sign-in, personal tokens only work
        # for people who can edit settings (break-glass access if the IdP is down).
        if via == "token" and c.cfg["sso"]["required"] and not c.can("config.edit"):
            raise HTTPException(401, "this workspace requires company sign-in")
        return c

    for exc_type, code in ((config.ConfigError, 422), (engines.EngineError, 422),
                           (stages.StageEngineError, 422), (plugins.PluginError, 502),
                           (crypto.SecretError, 500)):
        def _handler(_: Request, exc, code=code):
            return JSONResponse({"detail": str(exc)}, status_code=code)
        app.add_exception_handler(exc_type, _handler)

    # --------------------------------------------------------------- shell

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(FRONTEND / "index.html")

    @app.get("/welcome", include_in_schema=False)
    def welcome():
        return FileResponse(FRONTEND / "welcome.html")

    @app.get("/api/health")
    def health():
        return {"ok": True, "database": conn.dialect, "dev_encryption_key": crypto.using_dev_key()}

    @app.get("/api/demo")
    def demo_logins():
        """Sign-in shortcuts for the demo workspace. Off unless FIELDWORK_DEMO=1."""
        if not demo_on():
            raise HTTPException(404, "not found")
        from .seed import DEMO_LOGINS
        return DEMO_LOGINS

    @app.get("/api/demo/sample-csv")
    def demo_csv():
        if not demo_on():
            raise HTTPException(404, "not found")
        from .seed import northfield_adoption_csv
        return {"csv": northfield_adoption_csv(), "metric": "minutes_per_exception", "baseline": 6}

    @app.get("/api/demo/sample-input/{engine_key}")
    def demo_input(engine_key: str):
        if not demo_on():
            raise HTTPException(404, "not found")
        from .seed import SAMPLE_INPUTS
        if engine_key not in SAMPLE_INPUTS:
            raise HTTPException(404, "no sample for this engine")
        return {"input": SAMPLE_INPUTS[engine_key]}

    @app.post("/demo-engines/readiness", include_in_schema=False)
    async def demo_readiness(request: Request):
        if not demo_on():
            raise HTTPException(404, "not found")
        from .demo_readiness import score
        from .seed import DEMO_ENGINE_SECRET
        body = await request.body()
        if not plugins.verify_signature(DEMO_ENGINE_SECRET, request.headers.get("x-fieldwork-timestamp", ""),
                                        body, request.headers.get("x-fieldwork-signature", "")):
            raise HTTPException(401, "bad signature")
        return score(json.loads(body).get("input", ""))

    @app.get("/api/me")
    def me(c: Ctx = Depends(ctx)):
        return {
            "user": {"id": c.uid, "name": c.user["name"], "email": c.user["email"], "role": c.role,
                     "role_name": config.role_name(c.cfg, c.role)},
            "tenant": {"id": c.tenant_id, "name": c.tenant_name, "slug": c.tenant_slug},
            "branding": c.cfg["branding"],
            "can": {a: c.scope(a) for a in config.ACTIONS if c.can(a)},
            "home": c.cfg["views"].get(c.role, []),
            "signed_in_via": c.via,
        }

    # ------------------------------------------------------ company sign-in

    @app.get("/api/auth/workspace/{slug}")
    def workspace_info(slug: str):
        """Public: what the sign-in page needs to show for a workspace."""
        t = conn.execute("SELECT * FROM tenants WHERE slug=?", (slug,)).fetchone()
        if not t:
            raise HTTPException(404, "workspace not found")
        cfg = config.upgrade(json.loads(t["config_json"]))
        return {"name": t["name"], "branding": cfg["branding"], "sso": cfg["sso"]["enabled"],
                "sso_required": cfg["sso"]["required"]}

    @app.get("/auth/sso/{slug}/start", include_in_schema=False)
    def sso_start(slug: str):
        t = conn.execute("SELECT * FROM tenants WHERE slug=?", (slug,)).fetchone()
        if not t:
            raise HTTPException(404, "workspace not found")
        cfg = config.upgrade(json.loads(t["config_json"]))
        if not cfg["sso"]["enabled"]:
            raise HTTPException(409, "company sign-in isn't set up for this workspace")
        state, nonce = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
        verifier, challenge = sso.pkce_pair()
        try:
            url = sso.authorize_url(cfg["sso"], state, nonce, challenge)
        except sso.SSOError as e:
            raise HTTPException(502, str(e))
        with db.tx(conn):
            conn.execute("DELETE FROM sso_states WHERE created_at < ?",
                         ((utcnow() - timedelta(seconds=sso.STATE_TTL_S)).isoformat(),))
            conn.execute("INSERT INTO sso_states (state, tenant_id, nonce, code_verifier, created_at)"
                         " VALUES (?,?,?,?,?)", (state, t["id"], nonce, verifier, utcnow().isoformat()))
        return RedirectResponse(url, status_code=302)

    @app.get("/auth/sso/callback", include_in_schema=False)
    def sso_callback(state: str = "", code: str = "", error: str = "", error_description: str = ""):
        def fail(msg: str):
            from urllib.parse import quote
            return RedirectResponse(f"/#sso_error={quote(msg)}", status_code=302)

        if error:
            return fail(error_description or error)
        with db.tx(conn):
            st = conn.execute("SELECT * FROM sso_states WHERE state=?", (state,)).fetchone()
            if st:
                conn.execute("DELETE FROM sso_states WHERE state=?", (state,))  # single use
        if not st or st["created_at"] < (utcnow() - timedelta(seconds=sso.STATE_TTL_S)).isoformat():
            return fail("sign-in expired or was already used; start again")
        t = load_tenant(st["tenant_id"])
        cfg = config.upgrade(json.loads(t["config_json"]))
        secret_row = conn.execute("SELECT secret FROM tenant_secrets WHERE tenant_id=? AND name='sso_client_secret'",
                                  (t["id"],)).fetchone()
        if not secret_row:
            return fail("company sign-in is missing its client secret")
        try:
            id_token = sso.exchange(cfg["sso"], crypto.decrypt(secret_row["secret"]), code, st["code_verifier"])
            claims = sso.verify_id_token(cfg["sso"], id_token, st["nonce"])
        except sso.SSOError as e:
            return fail(str(e))
        user = conn.execute("SELECT * FROM users WHERE tenant_id=? AND lower(email)=?",
                            (t["id"], claims["email"])).fetchone()
        tmp_actor = "sso:" + claims["email"]
        if user and not user["active"]:
            return fail("this account has been deactivated; ask an admin to reactivate it")
        with db.tx(conn):
            if not user:
                role = cfg["sso"]["jit_role"]
                if not role:
                    audit.record(conn, t["id"], tmp_actor, "auth.sso_denied", claims["email"],
                                 {"reason": "no account and just-in-time access is off"})
                    return fail("you don't have an account in this workspace yet; ask an admin to add you")
                uid = new_id("usr")
                name = claims.get("name") or claims["email"].split("@")[0]
                conn.execute("INSERT INTO users (id, tenant_id, name, email, role, manager_id, token_hash,"
                             " profile_json, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                             (uid, t["id"], name, claims["email"], role, None,
                              token_hash("disabled:" + secrets.token_hex(16)), "{}", utcnow().isoformat()))
                audit.record(conn, t["id"], tmp_actor, "people.add", uid,
                             {"name": name, "role": role, "via": "sso just-in-time"})
                user = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
            tok = "fwsess_" + secrets.token_urlsafe(32)
            conn.execute("INSERT INTO sessions (token_hash, user_id, tenant_id, method, created_at, expires_at)"
                         " VALUES (?,?,?,?,?,?)",
                         (token_hash(tok), user["id"], t["id"], "oidc", utcnow().isoformat(),
                          (utcnow() + timedelta(hours=sso.SESSION_HOURS)).isoformat()))
            audit.record(conn, t["id"], user["id"], "auth.sso_login", user["id"],
                         {"issuer": claims["iss"], "subject": claims["sub"]})
        return RedirectResponse(f"/#session={tok}", status_code=302)

    @app.post("/api/auth/logout")
    def logout(authorization: str = Header(default="")):
        tok = authorization.split(" ", 1)[1].strip() if " " in authorization else ""
        with db.tx(conn):
            conn.execute("DELETE FROM sessions WHERE token_hash=?", (token_hash(tok),))
        return {"ok": True}

    class SecretIn(BaseModel):
        client_secret: str = Field(min_length=1, max_length=2000)

    @app.put("/api/sso/secret")
    def set_sso_secret(body: SecretIn, c: Ctx = Depends(ctx)):
        c.require("config.edit")
        with db.tx(conn):
            conn.execute("INSERT INTO tenant_secrets (tenant_id, name, secret, created_at) VALUES (?,?,?,?)"
                         " ON CONFLICT (tenant_id, name) DO UPDATE SET secret=excluded.secret,"
                         " created_at=excluded.created_at",
                         (c.tenant_id, "sso_client_secret", crypto.encrypt(body.client_secret),
                          utcnow().isoformat()))
            c.log("sso.secret_set", c.tenant_id, {})
        return {"ok": True}

    @app.get("/api/sso")
    def sso_status(c: Ctx = Depends(ctx)):
        c.require("config.edit")
        has = conn.execute("SELECT 1 FROM tenant_secrets WHERE tenant_id=? AND name='sso_client_secret'",
                           (c.tenant_id,)).fetchone() is not None
        return {"config": c.cfg["sso"], "client_secret_set": has, "redirect_uri": sso.redirect_uri(),
                "start_url": f"{sso.public_url()}/auth/sso/{c.tenant_slug}/start"}

    # -------------------------------------------------------------- config

    @app.get("/api/config")
    def get_config(c: Ctx = Depends(ctx)):
        return {"config": c.cfg, "actions": config.ACTIONS, "engines": engine_catalog(c.cfg),
                "widgets": config.WIDGETS, "workspace_actions": sorted(config.WORKSPACE_ACTIONS),
                "defaults": config.default()}

    @app.put("/api/config")
    def put_config(body: dict, c: Ctx = Depends(ctx)):
        c.require("config.edit")
        new = config.validate(body, allow_http_engines=allow_private_engines() or sso.allow_insecure(),
                              known_urls=frozenset(e.get("url") for e in c.cfg.get("engines", [])))
        if new["engines"] != c.cfg.get("engines", []):
            c.require("engine.manage")
        if new["permissions"]["config.edit"].get(c.role) != "all":
            raise HTTPException(422, "you'd lose access to settings; keep config.edit on your own role")
        if new["sso"]["required"] and not conn.execute(
                "SELECT 1 FROM tenant_secrets WHERE tenant_id=? AND name='sso_client_secret'",
                (c.tenant_id,)).fetchone():
            raise HTTPException(422, "set the SSO client secret before requiring company sign-in")
        role_keys = {r["key"] for r in new["roles"]}
        in_use = {r["role"] for r in conn.execute(
            "SELECT DISTINCT role FROM users WHERE tenant_id=?", (c.tenant_id,))}
        if in_use - role_keys:
            raise HTTPException(409, f"people still hold role(s): {', '.join(sorted(in_use - role_keys))}; "
                                     "move them first")
        live = {r["stage"] for r in conn.execute(
            "SELECT DISTINCT stage FROM deployments WHERE tenant_id=?", (c.tenant_id,))}
        keys = set(config.stage_keys(new))
        if live - keys:
            raise HTTPException(409, f"can't remove stage(s) with live deployments: {', '.join(sorted(live - keys))}")
        removed_engines = {e["key"] for e in c.cfg.get("engines", [])} - {e["key"] for e in new["engines"]}
        with db.tx(conn):
            conn.execute("UPDATE tenants SET config_json=? WHERE id=?", (json.dumps(new), c.tenant_id))
            for k in removed_engines:
                conn.execute("DELETE FROM engine_credentials WHERE tenant_id=? AND engine_key=?", (c.tenant_id, k))
            c.log("config.update", c.tenant_id, {
                "roles": sorted(role_keys), "stages": config.stage_keys(new),
                "branding": new["branding"]["product_name"], "sso": new["sso"]["enabled"],
                "sso_required": new["sso"]["required"], "engines_removed": sorted(removed_engines)})
        return {"config": new}

    # -------------------------------------------------------------- people

    @app.get("/api/people")
    def people(c: Ctx = Depends(ctx)):
        c.require("people.read")
        rows = conn.execute(
            """SELECT u.id, u.name, u.email, u.role, u.weekly_hours, u.active, u.deactivated_at,
                      (SELECT COUNT(*) FROM tasks t WHERE t.assignee_id=u.id AND t.status!='done') open_tasks,
                      (SELECT COUNT(*) FROM deployment_members m WHERE m.user_id=u.id) deployments
               FROM users u WHERE u.tenant_id=? ORDER BY u.name""", (c.tenant_id,)).fetchall()
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
        ex = conn.execute("SELECT active FROM users WHERE tenant_id=? AND lower(email)=?",
                          (c.tenant_id, body.email.strip().lower())).fetchone()
        if ex:
            raise HTTPException(409, "someone with that email is already in the workspace" if ex["active"]
                                else "that person was deactivated; reactivate them instead")
        uid = new_id("usr")
        tok = "fwu_" + secrets.token_urlsafe(24)
        with db.tx(conn):
            conn.execute("INSERT INTO users (id, tenant_id, name, email, role, manager_id, token_hash,"
                         " profile_json, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                         (uid, c.tenant_id, body.name, body.email.strip(), body.role, None,
                          token_hash(tok), "{}", audit.now()))
            c.log("people.add", uid, {"name": body.name, "role": body.role})
        from . import beta
        ways = [{"github": "GitHub", "google": "Google"}.get(w, w) for w in beta.configured()]
        if c.cfg["sso"]["required"]:
            note = "this workspace requires company sign-in, so they'll sign in through your identity provider"
        elif ways:
            note = (f"they sign in at {events.public_url()} with {' or '.join(ways)} using {body.email.strip()}; "
                    "the token is a fallback, shown once")
        else:
            note = "shown once; share it securely"
        return {"id": uid, "token": tok, "note": note, "sign_in_with": ways, "sign_in_url": events.public_url()}

    class PersonPatch(BaseModel):
        role: str | None = None
        weekly_hours: float | None = Field(default=None, ge=0, le=80)

    class OffboardIn(BaseModel):
        reassign_to: str | None = None   # open tasks go here; None leaves them unassigned for someone to pick up

    def managers_left(c: Ctx, excluding: str) -> int:
        roles = [r for r, s in c.cfg["permissions"].get("people.manage", {}).items() if s]
        if not roles:
            return 0
        return conn.execute(f"SELECT COUNT(*) n FROM users WHERE tenant_id=? AND active=1 AND id!=?"
                            f" AND role IN ({','.join('?' * len(roles))})", (c.tenant_id, excluding, *roles)).fetchone()["n"]

    @app.post("/api/people/{user_id}/deactivate")
    def deactivate(user_id: str, body: OffboardIn, c: Ctx = Depends(ctx)):
        """Offboarding: access ends everywhere at once, and their work doesn't fall on the floor."""
        c.require("people.manage")
        u = conn.execute("SELECT * FROM users WHERE id=? AND tenant_id=?", (user_id, c.tenant_id)).fetchone()
        if not u:
            raise HTTPException(404, "person not found")
        if user_id == c.uid:
            raise HTTPException(422, "you can't deactivate yourself; ask another admin")
        if not u["active"]:
            raise HTTPException(409, "already deactivated")
        if c.cfg["permissions"].get("people.manage", {}).get(u["role"]) and not managers_left(c, user_id):
            raise HTTPException(422, "they're the last person who can manage people; give someone else that role first")
        heir = None
        if body.reassign_to:
            heir = tenant_user(c, body.reassign_to)
            if heir["id"] == user_id:
                raise HTTPException(422, "pick someone else to take their work")
        ts = audit.now()
        with db.tx(conn):
            open_tasks = conn.execute("SELECT * FROM tasks WHERE tenant_id=? AND assignee_id=? AND status!='done'",
                                      (c.tenant_id, user_id)).fetchall()
            moved, unassigned = [], []
            for t in open_tasks:
                if heir and (t["visibility"] == "shared" or sees_internal_tasks(c, heir["role"], t["deployment_id"], heir["id"])):
                    conn.execute("UPDATE tasks SET assignee_id=?, updated_at=? WHERE id=?", (heir["id"], ts, t["id"]))
                    moved.append(t["id"])
                else:  # nobody named, or the named person couldn't see it: it waits in Unassigned for a lead
                    conn.execute("UPDATE tasks SET assignee_id=NULL, updated_at=? WHERE id=?", (ts, t["id"]))
                    unassigned.append(t["id"])
            deps = [r["deployment_id"] for r in conn.execute(
                "SELECT deployment_id FROM deployment_members WHERE user_id=?", (user_id,))]
            conn.execute("DELETE FROM deployment_members WHERE user_id=?", (user_id,))
            conn.execute("UPDATE deployments SET lead_id=NULL WHERE lead_id=? AND tenant_id=?", (user_id, c.tenant_id))
            conn.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
            conn.execute("DELETE FROM personal_tokens WHERE user_id=?", (user_id,))
            personal = [r["id"] for r in conn.execute(
                "SELECT id FROM connections WHERE user_id=? AND status!='disconnected'", (user_id,))]
            for cid in personal:  # their calendar and mailbox: tokens forgotten, what was collected deleted
                conn.execute("UPDATE connections SET status='disconnected', tokens=NULL, updated_at=? WHERE id=?", (ts, cid))
                conn.execute("DELETE FROM time_off WHERE connection_id=?", (cid,))
                conn.execute("DELETE FROM contact_signals WHERE connection_id=?", (cid,))
            conn.execute("UPDATE users SET active=0, deactivated_at=?, token_hash=? WHERE id=?",
                         (ts, token_hash("deactivated:" + secrets.token_hex(16)), user_id))
            c.log("people.deactivate", user_id, {"name": u["name"], "tasks_reassigned": len(moved),
                                                 "tasks_unassigned": len(unassigned), "reassigned_to": heir["id"] if heir else None,
                                                 "deployments_left": deps, "connections_removed": len(personal)})
        return {"tasks_reassigned": len(moved), "tasks_unassigned": len(unassigned), "deployments_left": len(deps),
                "connections_removed": len(personal)}

    @app.post("/api/people/{user_id}/reactivate")
    def reactivate(user_id: str, c: Ctx = Depends(ctx)):
        c.require("people.manage")
        u = conn.execute("SELECT * FROM users WHERE id=? AND tenant_id=?", (user_id, c.tenant_id)).fetchone()
        if not u:
            raise HTTPException(404, "person not found")
        if u["active"]:
            raise HTTPException(409, "already active")
        with db.tx(conn):
            conn.execute("UPDATE users SET active=1, deactivated_at=NULL WHERE id=?", (user_id,))
            c.log("people.reactivate", user_id, {"name": u["name"]})
        return {"ok": True, "note": "they sign in again the usual way; staff them on deployments again as needed"}

    @app.patch("/api/people/{user_id}")
    def change_role(user_id: str, body: PersonPatch, c: Ctx = Depends(ctx)):
        c.require("people.manage")
        u = conn.execute("SELECT * FROM users WHERE id=? AND tenant_id=?", (user_id, c.tenant_id)).fetchone()
        if not u:
            raise HTTPException(404, "person not found")
        if body.role is not None and body.role not in {r["key"] for r in c.cfg["roles"]}:
            raise HTTPException(422, f"unknown role {body.role!r}")
        if body.weekly_hours is not None:
            with db.tx(conn):
                conn.execute("UPDATE users SET weekly_hours=? WHERE id=?", (body.weekly_hours, user_id))
                c.log("people.hours", user_id, {"weekly_hours": body.weekly_hours})
        if body.role is None:
            return {"ok": True}
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
            rows = conn.execute("SELECT * FROM customers WHERE tenant_id=? ORDER BY name", (c.tenant_id,)).fetchall()
        return [{**dict(r), "fields": jloads(r["fields_json"])} for r in rows]

    @app.post("/api/customers", status_code=201)
    def add_customer(body: CustomerIn, c: Ctx = Depends(ctx)):
        c.require("deployment.create")
        fields = config.check_fields(c.cfg, "customer", body.fields)
        cid = new_id("cus")
        with db.tx(conn):
            conn.execute("INSERT INTO customers (id, tenant_id, name, industry, fields_json, created_at)"
                         " VALUES (?,?,?,?,?,?)",
                         (cid, c.tenant_id, body.name, body.industry, json.dumps(fields), audit.now()))
            c.log("customer.create", cid, {"name": body.name})
        return {"id": cid}

    # --------------------------------------------------------- deployments

    def dep_out(r, c: Ctx) -> dict:
        members = conn.execute(
            "SELECT u.id, u.name, u.role, m.allocation FROM deployment_members m JOIN users u ON u.id=m.user_id"
            " WHERE m.deployment_id=? ORDER BY u.name", (r["id"],)).fetchall()
        cust = conn.execute("SELECT name FROM customers WHERE id=?", (r["customer_id"],)).fetchone()
        vis = "" if c.can_on("task.view_internal", r["id"]) else " AND visibility='shared'"
        counts = conn.execute(f"SELECT status, COUNT(*) n FROM tasks WHERE deployment_id=?{vis} GROUP BY status",
                              (r["id"],)).fetchall()
        return {
            "id": r["id"], "name": r["name"], "customer_id": r["customer_id"],
            "customer": cust["name"] if cust else "", "stage": r["stage"], "health": r["health"],
            "lead_id": r["lead_id"], "fields": jloads(r["fields_json"]),
            "staffing_req": r["staffing_req"] if c.can_on("bench.match", r["id"]) else "",
            "members": [dict(m) for m in members],
            "tasks": {x["status"]: x["n"] for x in counts},
            "start_on": r["start_on"], "end_on": r["end_on"],
            "budget_hours": r["budget_hours"] if c.can_on("task.view_internal", r["id"]) else None,
            "updated_at": r["updated_at"],
        }

    def visible_deployments(c: Ctx) -> list:
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
        return [dep_out(r, c) for r in visible_deployments(c)]

    class DeploymentIn(BaseModel):
        customer_id: str
        name: str = Field(min_length=1, max_length=120)
        lead_id: str | None = None
        fields: dict = {}
        staffing_req: str = ""

    def tenant_user(c: Ctx, uid: str):
        u = conn.execute("SELECT * FROM users WHERE id=? AND tenant_id=?", (uid, c.tenant_id)).fetchone()
        if u and not u["active"]:
            raise HTTPException(422, f"{u['name']} has been deactivated")
        if not u:
            raise HTTPException(422, f"no such person in this workspace: {uid}")
        return u

    def add_stage_event(c: Ctx, dep_id: str, frm, to: str, note: str, ts: str) -> None:
        conn.execute("INSERT INTO stage_events (tenant_id, deployment_id, from_stage, to_stage, actor_id, note, at)"
                     " VALUES (?,?,?,?,?,?,?)", (c.tenant_id, dep_id, frm, to, c.uid, note, ts))

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
            conn.execute("INSERT INTO deployments (id, tenant_id, customer_id, name, stage, health, lead_id,"
                         " fields_json, staffing_req, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                         (did, c.tenant_id, body.customer_id, body.name, first, "on_track",
                          body.lead_id, json.dumps(fields), body.staffing_req, ts, ts))
            for uid in sorted({body.lead_id, c.uid} - {None}):
                conn.execute("INSERT INTO deployment_members (deployment_id, user_id) VALUES (?,?)"
                             " ON CONFLICT DO NOTHING", (did, uid))
            add_stage_event(c, did, None, first, "opened", ts)
            c.log("deployment.create", did, {"name": body.name, "stage": first})
        return {"id": did}

    @app.get("/api/deployments/{dep_id}")
    def deployment(dep_id: str, c: Ctx = Depends(ctx)):
        r = c.deployment(dep_id)
        out = dep_out(r, c)
        out["history"] = [dict(e) for e in conn.execute(
            "SELECT e.from_stage, e.to_stage, e.note, e.at, u.name actor FROM stage_events e"
            " JOIN users u ON u.id=e.actor_id WHERE e.deployment_id=? ORDER BY e.id", (dep_id,))]
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
        with db.tx(conn):
            ops.move_to(conn, c.tenant_id, c.cfg, r, body.to_stage, c.uid, body.note.strip())
            c.log("deployment.advance", dep_id, {"from": r["stage"], "to": body.to_stage, "note": body.note})
            emit(c, "deployment.advanced", dep_id, to=body.to_stage,
                 to_name=next(x["name"] for x in c.cfg["stages"] if x["key"] == body.to_stage))
        return {"stage": body.to_stage}

    class PatchDeployment(BaseModel):
        health: str | None = None
        fields: dict | None = None
        staffing_req: str | None = None
        start_on: str | None = None
        end_on: str | None = None
        budget_hours: float | None = Field(default=None, ge=0, le=1_000_000)

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
        for k in ("start_on", "end_on"):
            v = getattr(body, k)
            if v is not None:
                c.require_on("deployment.staff", dep_id)
                if v and not ops.parse_day(v):
                    raise HTTPException(422, f"{k} must be YYYY-MM-DD")
                changes[k] = v[:10] or None
        if body.budget_hours is not None:
            c.require_on("deployment.staff", dep_id)
            changes["budget_hours"] = body.budget_hours or None
        s_on = changes.get("start_on", r["start_on"])
        e_on = changes.get("end_on", r["end_on"])
        if s_on and e_on and e_on < s_on:
            raise HTTPException(422, "end_on is before start_on")
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
            if changes.get("health") in ("at_risk", "blocked") and changes["health"] != r["health"]:
                emit(c, "deployment.health", dep_id, health=changes["health"])
        return {"changed": [k for k in changes if k != "updated_at"]}

    class MemberIn(BaseModel):
        user_id: str

    @app.post("/api/deployments/{dep_id}/members")
    def add_member(dep_id: str, body: MemberIn, c: Ctx = Depends(ctx)):
        c.deployment(dep_id)
        c.require_on("deployment.staff", dep_id)
        tenant_user(c, body.user_id)
        with db.tx(conn):
            conn.execute("INSERT INTO deployment_members (deployment_id, user_id) VALUES (?,?)"
                         " ON CONFLICT DO NOTHING", (dep_id, body.user_id))
            c.log("deployment.staff", dep_id, {"added": body.user_id})
        return {"ok": True}

    @app.delete("/api/deployments/{dep_id}/members/{user_id}")
    def remove_member(dep_id: str, user_id: str, c: Ctx = Depends(ctx)):
        c.deployment(dep_id)
        c.require_on("deployment.staff", dep_id)
        with db.tx(conn):
            conn.execute("DELETE FROM deployment_members WHERE deployment_id=? AND user_id=?", (dep_id, user_id))
            c.log("deployment.staff", dep_id, {"removed": user_id})
        return {"ok": True}

    # --------------------------------------------------------------- tasks

    def task_out(r) -> dict:
        d = dict(r)
        who = conn.execute("SELECT name FROM users WHERE id=?", (r["assignee_id"],)).fetchone() \
            if r["assignee_id"] else None
        dep = conn.execute("SELECT name FROM deployments WHERE id=?", (r["deployment_id"],)).fetchone()
        d["assignee"] = who["name"] if who else None
        d["deployment"] = dep["name"] if dep else ""
        return d

    def sees_internal_tasks(c: Ctx, user_role: str, dep_id: str, user_id: str) -> bool:
        """Would this person (not necessarily the caller) see internal tasks on dep?"""
        s = c.cfg["permissions"].get("task.view_internal", {}).get(user_role)
        if s == "all":
            return True
        return s == "own" and conn.execute(
            "SELECT 1 FROM deployment_members WHERE deployment_id=? AND user_id=?",
            (dep_id, user_id)).fetchone() is not None

    @app.get("/api/tasks")
    def tasks(mine: bool = False, deployment_id: str | None = None, c: Ctx = Depends(ctx)):
        deps = [r["id"] for r in visible_deployments(c)]
        if deployment_id:
            c.deployment(deployment_id)
            deps = [deployment_id]
        if not deps:
            return []
        internal = [d for d in deps if c.can_on("task.view_internal", d)]
        shared_only = [d for d in deps if d not in internal]
        clauses, args = [], [c.tenant_id]
        if internal:
            clauses.append(f"deployment_id IN ({','.join('?' * len(internal))})")
            args += internal
        if shared_only:
            clauses.append(f"(deployment_id IN ({','.join('?' * len(shared_only))}) AND visibility='shared')")
            args += shared_only
        q = f"SELECT * FROM tasks WHERE tenant_id=? AND ({' OR '.join(clauses)})"
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
        visibility: str = "internal"
        unassigned: bool = False

    @app.post("/api/tasks", status_code=201)
    def add_task(body: TaskIn, c: Ctx = Depends(ctx)):
        dep = c.deployment(body.deployment_id)
        c.require_on("task.create", body.deployment_id)
        if body.visibility not in VISIBILITY:
            raise HTTPException(422, "visibility must be internal or shared")
        if body.unassigned:
            c.require_on("task.assign", body.deployment_id)
            assignee, a = None, None
        else:
            assignee = body.assignee_id or c.uid
            if assignee != c.uid:
                c.require_on("task.assign", body.deployment_id)
            a = tenant_user(c, assignee)
        visibility = body.visibility
        if a and not sees_internal_tasks(c, a["role"], body.deployment_id, assignee):
            visibility = "shared"  # a task for the customer has to be one they can see
        if visibility == "shared":
            c.require_on("customer.share", body.deployment_id)
        stage = body.stage or dep["stage"]
        if stage not in config.stage_keys(c.cfg):
            raise HTTPException(422, f"unknown stage {stage!r}")
        tid = new_id("tsk")
        ts = audit.now()
        with db.tx(conn):
            conn.execute("INSERT INTO tasks (id, tenant_id, deployment_id, stage, title, assignee_id, status,"
                         " due, created_by, created_at, updated_at, visibility) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                         (tid, c.tenant_id, body.deployment_id, stage, body.title, assignee,
                          "open", body.due, c.uid, ts, ts, visibility))
            c.log("task.create", tid, {"deployment": body.deployment_id, "assignee": assignee,
                                       "title": body.title, "visibility": visibility})
            if a and assignee != c.uid:
                emit(c, "task.assigned", body.deployment_id, title=body.title, assignee=a["name"], task_id=tid,
                     assignee_id=assignee)
            trackers.queue_push(conn, c.tenant_id, c.cfg, dep, tid)
        return {"id": tid, "visibility": visibility}

    class TaskPatch(BaseModel):
        status: str | None = None
        assignee_id: str | None = None
        due: str | None = None
        visibility: str | None = None
        waiting_on: str | None = None          # customer | team | model_vendor | software_vendor | "" (unsure)
        blocked_reason: str | None = Field(default=None, max_length=300)

    @app.patch("/api/tasks/{task_id}")
    def patch_task(task_id: str, body: TaskPatch, c: Ctx = Depends(ctx)):
        t = conn.execute("SELECT * FROM tasks WHERE id=? AND tenant_id=?", (task_id, c.tenant_id)).fetchone()
        if not t:
            raise HTTPException(404, "task not found")
        c.deployment(t["deployment_id"])
        if t["visibility"] != "shared" and not c.can_on("task.view_internal", t["deployment_id"]):
            raise HTTPException(404, "task not found")
        if t["assignee_id"] != c.uid:
            c.require_on("task.update_any", t["deployment_id"])
        changes: dict = {}
        if body.status is not None:
            if body.status not in ("open", "in_progress", "blocked", "done"):
                raise HTTPException(422, "bad status")
            changes["status"] = body.status
        if body.assignee_id is not None:
            c.require_on("task.assign", t["deployment_id"])
            a = tenant_user(c, body.assignee_id)
            if t["visibility"] != "shared" and not sees_internal_tasks(c, a["role"], t["deployment_id"], a["id"]):
                raise HTTPException(422, "share the task with the customer before assigning it to them")
            changes["assignee_id"] = body.assignee_id
        if body.due is not None:
            changes["due"] = body.due or None
        if body.waiting_on is not None:
            if body.waiting_on and body.waiting_on not in ops.OWNERS:
                raise HTTPException(422, f"waiting_on must be one of {', '.join(ops.OWNERS)}")
            changes["waiting_on"] = body.waiting_on or None
        if body.blocked_reason is not None:
            changes["blocked_reason"] = body.blocked_reason.strip()
        if body.visibility is not None:
            if body.visibility not in VISIBILITY:
                raise HTTPException(422, "visibility must be internal or shared")
            c.require_on("customer.share", t["deployment_id"])
            if body.visibility == "internal" and t["assignee_id"]:
                a = conn.execute("SELECT * FROM users WHERE id=?", (t["assignee_id"],)).fetchone()
                if a and not sees_internal_tasks(c, a["role"], t["deployment_id"], a["id"]):
                    raise HTTPException(422, "it's assigned to someone who only sees shared tasks")
            changes["visibility"] = body.visibility
        if not changes:
            return {"changed": []}
        changes["updated_at"] = audit.now()
        with db.tx(conn):
            conn.execute(f"UPDATE tasks SET {', '.join(k + '=?' for k in changes)} WHERE id=?",
                         (*changes.values(), task_id))
            c.log("task.update", task_id, {k: v for k, v in changes.items() if k != "updated_at"})
            merged = {**dict(t), **changes}
            if "status" in changes:
                ops.on_task_status(conn, c.tenant_id, c.tenant_name, c.cfg, {**merged, "status": t["status"]},
                                   changes["status"])
                if changes["status"] != "blocked":
                    conn.execute("UPDATE tasks SET waiting_on=NULL, blocked_reason='' WHERE id=?", (task_id,))
            elif merged["status"] == "blocked" and ("waiting_on" in changes or "blocked_reason" in changes) \
                    and merged.get("waiting_on"):
                ops.on_waiting_on(conn, merged, merged["waiting_on"], merged.get("blocked_reason") or "")
            title = t["title"]
            assignee_name = uname(changes.get("assignee_id", t["assignee_id"]))
            if changes.get("status") == "blocked" and t["status"] != "blocked":
                emit(c, "task.blocked", t["deployment_id"], title=title, assignee=assignee_name, task_id=task_id)
            if changes.get("status") == "done" and t["status"] != "done":
                emit(c, "task.done", t["deployment_id"], title=title, task_id=task_id)
            if "assignee_id" in changes and changes["assignee_id"] != t["assignee_id"]:
                emit(c, "task.assigned", t["deployment_id"], title=title, assignee=assignee_name, task_id=task_id,
                     assignee_id=changes["assignee_id"])
            if set(changes) & {"status", "title"}:
                dep_row = conn.execute("SELECT * FROM deployments WHERE id=?", (t["deployment_id"],)).fetchone()
                trackers.queue_push(conn, c.tenant_id, c.cfg, dep_row, task_id)
        return {"changed": [k for k in changes if k != "updated_at"]}

    # ------------------------------------------------------------- engines

    @app.get("/api/engines")
    def engine_list(c: Ctx = Depends(ctx)):
        cat = engine_catalog(c.cfg)
        creds = {r["engine_key"] for r in conn.execute(
            "SELECT engine_key FROM engine_credentials WHERE tenant_id=?", (c.tenant_id,))}
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

    def issue_credential(cfg: dict, key: str) -> dict:
        e = custom_engine(cfg, key)
        if not e:
            raise HTTPException(404, "no such custom engine")
        if e["kind"] == "webhook":
            s = "fws_" + secrets.token_urlsafe(32)
            return {"secret": crypto.encrypt(s), "token_hash": None,
                    "public": {"signing_secret": s,
                               "note": "shown once; your service verifies X-Fieldwork-Signature with it"}}
        t = "fwe_" + secrets.token_urlsafe(32)
        return {"secret": None, "token_hash": token_hash(t),
                "public": {"engine_token": t, "note": "shown once; your script posts to /api/ingest/findings with it"}}

    def store_credential(c: Ctx, key: str, cred: dict) -> None:
        conn.execute("INSERT INTO engine_credentials (tenant_id, engine_key, secret, token_hash, created_at)"
                     " VALUES (?,?,?,?,?) ON CONFLICT (tenant_id, engine_key) DO UPDATE SET"
                     " secret=excluded.secret, token_hash=excluded.token_hash, created_at=excluded.created_at",
                     (c.tenant_id, key, cred["secret"], cred["token_hash"], audit.now()))

    @app.post("/api/engines", status_code=201)
    def register_engine(body: EngineIn, c: Ctx = Depends(ctx)):
        """Register a team's own script or service. Returns its credential once."""
        c.require("engine.manage")
        cfg = json.loads(json.dumps(c.cfg))
        cfg["engines"] = [*cfg.get("engines", []), body.model_dump(exclude_none=True)]
        new = config.validate(cfg, allow_http_engines=allow_private_engines(),
                              known_urls=frozenset(e.get("url") for e in c.cfg.get("engines", [])))
        cred = issue_credential(new, body.key)
        with db.tx(conn):
            conn.execute("UPDATE tenants SET config_json=? WHERE id=?", (json.dumps(new), c.tenant_id))
            store_credential(c, body.key, cred)
            c.log("engine.register", body.key, {"kind": body.kind, "name": body.name, "url": body.url or None})
        return {"engine": custom_engine(new, body.key), **cred["public"]}

    @app.post("/api/engines/{key}/rotate")
    def rotate_engine(key: str, c: Ctx = Depends(ctx)):
        c.require("engine.manage")
        cred = issue_credential(c.cfg, key)
        with db.tx(conn):
            store_credential(c, key, cred)
            c.log("engine.rotate", key, {})
        return cred["public"]

    def engine_gate(c: Ctx, dep_id: str):
        dep = c.deployment(dep_id)
        c.require_on("engine.run", dep_id)
        return dep

    def save_finding(c: Ctx, dep_id: str, engine: str, title: str, result: dict, actor: str | None = None) -> str:
        fid = new_id("fnd")
        digest = "sha256:" + hashlib.sha256(audit.canonical(result).encode()).hexdigest()
        with db.tx(conn):
            conn.execute("INSERT INTO findings (id, tenant_id, deployment_id, engine, title, result_json,"
                         " confirmed_by, confirmed_at, created_by, created_at, visibility)"
                         " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                         (fid, c.tenant_id, dep_id, engine, title, json.dumps(result),
                          None, None, actor or c.uid, audit.now(), "internal"))
            c.log("engine.run", fid, {"engine": engine, "deployment": dep_id, "title": title,
                                      "result_digest": digest}, actor=actor)
            summary = result.get("summary") if isinstance(result, dict) else None
            if engine == "sendero":
                summary = f"{result.get('classification')} ({result.get('confidence_pct')}% confidence)"
            elif engine == "threshold":
                summary = f"{len(result.get('ranked', []))} people scored"
            emit(c, "finding.created", dep_id, title=title, summary=summary or "", finding_id=fid,
                 engine_name=engine_catalog(c.cfg).get(engine, {}).get("name", engine))
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
        return {"finding_id": save_finding(c, dep_id, "sendero", body.title, result), "result": result}

    @app.post("/api/deployments/{dep_id}/engines/threshold")
    def run_threshold(dep_id: str, c: Ctx = Depends(ctx)):
        dep = c.deployment(dep_id)
        c.require_on("bench.match", dep_id)
        rows = conn.execute("SELECT id, name, profile_json FROM users WHERE tenant_id=?", (c.tenant_id,)).fetchall()
        pool = [{"id": p["id"], "name": p["name"], "profile": jloads(p["profile_json"])}
                for p in rows if jloads(p["profile_json"]).get("evidence")]
        if not pool:
            raise HTTPException(422, "nobody in the workspace has a bench profile yet")
        result = engines.run_threshold(dep["staffing_req"], dep["name"], pool)
        fid = save_finding(c, dep_id, "threshold", f"Bench match · {dep['name']}", result)
        return {"finding_id": fid, "result": result}

    class RunIn(BaseModel):
        title: str = Field(default="", max_length=200)
        input: str = Field(default="", max_length=2_000_000)

    @app.post("/api/deployments/{dep_id}/engines/stage/{key}")
    def run_stage_engine(dep_id: str, key: str, body: RunIn, c: Ctx = Depends(ctx)):
        dep = engine_gate(c, dep_id)
        fn = stages.STAGE_ENGINES.get(key)
        if not fn:
            raise HTTPException(404, "no such engine")
        if not body.input.strip():
            raise HTTPException(422, "the engine needs input")
        result = fn(body.input)
        title = body.title or f"{engines.REGISTRY[key]['name']} · {dep['name']}"
        return {"finding_id": save_finding(c, dep_id, key, title, result), "result": result}

    @app.post("/api/deployments/{dep_id}/engines/custom/{key}")
    def run_custom(dep_id: str, key: str, body: RunIn, c: Ctx = Depends(ctx)):
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
        payload = {"engine": key, "run_id": new_id("run"), "input": body.input,
                   "requested_by": {"id": c.uid, "name": c.user["name"], "role": c.role},
                   "deployment": {k: v for k, v in dep_out(dep, c).items() if k != "staffing_req"},
                   "workspace": c.tenant_id}
        result = plugins.call_webhook(e["url"], crypto.decrypt(cred["secret"]), payload, allow_private_engines())
        title = body.title or f"{e['name']} · {dep['name']}"
        return {"finding_id": save_finding(c, dep_id, key, title, result), "result": result}

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
        tenant = load_tenant(cred["tenant_id"])
        cfg = config.upgrade(json.loads(tenant["config_json"]))
        e = custom_engine(cfg, cred["engine_key"])
        if not e or e["kind"] != "push":
            raise HTTPException(401, "engine no longer registered")
        if not conn.execute("SELECT 1 FROM deployments WHERE id=? AND tenant_id=?",
                            (body.deployment_id, cred["tenant_id"])).fetchone():
            raise HTTPException(404, "deployment not found")
        result = plugins.normalize_result({"summary": body.summary, "status": body.status, "result": body.result})
        ectx = Ctx.__new__(Ctx)
        ectx.conn, ectx.tenant_id, ectx.cfg = conn, cred["tenant_id"], cfg
        ectx.user = {"id": "engine:" + e["key"], "role": None, "name": e["name"]}
        fid = save_finding(ectx, body.deployment_id, e["key"], body.title, result, actor="engine:" + e["key"])
        return {"finding_id": fid}

    @app.get("/api/deployments/{dep_id}/findings")
    def findings(dep_id: str, c: Ctx = Depends(ctx)):
        c.deployment(dep_id)
        vis = "" if c.can_on("finding.view_internal", dep_id) else " AND f.visibility='shared'"
        rows = conn.execute(
            "SELECT f.*, COALESCE(u.name, f.created_by) created_by_name, v.name confirmed_by_name"
            " FROM findings f LEFT JOIN users u ON u.id=f.created_by LEFT JOIN users v ON v.id=f.confirmed_by"
            f" WHERE f.deployment_id=? AND f.tenant_id=?{vis} ORDER BY f.created_at DESC",
            (dep_id, c.tenant_id)).fetchall()
        return [{**{k: r[k] for k in r.keys() if k != "result_json"}, "result": jloads(r["result_json"])}
                for r in rows]

    def finding_for(c: Ctx, fid: str):
        f = conn.execute("SELECT * FROM findings WHERE id=? AND tenant_id=?", (fid, c.tenant_id)).fetchone()
        if not f:
            raise HTTPException(404, "finding not found")
        c.deployment(f["deployment_id"])
        if f["visibility"] != "shared" and not c.can_on("finding.view_internal", f["deployment_id"]):
            raise HTTPException(404, "finding not found")
        return f

    @app.post("/api/findings/{fid}/confirm")
    def confirm(fid: str, c: Ctx = Depends(ctx)):
        f = finding_for(c, fid)
        c.require_on("finding.confirm", f["deployment_id"])
        if f["confirmed_by"]:
            raise HTTPException(409, "already confirmed")
        if f["created_by"] == c.uid:
            raise HTTPException(403, "a finding is confirmed by someone other than the person who ran it")
        with db.tx(conn):
            conn.execute("UPDATE findings SET confirmed_by=?, confirmed_at=? WHERE id=?", (c.uid, audit.now(), fid))
            c.log("finding.confirm", fid, {"engine": f["engine"], "deployment": f["deployment_id"]})
            emit(c, "finding.confirmed", f["deployment_id"], title=f["title"])
        return {"ok": True}

    class ShareIn(BaseModel):
        shared: bool

    @app.post("/api/findings/{fid}/share")
    def share_finding(fid: str, body: ShareIn, c: Ctx = Depends(ctx)):
        f = finding_for(c, fid)
        c.require_on("customer.share", f["deployment_id"])
        if body.shared and not f["confirmed_by"]:
            raise HTTPException(409, "confirm a finding before sharing it with the customer")
        vis = "shared" if body.shared else "internal"
        with db.tx(conn):
            conn.execute("UPDATE findings SET visibility=? WHERE id=?", (vis, fid))
            c.log("finding.share", fid, {"visibility": vis, "deployment": f["deployment_id"]})
            if vis == "shared":
                emit(c, "finding.shared", f["deployment_id"], title=f["title"])
        return {"visibility": vis}

    # ----------------------------------------------------------- dashboard

    @app.get("/api/dashboard")
    def dashboard(c: Ctx = Depends(ctx)):
        widgets = c.cfg["views"].get(c.role, [])
        deps = [dep_out(r, c) for r in visible_deployments(c)]
        out: dict = {"widgets": widgets}
        if "kpis" in widgets:
            out["kpis"] = {"deployments": len(deps),
                           **{h: sum(1 for d in deps if d["health"] == h) for h in ("on_track", "at_risk", "blocked")}}
        if "chain" in widgets:
            out["chain"] = deps
        if "my_tasks" in widgets:
            out["my_tasks"] = [t for t in tasks(mine=True, deployment_id=None, c=c) if t["status"] != "done"]
        if "team" in widgets and c.can("people.read"):
            out["team"] = [p for p in people(c) if p["deployments"] or p["open_tasks"]]
        if "findings" in widgets:
            ids = [d["id"] for d in deps if c.can_on("finding.view_internal", d["id"])]
            rows = conn.execute(
                "SELECT f.id, f.title, f.engine, f.deployment_id, f.created_at, d.name deployment"
                " FROM findings f JOIN deployments d ON d.id=f.deployment_id"
                f" WHERE f.tenant_id=? AND f.confirmed_by IS NULL AND f.deployment_id IN ({','.join('?' * len(ids))})"
                " ORDER BY f.created_at DESC LIMIT 20", (c.tenant_id, *ids)).fetchall() if ids else []
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
        return [{**{k: r[k] for k in r.keys() if k != "detail_json"}, "detail": jloads(r["detail_json"])}
                for r in rows]

    @app.get("/api/audit/verify")
    def audit_verify(c: Ctx = Depends(ctx)):
        c.require("audit.verify")
        return audit.verify(conn, c.tenant_id)

    # ------------------------------------------------------ launch features
    from types import SimpleNamespace
    from . import launch, mcp
    deps = SimpleNamespace(conn=conn, ctx=ctx, Ctx=Ctx, emit=emit, dep_out=dep_out,
                           visible_deployments=visible_deployments, tasks=tasks)
    launch.register(app, deps)
    ops.register(app, deps)
    mcp.register(app)
    from . import beta, connect
    connect.register(app, deps)
    beta.register(app, deps)
    from . import sow
    sow.register(app, deps)
    from . import onboarding
    onboarding.register(app, deps)

    if background:
        @app.on_event("startup")
        def _start_background():
            app.state.worker_stop = events.start_worker(conn)
            from .connect import core as connect_core
            connect_core.start_scheduler(conn, app.state.worker_stop)
            ops.start_sweeper(conn, app.state.worker_stop,
                              float(os.environ.get("FIELDWORK_SWEEP_MINUTES", "30") or 30))
            minutes = float(os.environ.get("FIELDWORK_DEMO_RESET_MINUTES", "0") or 0)
            if demo_on() and minutes > 0:
                import threading
                from .seed import reseed_demo

                def reset_loop():  # only the demo workspaces; beta workspaces on the same server are untouched
                    while not app.state.worker_stop.wait(minutes * 60):
                        with conn.lock:
                            reseed_demo(conn)

                threading.Thread(target=reset_loop, name="fieldwork-demo-reset", daemon=True).start()

    return app
