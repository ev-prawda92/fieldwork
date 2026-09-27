"""Time off, from people's own calendars (Google Calendar, Microsoft 365).

Each person connects their own calendar. Fieldwork reads only whether they're
out: Google "Out of office" events, Outlook events shown as "Away" (oof), and
all-day events titled like time off (OOO, PTO, vacation, holiday, leave). It
never stores other events. Capacity then plans around the days people are
actually out, and changes arrive as they happen (Google push channels,
Microsoft Graph subscriptions), with an hourly poll behind them.
"""


import hashlib
import re
import secrets
from datetime import date, datetime, timedelta

from fastapi import Depends, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from .. import audit, db
from . import core, http
from .core import Provider

OFF_WORDS = re.compile(r"\b(ooo|out of (the )?office|pto|vacation|holiday|annual leave|leave|off|sick|parental)\b", re.I)
LOOKBACK_DAYS = 30
AHEAD_DAYS = 180


def _day(s: str | None) -> date | None:
    try:
        return date.fromisoformat((s or "")[:10])
    except ValueError:
        return None


def store_off(rt, cx, items: list, removed: list) -> dict:
    """items: [(external id, start day, end day inclusive, title)]; removed: [external id]. Inside a transaction."""
    n = {"added": 0, "changed": 0, "removed": 0}
    for ext, start, end, title in items:
        ext = str(ext)[:200]
        ex = rt.conn.execute("SELECT * FROM time_off WHERE connection_id=? AND external_id=?", (cx.id, ext)).fetchone()
        if ex:
            if (ex["start_on"], ex["end_on"]) != (start.isoformat(), end.isoformat()):
                rt.conn.execute("UPDATE time_off SET start_on=?, end_on=?, title=? WHERE id=?",
                                (start.isoformat(), end.isoformat(), title[:120], ex["id"]))
                n["changed"] += 1
        else:
            rt.conn.execute("INSERT INTO time_off (id, tenant_id, user_id, start_on, end_on, source, external_id,"
                            " connection_id, title, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                            ("off_" + secrets.token_hex(6), rt.tenant_id, cx.user_id, start.isoformat(),
                             end.isoformat(), cx.provider, ext, cx.id, title[:120], audit.now()))
            n["added"] += 1
    for ext in removed:
        r = rt.conn.execute("DELETE FROM time_off WHERE connection_id=? AND external_id=?", (cx.id, str(ext)[:200]))
        n["removed"] += r.rowcount or 0
    if any(n.values()):
        rt.log(f"sync:{cx.provider}", "time_off.sync", cx.user_id, n)
    return n


def is_off(title: str, event_type: str = "", show_as: str = "", all_day: bool = False) -> bool:
    if event_type == "outOfOffice" or show_as == "oof":
        return True
    return all_day and bool(OFF_WORDS.search(title or ""))


# ===================================================================== Google

class GoogleOAuth(Provider):
    env = "GOOGLE"
    personal = True
    authorize_url = "https://accounts.google.com/o/oauth2/v2/auth"
    token_url = "https://oauth2.googleapis.com/token"
    pkce = True

    def authorize_params(self, state, challenge):
        return {**super().authorize_params(state, challenge), "access_type": "offline", "prompt": "consent",
                "include_granted_scopes": "true"}

    def identify(self, rt, tokens):
        me = http.call("GET", "https://openidconnect.googleapis.com/v1/userinfo", "Google profile",
                       bearer=tokens["access_token"])
        return {"external_account_id": me.get("sub", ""), "account_name": me.get("email", self.name),
                "extra": {"email": me.get("email")}}


GCAL = "https://www.googleapis.com/calendar/v3"


