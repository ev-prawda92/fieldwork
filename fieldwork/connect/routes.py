"""HTTP surface for connections: the catalog, one-click installs, health, replay,
inbound webhooks, and the live-update stream the console listens to."""


import asyncio
import base64
import hashlib
import json
import secrets
from datetime import timedelta
from urllib.parse import urlencode

from fastapi import Depends, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse
from pydantic import BaseModel, Field

from .. import db
from . import core, http
from .core import REGISTRY, ConnectError, Runtime


def _challenge(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


def _cookie(state: str) -> str:
    return hashlib.sha256(("fw-oauth:" + state).encode()).hexdigest()


def register(app, d) -> None:
    conn, ctx, Ctx = d.conn, d.ctx, d.Ctx

    # provider-specific routes first, so /hooks/slack/interact isn't read as /hooks/{provider}/{connection}
    for prov in REGISTRY.values():
        if hasattr(prov, "routes"):
            prov.routes(app, d)

    def internal(c) -> bool:
        return c.can("task.view_internal")

    def may_manage(c, p) -> bool:
        return internal(c) if p.personal else c.can("integrations.manage")

    def the_provider(key: str):
        if key not in REGISTRY:
            raise HTTPException(404, "no such integration")
        return REGISTRY[key]

    def the_connection(c, cid: str) -> core.Conn:
        cx = core.get(conn, cid, c.tenant_id)
        if not cx or cx["status"] == "disconnected":
            raise HTTPException(404, "connection not found")
        if cx.user_id:
            if cx.user_id != c.uid and not c.can("integrations.manage"):
                raise HTTPException(404, "connection not found")
        elif not c.can("integrations.manage"):
            raise HTTPException(404, "connection not found")
        return cx

    # ---------------------------------------------------------------- catalog

    @app.get("/api/connections")
    def catalog(c: Ctx = Depends(ctx)):
        manage = c.can("integrations.manage")
        items = []
        for p in REGISTRY.values():
            if not (manage or (p.personal and internal(c))):
                continue
            if p.personal:
                rows = conn.execute("SELECT * FROM connections WHERE tenant_id=? AND provider=? AND user_id=?"
                                    " AND status!='disconnected' ORDER BY created_at", (c.tenant_id, p.key, c.uid))
                people = conn.execute("SELECT COUNT(DISTINCT user_id) n FROM connections WHERE tenant_id=? AND"
                                      " provider=? AND status='active'", (c.tenant_id, p.key)).fetchone()["n"]
            else:
                rows = conn.execute("SELECT * FROM connections WHERE tenant_id=? AND provider=? AND user_id IS NULL"
                                    " AND status!='disconnected' ORDER BY created_at", (c.tenant_id, p.key))
                people = None
            items.append({
                "key": p.key, "name": p.name, "category": p.category, "personal": p.personal, "auth": p.auth,
                "configured": p.configured(), "verified": p.verified, "blurb": p.blurb, "setup": p.setup,
                "env_needed": p.env_needed() if manage else [], "you_can_connect": may_manage(c, p),
                "people_connected": people if manage else None,
                "connections": [core.out(conn, core.Conn(r)) for r in rows.fetchall()]})
        return {"providers": items, "redirect_uri": core.redirect_uri(), "public_url": core.public_url()}

    # ----------------------------------------------------------------- OAuth

    @app.post("/api/connections/{key}/start")
    def start(key: str, response: Response, c: Ctx = Depends(ctx)):
        p = the_provider(key)
        if not may_manage(c, p):
            raise HTTPException(403, "your role can't connect this" if not p.personal
                                else "personal connections are for the delivery team")
        if p.auth != "oauth":
            raise HTTPException(409, f"{p.name} connects with a token")
        if not p.configured():
            raise HTTPException(409, f"{p.name} needs app setup first: set {' and '.join(p.env_needed())}")
        state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
        with db.tx(conn):
            conn.execute("INSERT INTO oauth_states (state, tenant_id, user_id, provider, personal, verifier, created_at)"
                         " VALUES (?,?,?,?,?,?,?)", (state, c.tenant_id, c.uid, p.key, int(p.personal), verifier,
                                                    core.iso(core.now())))
        response.set_cookie("fw_oauth", _cookie(state), max_age=900, httponly=True, samesite="lax",
                            secure=core.public_url().startswith("https"), path="/oauth")
        return {"url": p.authorize_url + "?" + urlencode(p.authorize_params(state, _challenge(verifier)))}

    def back(personal: bool, **q) -> RedirectResponse:
        return RedirectResponse(f"/?{urlencode(q)}#/{'work' if personal else 'integrations'}", status_code=303)

    @app.get("/oauth/callback", include_in_schema=False)
    def callback(request: Request, state: str = "", code: str = "", error: str = ""):
        row = conn.execute("SELECT * FROM oauth_states WHERE state=?", (state,)).fetchone() if state else None
        if row:
            with db.tx(conn):
                conn.execute("DELETE FROM oauth_states WHERE state=?", (state,))
        if not row or core.parse(row["created_at"]) < core.now() - timedelta(minutes=15):
            return back(False, oauth_error="That sign-in link expired. Start again from Integrations.")
        personal = bool(row["personal"])
        if not secrets.compare_digest(request.cookies.get("fw_oauth", ""), _cookie(state)):
            return back(personal, oauth_error="Finish connecting in the same browser you started in.")
        p = REGISTRY.get(row["provider"])
        user = conn.execute("SELECT * FROM users WHERE id=? AND tenant_id=?", (row["user_id"], row["tenant_id"])).fetchone()
        if not p or not user:
            return back(personal, oauth_error="That connection can't be finished.")
        if error or not code:
            return back(personal, oauth_error=f"{p.name} said: {error or 'no code returned'}")
        rt = Runtime(conn, row["tenant_id"])
        try:
            tokens = p.exchange(code, row["verifier"])
            ident = p.identify(rt, tokens)
        except (ConnectError, http.HTTPError) as e:
            return back(personal, oauth_error=str(e)[:300])
        except Exception as e:  # never a raw error page mid-install
            core.log.warning("oauth callback for %s failed: %r", p.key, e)
            return back(personal, oauth_error=f"{p.name} answered in a way we didn't expect; try again")
        with db.tx(conn):
            cx = core.create(conn, rt, p, user_id=user["id"] if personal else None, created_by=user["id"],
                             tokens=tokens, ident=ident)
            rt.log(user["id"], "connection.create", cx.id, {"provider": p.key, "account": cx["account_name"],
                                                          "personal": personal})
        try:
            p.after_connect(rt, core.get(conn, cx.id))
        except Exception as e:  # connected, but setup didn't finish; health shows it
            with db.tx(conn):
                core.save(conn, core.get(conn, cx.id), last_error=f"setup: {str(e)[:400]}")
        return back(personal, connected=p.key)

    class TokenIn(BaseModel):
        token: str = Field(min_length=4, max_length=500)
        account: str = Field(default="", max_length=200)

    @app.post("/api/connections/{key}/token", status_code=201)
    def connect_token(key: str, body: TokenIn, c: Ctx = Depends(ctx)):
        p = the_provider(key)
        if p.auth != "token":
            raise HTTPException(409, f"{p.name} connects with its own sign-in")
        if not may_manage(c, p):
            raise HTTPException(403, "your role can't connect this")
        rt = Runtime(conn, c.tenant_id)
        tokens = p.from_token(rt, body.token.strip(), body.account.strip())
        ident = p.identify(rt, tokens)
        with db.tx(conn):
            cx = core.create(conn, rt, p, user_id=c.uid if p.personal else None, created_by=c.uid,
                             tokens=tokens, ident=ident)
            c.log("connection.create", cx.id, {"provider": p.key, "account": cx["account_name"]})
        try:
            p.after_connect(rt, core.get(conn, cx.id))
        except Exception as e:
            with db.tx(conn):
                core.save(conn, core.get(conn, cx.id), last_error=f"setup: {str(e)[:400]}")
        return core.out(conn, core.get(conn, cx.id))

    # ------------------------------------------------------------- managing

    @app.get("/api/connections/{cid}")
    def detail(cid: str, c: Ctx = Depends(ctx)):
        cx = the_connection(c, cid)
        x = core.out(conn, cx, detail=True)
        x["hooks"] = {k: v for k, v in (getattr(cx.p, "hook_urls", lambda cx: {})(cx) or {}).items()}
        x["options"] = cx.p.options(Runtime(conn, c.tenant_id), cx) if hasattr(cx.p, "options") else {}
        return x

    class SettingsIn(BaseModel):
        settings: dict

    @app.patch("/api/connections/{cid}/settings")
    def set_settings(cid: str, body: SettingsIn, c: Ctx = Depends(ctx)):
        cx = the_connection(c, cid)
        allowed = {f["key"]: f for f in cx.p.settings_fields}
        bad = [k for k in body.settings if k not in allowed]
        if bad:
            raise HTTPException(422, f"unknown setting(s): {', '.join(bad)}")
        new = dict(cx.settings)
        for k, v in body.settings.items():
            t = allowed[k].get("type", "text")
            if t == "bool":
                v = bool(v)
            elif t == "number":
                try:
                    v = float(v) if v not in (None, "") else None
                except (TypeError, ValueError):
                    raise HTTPException(422, f"{allowed[k]['label']} must be a number")
            elif t == "map":
                if not isinstance(v, dict) or len(v) > 2000:
                    raise HTTPException(422, f"{allowed[k]['label']} must be a mapping")
                v = {str(a)[:200]: (str(b)[:200] if b is not None else None) for a, b in v.items()}
            else:
                v = str(v or "")[:500]
            new[k] = v
        if hasattr(cx.p, "check_settings"):
            cx.p.check_settings(Runtime(conn, c.tenant_id), cx, new)
        with db.tx(conn):
            core.save(conn, cx, settings=new)
            c.log("connection.settings", cid, {"provider": cx.provider, "changed": sorted(body.settings)})
        return {"settings": new}

    class SyncIn(BaseModel):
        full: bool = False

    @app.post("/api/connections/{cid}/sync")
    def sync_now(cid: str, body: SyncIn, c: Ctx = Depends(ctx)):
        cx = the_connection(c, cid)
        if cx["status"] != "active":
            raise HTTPException(409, f"this connection is {cx['status'].replace('_', ' ')}; reconnect it")
        try:
            res = core.run_sync(conn, cx, full=body.full)
        except (ConnectError, http.HTTPError) as e:
            raise HTTPException(502, str(e))
        return {"result": res, "health": core.health(conn, core.get(conn, cid))}

    @app.delete("/api/connections/{cid}")
    def disconnect(cid: str, c: Ctx = Depends(ctx)):
        cx = the_connection(c, cid)
        err = None
        try:
            cx.p.before_disconnect(Runtime(conn, c.tenant_id), cx)
        except Exception as e:  # removing their webhook failed; we still forget the tokens
            err = str(e)[:300]
        with db.tx(conn):
            core.save(conn, cx, status="disconnected", tokens=None)
            c.log("connection.remove", cid, {"provider": cx.provider, "cleanup_error": err})
        return {"ok": True, "cleanup_error": err}

    @app.get("/api/connections/{cid}/events")
    def events_list(cid: str, status: str | None = None, c: Ctx = Depends(ctx)):
        cx = the_connection(c, cid)
        q, args = "SELECT id, external_id, kind, status, error, attempts, received_at, processed_at FROM inbound_events" \
                  " WHERE connection_id=?", [cx.id]
        if status:
            q += " AND status=?"
            args.append(status)
        return [dict(r) for r in conn.execute(q + " ORDER BY id DESC LIMIT 50", args)]

    @app.post("/api/connections/{cid}/events/{eid}/replay")
    def replay(cid: str, eid: int, c: Ctx = Depends(ctx)):
        cx = the_connection(c, cid)
        ev = conn.execute("SELECT * FROM inbound_events WHERE id=? AND connection_id=?", (eid, cx.id)).fetchone()
        if not ev:
            raise HTTPException(404, "event not found")
        with db.tx(conn):
            conn.execute("UPDATE inbound_events SET status='pending', attempts=0, error=NULL, next_at=NULL"
                         " WHERE id=?", (eid,))
            c.log("connection.replay", cid, {"event": eid, "kind": ev["kind"]})
        return {"status": core.apply_event(conn, eid)}

    # ------------------------------------------------------ inbound webhooks

    @app.api_route("/hooks/{key}/{cid}", methods=["POST", "GET"], include_in_schema=False)
    async def inbound(key: str, cid: str, request: Request):
        body = await request.body()
        headers = {k.lower(): v for k, v in request.headers.items()}
        return await asyncio.to_thread(receive, key, cid, body, headers, dict(request.query_params))

    def receive(key: str, cid: str, body: bytes, headers: dict, query: dict):
        p = REGISTRY.get(key)
        cx = core.get(conn, cid)
        if not p or not cx or cx.provider != key or cx["status"] != "active":
            raise HTTPException(404, "not found")
        rt = Runtime(conn, cx.tenant_id)
        if hasattr(p, "handshake"):  # e.g. Microsoft Graph's validationToken echo
            hs = p.handshake(rt, cx, headers, body, query)
            if hs is not None:
                return hs
        if not p.verify(rt, cx, headers, body, query):
            raise HTTPException(401, "bad signature")
        try:
            payload = json.loads(body) if body else {}
        except ValueError:
            raise HTTPException(400, "not JSON")
        stored = [e for e in (core.store_event(conn, cx, ext, kind, item) for ext, kind, item in p.split(headers, payload))
                  if e]
        if getattr(p, "nudge", False):
            core.kick()
            return JSONResponse({"stored": len(stored)}, status_code=202)
        return {"stored": len(stored), "results": [core.apply_event(conn, e) for e in stored]}

    # ---------------------------------------------------------- live updates

    @app.get("/api/stream")
    async def stream(request: Request, c: Ctx = Depends(ctx), max_events: int = 0, poll: float = 2.0,
                     authorization: str = Header(default="")):
        """Server-sent events: `change` whenever anything in the workspace is recorded.
        The console refreshes what it's showing; nothing about the change itself is sent."""
        tid = c.tenant_id
        poll = min(max(poll, 0.25), 10.0)

        def still_signed_in() -> bool:
            try:
                ctx(authorization)
                return True
            except HTTPException:
                return False

        def seq() -> int:
            r = conn.execute("SELECT MAX(seq) s FROM audit WHERE tenant_id=?", (tid,)).fetchone()
            return int(r["s"] or 0)

        async def gen():
            last = await asyncio.to_thread(seq)
            yield f"event: hello\ndata: {json.dumps({'seq': last})}\n\n"
            sent, idle = 0, 0.0
            while True:
                if await request.is_disconnected():
                    break
                await asyncio.sleep(poll)
                cur = await asyncio.to_thread(seq)
                if cur != last:
                    last = cur
                    idle = 0.0
                    yield f"event: change\ndata: {json.dumps({'seq': cur})}\n\n"
                    sent += 1
                    if max_events and sent >= max_events:
                        break
                else:
                    idle += poll
                    if idle >= 15:
                        idle = 0.0
                        if not await asyncio.to_thread(still_signed_in):  # signed out or expired: stop
                            break
                        yield ": keep-alive\n\n"
        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

