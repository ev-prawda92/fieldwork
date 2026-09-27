"""Connections: installs, tokens, the inbound event store, sync and health.

A connection is one install of one provider: a workspace's Slack, a
workspace's Salesforce org, or one person's Google Calendar. Tokens are
encrypted at rest and refreshed when they're close to expiring.

Every webhook that passes its provider's signature check is written to
inbound_events before anything else happens, keyed by the provider's own
delivery id so a retried delivery is stored once. Events are then applied; a
failure leaves the event pending for the scheduler to retry, and any event can
be replayed from the console. Webhooks are treated as a nudge and polling as
the truth: every connection with a sync is also reconciled on a schedule, so a
missed webhook costs minutes, never data.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import threading
from datetime import datetime, timedelta, timezone

from .. import audit, config, crypto
from . import http

log = logging.getLogger("fieldwork.connect")

MAX_EVENT_ATTEMPTS = 6
ACTIVE = ("active",)


def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class ConnectError(ValueError):
    """Something the person connecting needs to hear about, in plain words."""


# ----------------------------------------------------------------- providers

class Provider:
    key = ""
    name = ""
    category = ""            # chat | tracker | crm | time | calendar | email
    personal = False         # one per person (calendar, email) rather than one per workspace
    auth = "oauth"           # oauth | token
    env = ""                 # env prefix for the app's client id/secret; defaults to key
    authorize_url = ""
    token_url = ""
    scopes: tuple = ()
    scope_sep = " "
    pkce = False
    poll_minutes = 0         # 0 = webhooks only
    reconcile_hours = 24
    verified = False         # True once exercised against the live service
    blurb = ""
    setup = ""               # how to register the app, shown on the integrations page
    settings_fields: tuple = ()   # ({"key", "label", "type", "help"}, ...)

    # -- app credentials
    def _env(self) -> str:
        return (self.env or self.key).upper()

    def client_id(self) -> str:
        return os.environ.get(f"FIELDWORK_{self._env()}_CLIENT_ID", "")

    def client_secret(self) -> str:
        return os.environ.get(f"FIELDWORK_{self._env()}_CLIENT_SECRET", "")

    def env_needed(self) -> list[str]:
        if self.auth != "oauth":
            return []
        return [f"FIELDWORK_{self._env()}_CLIENT_ID", f"FIELDWORK_{self._env()}_CLIENT_SECRET"]

    def configured(self) -> bool:
        return self.auth != "oauth" or bool(self.client_id() and self.client_secret())

    # -- OAuth
    def authorize_params(self, state: str, challenge: str) -> dict:
        p = {"client_id": self.client_id(), "redirect_uri": redirect_uri(), "state": state,
             "response_type": "code"}
        if self.scopes:
            p["scope"] = self.scope_sep.join(self.scopes)
        if self.pkce:
            p.update(code_challenge=challenge, code_challenge_method="S256")
        return p

    def exchange(self, code: str, verifier: str) -> dict:
        form = {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri(),
                "client_id": self.client_id(), "client_secret": self.client_secret()}
        if self.pkce:
            form["code_verifier"] = verifier
        return http.call("POST", self.token_url, f"{self.name} sign-in", form=form)

    def refresh(self, tokens: dict) -> dict:
        if not tokens.get("refresh_token"):
            raise ConnectError(f"{self.name} didn't give a refresh token; reconnect it")
        r = http.request("POST", self.token_url, form={
            "grant_type": "refresh_token", "refresh_token": tokens["refresh_token"],
            "client_id": self.client_id(), "client_secret": self.client_secret()})
        if r.status in (400, 401):
            raise NeedsReauth(f"{self.name} refused the refresh token ({r.status}); reconnect it")
        if not r.ok:
            raise http.HTTPError(f"{self.name} token refresh failed ({r.status})", r.status)
        return {**tokens, **r.json()}

    def identify(self, rt, tokens: dict) -> dict:
        """-> {"external_account_id", "account_name", "extra": {...}}"""
        return {"external_account_id": "", "account_name": self.name, "extra": {}}

    def from_token(self, rt, token: str, account: str) -> dict:
        """Token-auth providers: validate a pasted token -> tokens dict."""
        raise ConnectError(f"{self.name} connects with its own sign-in")

    # -- lifecycle hooks (rt is a Runtime, cx a Conn)
    def after_connect(self, rt, cx) -> None:
        pass

    def before_disconnect(self, rt, cx) -> None:
        pass

    def sync(self, rt, cx, full: bool = False) -> dict:
        return {}

    def renew(self, rt, cx) -> None:
        """Renew webhooks or channels that expire. Called when webhook_json.renew_at is due."""

    # -- inbound
    def verify(self, rt, cx, headers: dict, body: bytes, query: dict) -> bool:
        return False

    def split(self, headers: dict, payload) -> list[tuple[str, str, dict]]:
        """One delivery -> [(external_id, kind, payload)]."""
        return []

    def handle(self, rt, cx, kind: str, payload: dict) -> None:
        """Apply one stored event. Raise to leave it pending for a retry."""


class NeedsReauth(ConnectError):
    pass


REGISTRY: dict[str, Provider] = {}


def register(p: Provider) -> Provider:
    REGISTRY[p.key] = p
    return p


def provider(key: str) -> Provider:
    if key not in REGISTRY:
        raise ConnectError(f"unknown integration {key!r}")
    return REGISTRY[key]


def public_url() -> str:
    return os.environ.get("FIELDWORK_PUBLIC_URL", "http://127.0.0.1:8000").rstrip("/")


def redirect_uri() -> str:
    return f"{public_url()}/oauth/callback"


def hook_url(provider_key: str, cx_id: str, key: str = "") -> str:
    return f"{public_url()}/hooks/{provider_key}/{cx_id}" + (f"?k={key}" if key else "")


# ------------------------------------------------------------------- runtime

class Runtime:
    """What a provider needs to change things in one workspace."""

    def __init__(self, conn, tenant_id: str):
        self.conn = conn
        self.tenant_id = tenant_id
        t = conn.execute("SELECT * FROM tenants WHERE id=?", (tenant_id,)).fetchone()
        self.tenant_name = t["name"]
        self.tenant_slug = t["slug"]
        self.cfg = config.upgrade(json.loads(t["config_json"]))

    def log(self, actor: str, action: str, subject: str, detail: dict | None = None) -> None:
        audit.record(self.conn, self.tenant_id, actor, action, subject, detail or {})

    def users_by_email(self) -> dict:
        return {u["email"].lower(): u for u in
                self.conn.execute("SELECT * FROM users WHERE tenant_id=?", (self.tenant_id,))}


def update_config(conn, tenant_id: str, actor: str, change) -> dict:
    """Apply change(cfg) to a workspace's settings and record it. Inside a transaction."""
    t = conn.execute("SELECT config_json FROM tenants WHERE id=?", (tenant_id,)).fetchone()
    cfg = config.upgrade(json.loads(t["config_json"]))
    change(cfg)
    conn.execute("UPDATE tenants SET config_json=? WHERE id=?", (json.dumps(cfg), tenant_id))
    audit.record(conn, tenant_id, actor, "config.update", tenant_id, {"via": "connection"})
    return cfg


