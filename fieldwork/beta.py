"""The open beta: anyone can start a workspace, and people sign in with GitHub or Google.

  Start a workspace   name it, sign in with GitHub or Google, and you're its Head of Deployments
                      with the default template, ready to make your own.
  Sign in             GitHub or Google, matched to your account by verified email. People an admin
                      adds under Team sign in the same way with the email they were added with.
  Feedback            a button on every screen; it lands with whoever runs the service.

Sign-in reuses the connection apps (FIELDWORK_GITHUB_CLIENT_ID/SECRET, FIELDWORK_GOOGLE_CLIENT_ID/SECRET)
with sign-in-only scopes, and the same /oauth/callback, so one app registration covers both.

Switches:
  FIELDWORK_OPEN_SIGNUP=1             let anyone start a workspace (off by default for self-hosted installs)
  FIELDWORK_BETA_MAX_WORKSPACES=200   stop taking new workspaces past this many
  FIELDWORK_FEEDBACK_WEBHOOK=https://hooks.slack.com/...   also post feedback to a channel you watch

The demo workspaces are never matched at sign-in, and connections are switched off in them, so a
shared demo login can't reach anyone's real accounts.
"""

import hashlib
import json
import os
import re
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

from fastapi import Depends, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from . import audit, config, db
from .connect import core, http
from .connect.calendars import GoogleOAuth
from .connect.trackers import GitHubApp

DEMO_TENANTS = ("ten_meridian", "ten_orbital")
SESSION_DAYS = 14
LOGIN_SCOPES = {"github": ("read:user", "user:email"), "google": ("openid", "email", "profile")}
_signups: dict = {}
_lock = threading.Lock()


def open_signup() -> bool:
    return os.environ.get("FIELDWORK_OPEN_SIGNUP") == "1"


def max_workspaces() -> int:
    try:
        return int(os.environ.get("FIELDWORK_BETA_MAX_WORKSPACES", "200"))
    except ValueError:
        return 200


def demo_on() -> bool:
    return os.environ.get("FIELDWORK_DEMO") == "1"


def is_demo_tenant(tenant_id: str) -> bool:
    return demo_on() and tenant_id in DEMO_TENANTS


def providers() -> dict:
    return {"github": GitHubApp(), "google": GoogleOAuth()}


def configured() -> list[str]:
    return [k for k, p in providers().items() if p.client_id() and p.client_secret()]


def now() -> datetime:
    return datetime.now(timezone.utc)


def _cookie(state: str) -> str:
    return hashlib.sha256(("fw-oauth:" + state).encode()).hexdigest()


def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40] or "team"
    return s if s[0].isalpha() else "w-" + s


def unique_slug(conn, name: str) -> str:
    base = slugify(name)
    slug, n = base, 1
    while slug in ("meridian", "orbital", "demo", "admin", "api") or \
            conn.execute("SELECT 1 FROM tenants WHERE slug=?", (slug,)).fetchone():
        n += 1
        slug = f"{base}-{n}"
    return slug


def _rate_ok(ip: str, per_hour: int = 5) -> bool:
    with _lock:
        t = time.time()
        hits = [x for x in _signups.get(ip, []) if x > t - 3600]
        if len(hits) >= per_hour:
            _signups[ip] = hits
            return False
        _signups[ip] = hits + [t]
        return True


# ------------------------------------------------------------ identity

def identity(key: str, code: str, verifier: str) -> dict:
    """-> {"email", "name", "verified"} from the provider, using sign-in scopes only."""
    p = providers()[key]
    tokens = p.exchange(code, verifier)
    tok = tokens["access_token"]
    if key == "github":
        me = http.call("GET", "https://api.github.com/user", "GitHub profile", bearer=tok)
        emails = http.call("GET", "https://api.github.com/user/emails", "GitHub email", bearer=tok)
        primary = next((e for e in emails if e.get("primary") and e.get("verified")), None) or \
            next((e for e in emails if e.get("verified")), None)
        if not primary:
            raise core.ConnectError("Your GitHub account has no verified email address")
        return {"email": primary["email"].lower(), "name": me.get("name") or me.get("login") or "", "verified": True}
    me = http.call("GET", "https://openidconnect.googleapis.com/v1/userinfo", "Google profile", bearer=tok)
    if not me.get("email") or not me.get("email_verified"):
        raise core.ConnectError("Your Google account's email isn't verified")
    return {"email": me["email"].lower(), "name": me.get("name") or "", "verified": True}