class GoogleCalendar(GoogleOAuth):
    key = "google_calendar"
    name = "Google Calendar"
    category = "calendar"
    scopes = ("openid", "email", "https://www.googleapis.com/auth/calendar.events.readonly")
    poll_minutes = 60
    reconcile_hours = 24
    nudge = True
    blurb = "Your out-of-office days come off your capacity automatically."
    setup = ("Create an OAuth client (Google Cloud console → APIs & Services → Credentials, type Web application) "
             "with the callback URL above, and enable the Google Calendar API. Shared with Gmail.")

    def sync(self, rt, cx, full=False):
        tok = cx.cursor.get("sync_token") if not full else None
        today = core.now().date()
        items, removed, page, next_sync = [], [], None, None
        for _ in range(40):
            params = {"singleEvents": "true", "maxResults": 250, "pageToken": page}
            if tok:
                params["syncToken"] = tok
            else:
                params["timeMin"] = f"{today - timedelta(days=LOOKBACK_DAYS)}T00:00:00Z"
                params["timeMax"] = f"{today + timedelta(days=AHEAD_DAYS)}T00:00:00Z"
            r = http.request("GET", f"{GCAL}/calendars/primary/events", params=params,
                             bearer=core.access_token(rt.conn, cx))
            if r.status == 410 and tok:  # sync token expired: start over
                with rt.conn.tx():
                    core.save(rt.conn, cx, cursor={})
                return self.sync(rt, core.get(rt.conn, cx.id), full=True)
            if not r.ok:
                raise http.HTTPError(f"Google Calendar failed ({r.status})", r.status)
            body = r.json()
            for ev in body.get("items", []):
                if ev.get("status") == "cancelled":
                    removed.append(ev["id"])
                    continue
                s, e = ev.get("start") or {}, ev.get("end") or {}
                all_day = "date" in s
                if not is_off(ev.get("summary", ""), ev.get("eventType", ""), all_day=all_day):
                    removed.append(ev["id"])  # it may have been time off before an edit
                    continue
                start = _day(s.get("date") or s.get("dateTime"))
                end = _day(e.get("date") or e.get("dateTime"))
                if not start:
                    continue
                if all_day and end:
                    end = end - timedelta(days=1)  # all-day end dates are exclusive
                items.append((ev["id"], start, max(start, end or start), ev.get("summary") or "Out of office"))
            page = body.get("nextPageToken")
            next_sync = body.get("nextSyncToken") or next_sync
            if not page:
                break
        with rt.conn.tx():
            if full:
                keep = {i[0] for i in items}
                removed += [r["external_id"] for r in rt.conn.execute(
                    "SELECT external_id FROM time_off WHERE connection_id=?", (cx.id,)) if r["external_id"] not in keep]
            n = store_off(rt, cx, items, removed)
            core.save(rt.conn, cx, cursor={"sync_token": next_sync} if next_sync else cx.cursor)
        return n

    def after_connect(self, rt, cx):
        core.run_sync(rt.conn, cx, full=True)
        self.renew(rt, core.get(rt.conn, cx.id))

    def renew(self, rt, cx):
        if not core.public_url().startswith("https://"):
            return  # Google only pushes to https; the hourly poll covers it
        old = cx.webhook.get("channel")
        if old:
            http.request("POST", f"{GCAL}/channels/stop", bearer=core.access_token(rt.conn, cx),
                         json_body={"id": old["id"], "resourceId": old.get("resource_id")})
        cid = secrets.token_hex(16)
        r = http.call("POST", f"{GCAL}/calendars/primary/events/watch", "Google Calendar watch",
                      bearer=core.access_token(rt.conn, cx),
                      json_body={"id": cid, "type": "web_hook", "address": core.hook_url(self.key, cx.id),
                                 "token": cx.webhook.get("key", ""), "params": {"ttl": str(7 * 86400)}})
        exp = datetime.fromtimestamp(int(r.get("expiration", 0)) / 1000, tz=core.now().tzinfo) \
            if r.get("expiration") else core.now() + timedelta(days=7)
        with rt.conn.tx():
            core.save(rt.conn, cx, webhook={**cx.webhook, "channel": {"id": cid, "resource_id": r.get("resourceId")},
                                            "renew_at": core.iso(exp - timedelta(days=1))})

    def before_disconnect(self, rt, cx):
        ch = cx.webhook.get("channel")
        if ch:
            http.request("POST", f"{GCAL}/channels/stop", bearer=core.access_token(rt.conn, cx),
                         json_body={"id": ch["id"], "resourceId": ch.get("resource_id")})
        with rt.conn.tx():
            rt.conn.execute("DELETE FROM time_off WHERE connection_id=?", (cx.id,))

    def verify(self, rt, cx, headers, body, query):
        ch = cx.webhook.get("channel") or {}
        return bool(ch) and headers.get("x-goog-channel-id") == ch.get("id") and \
            secrets.compare_digest(headers.get("x-goog-channel-token", ""), cx.webhook.get("key", ""))

    def split(self, headers, payload):
        return [(f"{headers.get('x-goog-channel-id')}:{headers.get('x-goog-message-number')}",
                 headers.get("x-goog-resource-state", ""), {})]

    def handle(self, rt, cx, kind, payload):
        if kind != "sync":
            _debounced_sync(rt, cx)