class Conn:
    """A connection row with its JSON columns decoded and its tokens decrypted on demand."""

    JSON = ("extra", "cursor", "settings", "webhook")

    def __init__(self, row):
        self.row = {k: row[k] for k in row.keys()}
        self.id = self.row["id"]
        self.provider = self.row["provider"]
        self.tenant_id = self.row["tenant_id"]
        self.user_id = self.row["user_id"]
        for k in self.JSON:
            setattr(self, k, json.loads(self.row[f"{k}_json"] or "{}"))

    def __getitem__(self, k):
        return self.row[k]

    @property
    def tokens(self) -> dict:
        return json.loads(crypto.decrypt(self.row["tokens"])) if self.row["tokens"] else {}

    @property
    def p(self) -> Provider:
        return provider(self.provider)


def get(conn, cx_id: str, tenant_id: str | None = None) -> Conn | None:
    q, args = "SELECT * FROM connections WHERE id=?", [cx_id]
    if tenant_id:
        q += " AND tenant_id=?"
        args.append(tenant_id)
    r = conn.execute(q, args).fetchone()
    return Conn(r) if r else None


def active(conn, tenant_id: str, provider_key: str, user_id: str | None = None) -> Conn | None:
    q = "SELECT * FROM connections WHERE tenant_id=? AND provider=? AND status='active'"
    args = [tenant_id, provider_key]
    if user_id:
        q += " AND user_id=?"
        args.append(user_id)
    else:
        q += " AND user_id IS NULL"
    r = conn.execute(q + " ORDER BY created_at DESC", args).fetchone()
    return Conn(r) if r else None