def issue_session(conn, user) -> str:
    from .app import token_hash
    tok = "fwsess_" + secrets.token_urlsafe(32)
    conn.execute("INSERT INTO sessions (token_hash, user_id, tenant_id, method, created_at, expires_at)"
                 " VALUES (?,?,?,?,?,?)", (token_hash(tok), user["id"], user["tenant_id"], "login",
                                           now().isoformat(), (now() + timedelta(days=SESSION_DAYS)).isoformat()))
    return tok


def create_workspace(conn, name: str, email: str, person: str) -> tuple:
    """A new workspace on the default template, with its founder as Head of Deployments. In a transaction."""
    from .app import token_hash
    tid = "ten_" + secrets.token_hex(6)
    uid = "usr_" + secrets.token_hex(6)
    ts = audit.now()
    cfg = config.default()
    conn.execute("INSERT INTO tenants (id, name, config_json, created_at, slug, created_via) VALUES (?,?,?,?,?,?)",
                 (tid, name, json.dumps(cfg), ts, unique_slug(conn, name), "signup"))
    conn.execute("INSERT INTO users (id, tenant_id, name, email, role, manager_id, token_hash, profile_json, created_at)"
                 " VALUES (?,?,?,?,?,?,?,?,?)", (uid, tid, person or email.split("@")[0], email, "head", None,
                                                token_hash("disabled:" + secrets.token_hex(16)), "{}", ts))
    audit.record(conn, tid, uid, "workspace.create", tid, {"name": name, "via": "signup"})
    return tid, conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()


def finish_login(conn, row, code: str, error: str) -> RedirectResponse:
    """Called from /oauth/callback for a sign-in state (provider 'login:<key>')."""
    def back(**q):
        return RedirectResponse("/?" + urlencode(q), status_code=303)
    key = row["provider"].split(":", 1)[1]
    meta = json.loads(row["meta"] or "{}")
    if error or not code:
        return back(login_error=f"Sign-in was cancelled ({error or 'no code'})")
    try:
        who = identity(key, code, row["verifier"])
    except (core.ConnectError, http.HTTPError) as e:
        return back(login_error=str(e)[:200])
    except Exception:
        return back(login_error="Sign-in didn't go through; try again")
    demo = DEMO_TENANTS if demo_on() else ()
    ph = ",".join("?" * len(demo)) or "''"
    users = conn.execute(f"SELECT u.* FROM users u JOIN tenants t ON t.id=u.tenant_id WHERE lower(u.email)=? AND u.active=1"
                         f" AND u.tenant_id NOT IN ({ph}) ORDER BY t.created_at", (who["email"], *demo)).fetchall()
    with db.tx(conn):
        if users:
            user = users[0]
            if meta.get("mode") == "signup":
                note = "You already have a workspace, so you're signed in to it."
            else:
                note = ""
        elif meta.get("mode") == "signup":
            if not open_signup():
                return back(login_error="New workspaces aren't open right now")
            if conn.execute("SELECT COUNT(*) n FROM tenants WHERE created_via='signup'").fetchone()["n"] >= max_workspaces():
                return back(login_error="The beta is full for now. We'll open more room soon.")
            _, user = create_workspace(conn, (meta.get("workspace") or "My team")[:80], who["email"], who["name"])
            note = "welcome"
        else:
            return back(login_error=f"No workspace has {who['email']} yet. Start one, or ask your team to add you.")
        tok = issue_session(conn, user)
        audit.record(conn, user["tenant_id"], user["id"], "auth.login", user["id"], {"provider": key})
    frag = {"session": tok}
    if note:
        frag["note"] = note
    return RedirectResponse("/#" + urlencode(frag), status_code=302)