def _debounced_sync(rt, cx):
    last = core.parse(core.get(rt.conn, cx.id)["last_sync_at"])
    if last and last > core.now() - timedelta(seconds=20):
        return
    core.run_sync(rt.conn, core.get(rt.conn, cx.id))


# ================================================================== Microsoft

GRAPH = "https://graph.microsoft.com/v1.0"


class MicrosoftOAuth(Provider):
    env = "MICROSOFT"
    personal = True
    pkce = True

    @property
    def tenant(self) -> str:
        import os
        return os.environ.get("FIELDWORK_MICROSOFT_TENANT", "common")

    @property
    def authorize_url(self):
        return f"https://login.microsoftonline.com/{self.tenant}/oauth2/v2.0/authorize"

    @property
    def token_url(self):
        return f"https://login.microsoftonline.com/{self.tenant}/oauth2/v2.0/token"

    def refresh(self, tokens):
        r = http.request("POST", self.token_url, form={
            "grant_type": "refresh_token", "refresh_token": tokens.get("refresh_token", ""),
            "client_id": self.client_id(), "client_secret": self.client_secret(), "scope": " ".join(self.scopes)})
        if r.status in (400, 401):
            raise core.NeedsReauth("Microsoft refused the refresh token; reconnect it")
        if not r.ok:
            raise http.HTTPError(f"Microsoft token refresh failed ({r.status})", r.status)
        return {**tokens, **r.json()}

    def identify(self, rt, tokens):
        me = http.call("GET", f"{GRAPH}/me", "Microsoft profile", bearer=tokens["access_token"])
        email = me.get("mail") or me.get("userPrincipalName") or ""
        return {"external_account_id": me.get("id", ""), "account_name": email or self.name, "extra": {"email": email}}

    # Graph subscriptions: shared by calendar and mail
    resource = ""

    def handshake(self, rt, cx, headers, body, query):
        if "validationToken" in query:
            return PlainTextResponse(query["validationToken"])
        return None

    def verify(self, rt, cx, headers, body, query):
        import json
        try:
            vals = json.loads(body or b"{}").get("value", [])
        except ValueError:
            return False
        key = cx.webhook.get("key", "")
        return bool(vals) and all(secrets.compare_digest(str(v.get("clientState", "")), key) for v in vals)

    def split(self, headers, payload):
        out = []
        for v in payload.get("value", []):
            ext = hashlib.sha256(repr(sorted(v.items())).encode()).hexdigest()[:40]
            out.append((ext, v.get("changeType", "changed"), {"resource": v.get("resource")}))
        return out

    def handle(self, rt, cx, kind, payload):
        _debounced_sync(rt, cx)

    def renew(self, rt, cx):
        if not core.public_url().startswith("https://") or not self.resource:
            return
        exp = core.now() + timedelta(minutes=4000)
        sub = cx.webhook.get("subscription")
        if sub:
            core.authed(rt.conn, cx, "PATCH", f"{GRAPH}/subscriptions/{sub}", "Graph subscription",
                        json_body={"expirationDateTime": exp.strftime("%Y-%m-%dT%H:%M:%S.0000000Z")})
        else:
            r = core.authed(rt.conn, cx, "POST", f"{GRAPH}/subscriptions", "Graph subscription", json_body={
                "changeType": "created,updated,deleted", "notificationUrl": core.hook_url(self.key, cx.id),
                "resource": self.resource, "clientState": cx.webhook.get("key", ""),
                "expirationDateTime": exp.strftime("%Y-%m-%dT%H:%M:%S.0000000Z")})
            sub = r.get("id")
        with rt.conn.tx():
            core.save(rt.conn, cx, webhook={**cx.webhook, "subscription": sub,
                                            "renew_at": core.iso(exp - timedelta(hours=12))})

    def before_disconnect(self, rt, cx):
        sub = cx.webhook.get("subscription")
        if sub:
            http.request("DELETE", f"{GRAPH}/subscriptions/{sub}", bearer=core.access_token(rt.conn, cx))