def save(conn, cx: Conn, **cols) -> None:
    """Persist changed columns; JSON columns are passed decoded (extra=..., cursor=...)."""
    sets = {}
    for k, v in cols.items():
        if k in Conn.JSON:
            sets[f"{k}_json"] = json.dumps(v)
            setattr(cx, k, v)
        elif k == "tokens":
            sets["tokens"] = crypto.encrypt(json.dumps(v)) if v else None
        else:
            sets[k] = v
    sets["updated_at"] = iso(now())
    conn.execute(f"UPDATE connections SET {', '.join(k + '=?' for k in sets)} WHERE id=?", (*sets.values(), cx.id))
    cx.row.update({k: v for k, v in sets.items()})


def _expiry(tokens: dict) -> str | None:
    if tokens.get("expires_in"):
        try:
            return iso(now() + timedelta(seconds=int(tokens["expires_in"])))
        except (TypeError, ValueError):
            return None
    return tokens.get("expires_at")


def create(conn, rt: Runtime, p: Provider, *, user_id: str | None, created_by: str, tokens: dict,
           ident: dict) -> Conn:
    """New install, or re-auth of the same account (same provider, account and owner) in place."""
    ex = conn.execute("SELECT * FROM connections WHERE tenant_id=? AND provider=? AND external_account_id=?"
                      " AND COALESCE(user_id, '')=? AND status!='disconnected'",
                      (rt.tenant_id, p.key, ident.get("external_account_id", ""), user_id or "")).fetchone()
    ts = iso(now())
    if ex:
        cx = Conn(ex)
        save(conn, cx, tokens=tokens, token_expires_at=_expiry(tokens), status="active",
             account_name=ident.get("account_name", cx["account_name"]), extra={**cx.extra, **ident.get("extra", {})},
             last_error=None, errors=0, retry_at=None)
        return cx
    cid = "con_" + secrets.token_hex(12)
    conn.execute("INSERT INTO connections (id, tenant_id, provider, user_id, status, account_name, external_account_id,"
                 " tokens, token_expires_at, extra_json, cursor_json, settings_json, webhook_json, errors, created_by,"
                 " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (cid, rt.tenant_id, p.key, user_id, "active", ident.get("account_name", p.name)[:200],
                  str(ident.get("external_account_id", ""))[:200],
                  crypto.encrypt(json.dumps(tokens)), _expiry(tokens), json.dumps(ident.get("extra", {})), "{}",
                  json.dumps(default_settings(p)), json.dumps({"key": secrets.token_urlsafe(24)}), 0, created_by,
                  ts, ts))
    return get(conn, cid)


def default_settings(p: Provider) -> dict:
    return {f["key"]: f.get("default") for f in p.settings_fields if "default" in f}


def access_token(conn, cx: Conn, force_refresh: bool = False) -> str:
    """A live access token, refreshed first when it expires within two minutes (or when asked to)."""
    tokens = cx.tokens
    exp = parse(cx["token_expires_at"])
    if (force_refresh or (exp and exp <= now() + timedelta(minutes=2))) and tokens.get("refresh_token"):
        try:
            tokens = cx.p.refresh(tokens)
        except NeedsReauth as e:
            with conn.tx():
                save(conn, cx, status="needs_reauth", last_error=str(e)[:500])
            raise
        with conn.tx():
            save(conn, cx, tokens=tokens, token_expires_at=_expiry(tokens))
    tok = tokens.get("access_token") or tokens.get("api_token")
    if not tok:
        raise ConnectError(f"{cx.p.name} has no token; reconnect it")
    return tok


def authed(conn, cx: Conn, method: str, url: str, what: str, **kw):
    """An API call with the connection's token; on a 401 the token is refreshed once and the call retried
    (some services, like Salesforce, don't say when a token expires)."""
    r = http.request(method, url, bearer=access_token(conn, cx), **kw)
    if r.status == 401 and cx.tokens.get("refresh_token"):
        r = http.request(method, url, bearer=access_token(conn, cx, force_refresh=True), **kw)
    if not r.ok:
        raise http.HTTPError(f"{what} failed ({r.status}): {str(r.json())[:300]}", r.status, r.json())
    return r.json()


# -------------------------------------------------------------- inbound store