# -------------------------------------------------------------- routes

def register(app, d) -> None:
    conn, ctx, Ctx = d.conn, d.ctx, d.Ctx

    @app.get("/api/beta")
    def beta_info():
        return {"signup": open_signup(), "providers": configured(), "demo": demo_on()}

    class StartIn(BaseModel):
        provider: str
        mode: str = "signin"
        workspace: str = Field(default="", max_length=80)

    @app.post("/api/beta/start")
    def start(body: StartIn, request: Request, response: Response):
        if body.provider not in configured():
            raise HTTPException(409, f"Sign-in with {body.provider.title()} isn't set up on this server")
        if body.mode not in ("signin", "signup"):
            raise HTTPException(422, "mode is signin or signup")
        if body.mode == "signup":
            if not open_signup():
                raise HTTPException(403, "New workspaces aren't open right now")
            if not body.workspace.strip():
                raise HTTPException(422, "Name your workspace (your team or company)")
            ip = (request.headers.get("x-forwarded-for") or (request.client.host if request.client else "")).split(",")[0]
            if not _rate_ok(ip.strip()):
                raise HTTPException(429, "That's a lot of new workspaces from one place; try again in an hour")
        p = providers()[body.provider]
        state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
        with db.tx(conn):
            conn.execute("INSERT INTO oauth_states (state, tenant_id, user_id, provider, personal, verifier, created_at,"
                         " meta) VALUES (?,?,?,?,?,?,?,?)",
                         (state, "-", "-", "login:" + body.provider, 0, verifier, core.iso(core.now()),
                          json.dumps({"mode": body.mode, "workspace": body.workspace.strip()})))
        response.set_cookie("fw_oauth", _cookie(state), max_age=900, httponly=True, samesite="lax",
                            secure=core.public_url().startswith("https"), path="/oauth")
        params = p.authorize_params(state, _challenge(verifier))
        params["scope"] = " ".join(LOGIN_SCOPES[body.provider])
        if body.provider == "google":
            params.pop("access_type", None)
            params["prompt"] = "select_account"
        return {"url": p.authorize_url + "?" + urlencode(params)}

    class FeedbackIn(BaseModel):
        text: str = Field(min_length=1, max_length=4000)
        page: str = Field(default="", max_length=200)

    @app.post("/api/feedback", status_code=201)
    def feedback(body: FeedbackIn, c: Ctx = Depends(ctx)):
        with db.tx(conn):
            conn.execute("INSERT INTO feedback (tenant_id, user_id, page, text, created_at) VALUES (?,?,?,?,?)",
                         (c.tenant_id, c.uid, body.page, body.text.strip(), audit.now()))
        hook = os.environ.get("FIELDWORK_FEEDBACK_WEBHOOK", "")
        if hook:
            text = f":speech_balloon: *{c.user['name']}* ({c.tenant_name}, {c.role}) on `{body.page or '/'}`:\n{body.text.strip()}"
            threading.Thread(target=_post_quietly, args=(hook, {"text": text[:3500]}), daemon=True).start()
        return {"ok": True}

    @app.get("/api/operator/feedback", include_in_schema=False)
    def operator_feedback(request: Request):
        want = os.environ.get("FIELDWORK_OPERATOR_TOKEN", "")
        got = request.headers.get("x-operator-token", "")
        if not want or not secrets.compare_digest(want, got):
            raise HTTPException(404, "not found")
        rows = conn.execute("SELECT f.*, u.name, u.email, t.name workspace FROM feedback f JOIN users u ON u.id=f.user_id"
                            " JOIN tenants t ON t.id=f.tenant_id ORDER BY f.id DESC LIMIT 200").fetchall()
        return [dict(r) for r in rows]


def _post_quietly(url: str, body: dict) -> None:
    try:
        http.request("POST", url, json_body=body, timeout=5)
    except Exception:
        pass


def _challenge(verifier: str) -> str:
    import base64
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