class OutlookCalendar(MicrosoftOAuth):
    key = "outlook_calendar"
    name = "Outlook Calendar"
    category = "calendar"
    scopes = ("offline_access", "User.Read", "Calendars.ReadBasic")
    resource = "me/events"
    poll_minutes = 60
    reconcile_hours = 24
    nudge = True
    blurb = "Your Away days in Outlook come off your capacity automatically."
    setup = ("Register an app in Microsoft Entra ID (App registrations) with the callback URL above as a Web "
             "redirect URI, a client secret, and delegated Graph permissions offline_access, User.Read, "
             "Calendars.ReadBasic (and Mail.ReadBasic for the email signal). Shared with Outlook Mail.")

    def sync(self, rt, cx, full=False):
        link = None if full else cx.cursor.get("delta")
        today = core.now().date()
        url = link or f"{GRAPH}/me/calendarView/delta"
        params = None if link else {"startDateTime": f"{today - timedelta(days=LOOKBACK_DAYS)}T00:00:00Z",
                                    "endDateTime": f"{today + timedelta(days=AHEAD_DAYS)}T00:00:00Z"}
        items, removed, delta = [], [], None
        for _ in range(60):
            r = http.request("GET", url, params=params, bearer=core.access_token(rt.conn, cx),
                             headers={"Prefer": 'outlook.timezone="UTC", odata.maxpagesize=200'})
            if r.status == 410 and link:
                with rt.conn.tx():
                    core.save(rt.conn, cx, cursor={})
                return self.sync(rt, core.get(rt.conn, cx.id), full=True)
            if not r.ok:
                raise http.HTTPError(f"Outlook calendar failed ({r.status})", r.status)
            body = r.json()
            for ev in body.get("value", []):
                if "@removed" in ev:
                    removed.append(ev["id"])
                    continue
                if not is_off(ev.get("subject", ""), show_as=ev.get("showAs", ""), all_day=bool(ev.get("isAllDay"))):
                    removed.append(ev["id"])
                    continue
                start = _day((ev.get("start") or {}).get("dateTime"))
                end_dt = (ev.get("end") or {}).get("dateTime") or ""
                end = _day(end_dt)
                if not start:
                    continue
                if end and (ev.get("isAllDay") or end_dt[11:19] == "00:00:00") and end > start:
                    end = end - timedelta(days=1)
                items.append((ev["id"], start, max(start, end or start), ev.get("subject") or "Away"))
            url, params = body.get("@odata.nextLink"), None
            delta = body.get("@odata.deltaLink") or delta
            if not url:
                break
        with rt.conn.tx():
            if full:
                keep = {i[0] for i in items}
                removed += [r["external_id"] for r in rt.conn.execute(
                    "SELECT external_id FROM time_off WHERE connection_id=?", (cx.id,)) if r["external_id"] not in keep]
            n = store_off(rt, cx, items, removed)
            if delta:
                core.save(rt.conn, cx, cursor={"delta": delta})
        return n

    def after_connect(self, rt, cx):
        core.run_sync(rt.conn, cx, full=True)
        self.renew(rt, core.get(rt.conn, cx.id))

    def before_disconnect(self, rt, cx):
        super().before_disconnect(rt, cx)
        with rt.conn.tx():
            rt.conn.execute("DELETE FROM time_off WHERE connection_id=?", (cx.id,))