def store_event(conn, cx: Conn, external_id: str, kind: str, payload) -> int | None:
    """Record a verified delivery. Returns the new row id, or None if we already have it."""
    ext = (external_id or secrets.token_hex(8))[:200]
    if conn.execute("SELECT 1 FROM inbound_events WHERE connection_id=? AND external_id=?", (cx.id, ext)).fetchone():
        return None
    with conn.tx():
        conn.execute("INSERT INTO inbound_events (tenant_id, connection_id, provider, external_id, kind, payload_json,"
                     " status, attempts, received_at) VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING",
                     (cx.tenant_id, cx.id, cx.provider, ext, kind[:80], json.dumps(payload)[:1_000_000], "pending", 0,
                      iso(now())))
        conn.execute("UPDATE connections SET last_event_at=? WHERE id=?", (iso(now()), cx.id))
    r = conn.execute("SELECT id FROM inbound_events WHERE connection_id=? AND external_id=?", (cx.id, ext)).fetchone()
    return r["id"] if r else None


def apply_event(conn, event_id: int) -> str:
    """Apply one stored event. Returns its new status."""
    ev = conn.execute("SELECT * FROM inbound_events WHERE id=?", (event_id,)).fetchone()
    if not ev:
        return "missing"
    cx = get(conn, ev["connection_id"])
    if not cx or cx["status"] != "active":
        with conn.tx():
            conn.execute("UPDATE inbound_events SET status='skipped', error=?, processed_at=? WHERE id=?",
                         ("connection isn't active", iso(now()), event_id))
        return "skipped"
    rt = Runtime(conn, cx.tenant_id)
    try:
        cx.p.handle(rt, cx, ev["kind"], json.loads(ev["payload_json"]))
    except Exception as e:  # kept for retry and shown on the connection's health
        attempts = ev["attempts"] + 1
        status = "failed" if attempts >= MAX_EVENT_ATTEMPTS else "pending"
        with conn.tx():
            conn.execute("UPDATE inbound_events SET status=?, attempts=?, error=? WHERE id=?",
                         (status, attempts, str(e)[:500], event_id))
            conn.execute("UPDATE connections SET last_error=? WHERE id=?", (f"event: {str(e)[:400]}", cx.id))
        log.warning("inbound event %s (%s) failed: %s", event_id, cx.provider, e)
        return status
    with conn.tx():
        conn.execute("UPDATE inbound_events SET status='done', attempts=attempts+1, error=NULL, processed_at=?"
                     " WHERE id=?", (iso(now()), event_id))
    return "done"


def process_pending(conn, limit: int = 100) -> int:
    rows = conn.execute("SELECT id, attempts, received_at FROM inbound_events WHERE status='pending'"
                        " ORDER BY id LIMIT ?", (limit,)).fetchall()
    n = 0
    for r in rows:
        # back off: attempt k waits 30s * 2^k after the event arrived
        due = parse(r["received_at"]) + timedelta(seconds=30 * (2 ** r["attempts"]) if r["attempts"] else 0)
        if due <= now():
            apply_event(conn, r["id"])
            n += 1
    return n


# ---------------------------------------------------------------------- sync

def run_sync(conn, cx: Conn, full: bool = False) -> dict:
    """Sync one connection now, recording success or failure on the connection."""
    rt = Runtime(conn, cx.tenant_id)
    try:
        out = cx.p.sync(rt, cx, full=full) or {}
    except Exception as e:
        errors = (cx["errors"] or 0) + 1
        wait = min(360, 2 ** min(errors, 9))
        with conn.tx():
            fields = dict(errors=errors, last_error=str(e)[:500], retry_at=iso(now() + timedelta(minutes=wait)))
            if isinstance(e, NeedsReauth):
                fields["status"] = "needs_reauth"
            save(conn, cx, **fields)
        log.warning("sync %s (%s) failed: %s", cx.id, cx.provider, e)
        raise
    with conn.tx():
        fields = dict(last_sync_at=iso(now()), errors=0, last_error=None, retry_at=None)
        if full:
            fields["last_reconcile_at"] = iso(now())
        save(conn, cx, **fields)
    return out


def due_work(conn, at: datetime | None = None) -> list[tuple[Conn, str]]:
    at = at or now()
    work = []
    for r in conn.execute("SELECT * FROM connections WHERE status='active'").fetchall():
        cx = Conn(r)
        try:
            p = cx.p
        except ConnectError:
            continue
        retry = parse(cx["retry_at"])
        if retry and retry > at:
            continue
        renew_at = parse(cx.webhook.get("renew_at"))
        if renew_at and renew_at <= at:
            work.append((cx, "renew"))
        last_rec = parse(cx["last_reconcile_at"])
        last_sync = parse(cx["last_sync_at"])
        if p.reconcile_hours and type(p).sync is not Provider.sync and (
                not last_rec or last_rec <= at - timedelta(hours=p.reconcile_hours)):
            work.append((cx, "reconcile"))
        elif p.poll_minutes and (not last_sync or last_sync <= at - timedelta(minutes=p.poll_minutes)):
            work.append((cx, "poll"))
    return work