GOOGLE_CALENDAR = core.register(GoogleCalendar())
OUTLOOK_CALENDAR = core.register(OutlookCalendar())


# ============================================================ time off, by hand

def register_routes(app, d):
    conn, ctx, Ctx = d.conn, d.ctx, d.Ctx

    def out(r, names):
        return {**dict(r), "user": names.get(r["user_id"], "")}

    @app.get("/api/time-off")
    def list_off(c: Ctx = Depends(ctx)):
        if not c.can("task.view_internal"):
            return []
        since = (core.now().date() - timedelta(days=7)).isoformat()
        q = "SELECT * FROM time_off WHERE tenant_id=? AND end_on>=?"
        args = [c.tenant_id, since]
        if not c.can("people.read"):
            q += " AND user_id=?"
            args.append(c.uid)
        names = {u["id"]: u["name"] for u in conn.execute("SELECT id, name FROM users WHERE tenant_id=?", (c.tenant_id,))}
        return [out(r, names) for r in conn.execute(q + " ORDER BY start_on", args)]

    class OffIn(BaseModel):
        start_on: str
        end_on: str
        title: str = Field(default="Time off", max_length=120)
        user_id: str | None = None

    @app.post("/api/time-off", status_code=201)
    def add_off(body: OffIn, c: Ctx = Depends(ctx)):
        if not c.can("task.view_internal"):
            raise HTTPException(403, "time off is tracked for the delivery team")
        uid = body.user_id or c.uid
        if uid != c.uid:
            c.require("people.manage")
            if not conn.execute("SELECT 1 FROM users WHERE id=? AND tenant_id=?", (uid, c.tenant_id)).fetchone():
                raise HTTPException(404, "person not found")
        s, e = _day(body.start_on), _day(body.end_on)
        if not s or not e or e < s or (e - s).days > 366:
            raise HTTPException(422, "give a start and end day (YYYY-MM-DD), end on or after start")
        oid = "off_" + secrets.token_hex(6)
        with db.tx(conn):
            conn.execute("INSERT INTO time_off (id, tenant_id, user_id, start_on, end_on, source, title, created_at)"
                         " VALUES (?,?,?,?,?,?,?,?)", (oid, c.tenant_id, uid, s.isoformat(), e.isoformat(), "console",
                                                      body.title or "Time off", audit.now()))
            c.log("time_off.add", uid, {"start_on": s.isoformat(), "end_on": e.isoformat()})
        return {"id": oid}

    @app.delete("/api/time-off/{oid}")
    def remove_off(oid: str, c: Ctx = Depends(ctx)):
        r = conn.execute("SELECT * FROM time_off WHERE id=? AND tenant_id=?", (oid, c.tenant_id)).fetchone()
        if not r or (r["user_id"] != c.uid and not c.can("people.manage")):
            raise HTTPException(404, "not found")
        if r["connection_id"]:
            raise HTTPException(409, "this came from a calendar; change it there")
        with db.tx(conn):
            conn.execute("DELETE FROM time_off WHERE id=?", (oid,))
            c.log("time_off.remove", r["user_id"], {"start_on": r["start_on"], "end_on": r["end_on"]})
        return {"ok": True}