def run_due(conn, at: datetime | None = None) -> dict:
    """One scheduler tick: apply pending events, poll, reconcile and renew what's due."""
    done = {"events": process_pending(conn), "poll": 0, "reconcile": 0, "renew": 0, "failed": 0}
    for cx, what in due_work(conn, at):
        try:
            if what == "renew":
                cx.p.renew(Runtime(conn, cx.tenant_id), cx)
            else:
                run_sync(conn, cx, full=(what == "reconcile"))
            done[what] += 1
        except Exception as e:
            done["failed"] += 1
            if what == "renew":
                with conn.tx():
                    save(conn, get(conn, cx.id), last_error=f"renew: {str(e)[:400]}",
                         retry_at=iso(now() + timedelta(minutes=15)))
    cutoff = iso(now() - timedelta(hours=1))
    with conn.tx():
        conn.execute("DELETE FROM oauth_states WHERE created_at<?", (cutoff,))
        conn.execute("DELETE FROM inbound_events WHERE status IN ('done','skipped') AND received_at<?",
                     (iso(now() - timedelta(days=30)),))
    return done


_kick = threading.Event()


def kick() -> None:
    """Wake the scheduler now (a webhook nudged a connection)."""
    _kick.set()


def start_scheduler(conn, stop: threading.Event, seconds: float = 30.0) -> None:
    from .. import events
    digest_hour = os.environ.get("FIELDWORK_DIGEST_HOUR_UTC", "")
    state = {"digest_day": None}

    def loop():
        while not stop.is_set():
            try:
                run_due(conn)
                if digest_hour.isdigit():
                    t = now()
                    if t.hour == int(digest_hour) and state["digest_day"] != t.date():
                        state["digest_day"] = t.date()
                        events.queue_digests(conn)
            except Exception as e:
                log.warning("connection scheduler error: %s", e)
            _kick.wait(seconds)
            _kick.clear()
    threading.Thread(target=loop, name="fieldwork-connect", daemon=True).start()


# -------------------------------------------------------------------- health

def health(conn, cx: Conn) -> dict:
    counts = {r["status"]: r["n"] for r in conn.execute(
        "SELECT status, COUNT(*) n FROM inbound_events WHERE connection_id=? GROUP BY status", (cx.id,))}
    last = max([d for d in (parse(cx["last_event_at"]), parse(cx["last_sync_at"])) if d], default=None)
    p = cx.p
    expected = p.poll_minutes or (p.reconcile_hours * 60 if p.reconcile_hours else 0)
    lag = round((now() - last).total_seconds() / 60, 1) if last else None
    if cx["status"] != "active":
        state = cx["status"]
    elif cx["errors"] or counts.get("failed"):
        state = "erroring"
    elif expected and (lag is None or lag > expected * 2 + 30) and type(p).sync is not Provider.sync:
        state = "stale"
    else:
        state = "healthy"
    return {"state": state, "last_event_at": cx["last_event_at"], "last_sync_at": cx["last_sync_at"],
            "last_reconcile_at": cx["last_reconcile_at"], "minutes_since_activity": lag,
            "errors": cx["errors"], "last_error": cx["last_error"], "retry_at": cx["retry_at"],
            "events": {"pending": counts.get("pending", 0), "failed": counts.get("failed", 0),
                       "done": counts.get("done", 0)}}


def out(conn, cx: Conn, detail: bool = False) -> dict:
    p = cx.p
    x = {"id": cx.id, "provider": cx.provider, "name": p.name, "personal": bool(cx.user_id),
         "user_id": cx.user_id, "status": cx["status"], "account_name": cx["account_name"],
         "created_at": cx["created_at"], "health": health(conn, cx)}
    if detail:
        x["settings"] = cx.settings
        x["settings_fields"] = list(p.settings_fields)
        x["webhook"] = {k: v for k, v in cx.webhook.items() if k not in ("key", "secret", "secrets")}
        x["cursor"] = cx.cursor
        x["extra"] = {k: v for k, v in cx.extra.items() if not k.startswith("_")}
    return x
