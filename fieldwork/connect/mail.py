"""Last customer contact, from email metadata (opt-in, per person).

A person on the delivery team can connect their Gmail or Outlook mailbox.
Fieldwork reads message headers only (Gmail's metadata scope, Graph's
Mail.ReadBasic) and keeps, for each message to or from a customer's email
domain, just three things: which customer, which direction, and when. No
subjects, bodies, addresses or attachments are stored.

That's enough for "last heard from the customer 9 days ago" on a deployment,
and for a quiet-customer flag when an active deployment goes silent.

Customer domains come from each customer's settings and from the email
domains of that customer's own people in the workspace.
"""


import hashlib
import json
from datetime import datetime, timedelta, timezone
from email.utils import getaddresses, parsedate_to_datetime

from fastapi import Depends, HTTPException
from pydantic import BaseModel, Field

from .. import config, db
from . import core, http
from .calendars import GRAPH, GoogleOAuth, MicrosoftOAuth

FREE_MAIL = {"gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "live.com", "yahoo.com", "icloud.com",
             "me.com", "aol.com", "proton.me", "protonmail.com", "gmx.com"}
PER_SYNC = 300


def customer_domains(conn, tenant_id: str, exclude: set) -> dict:
    """{domain: customer_id} from customers' domains and their own people's email domains."""
    out: dict = {}
    for c in conn.execute("SELECT id, domains FROM customers WHERE tenant_id=?", (tenant_id,)):
        for dom in (c["domains"] or "").split(","):
            dom = dom.strip().lower()
            if dom:
                out.setdefault(dom, c["id"])
    t = conn.execute("SELECT config_json FROM tenants WHERE id=?", (tenant_id,)).fetchone()
    cfg = config.upgrade(json.loads(t["config_json"]))
    outside = {r for r, s in cfg["permissions"].get("task.view_internal", {}).items() if s is None} | \
        {r["key"] for r in cfg["roles"] if r["key"] not in cfg["permissions"].get("task.view_internal", {})}
    for r in conn.execute("SELECT DISTINCT u.email, u.role, d.customer_id FROM users u"
                          " JOIN deployment_members m ON m.user_id=u.id JOIN deployments d ON d.id=m.deployment_id"
                          " WHERE u.tenant_id=?", (tenant_id,)):
        if r["role"] in outside and "@" in r["email"]:
            out.setdefault(r["email"].split("@")[1].lower(), r["customer_id"])
    return {k: v for k, v in out.items() if k not in exclude and k not in FREE_MAIL}


def _domain(addr: str) -> str:
    return addr.rsplit("@", 1)[1].lower().strip(">") if "@" in addr else ""


class MailSignal:
    category = "email"
    poll_minutes = 30
    reconcile_hours = 0
    blurb = "Opt in: shows when your customers last heard from you, and you from them. Headers only; " \
            "stores the customer, direction and time, nothing else."

    def record(self, rt, cx, msgs: list) -> dict:
        """msgs: [(message id, from address, [to/cc addresses], datetime)]"""
        mine = _domain(cx.extra.get("email") or "")
        own = {mine} | {_domain(u["email"]) for u in rt.conn.execute(
            "SELECT email FROM users WHERE id=?", (cx.user_id,))}
        doms = customer_domains(rt.conn, rt.tenant_id, own)
        n = 0
        with rt.conn.tx():
            for mid, frm, to, at in msgs:
                if not at:
                    continue
                fd = _domain(frm)
                if fd in doms:
                    hits = [(doms[fd], fd, "in")]
                elif fd in own:
                    hits = [(doms[d], d, "out") for d in {_domain(a) for a in to} if d in doms]
                else:
                    hits = []
                for cust, dom, direction in hits:
                    ext = hashlib.sha256(f"{mid}:{cust}".encode()).hexdigest()[:40]
                    r = rt.conn.execute(
                        "INSERT INTO contact_signals (tenant_id, user_id, connection_id, customer_id, domain, direction,"
                        " at, external_id) VALUES (?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING",
                        (rt.tenant_id, cx.user_id, cx.id, cust, dom, direction, core.iso(at), ext))
                    n += r.rowcount or 0
        return {"messages": len(msgs), "signals": n}

    def before_disconnect(self, rt, cx):
        with rt.conn.tx():
            rt.conn.execute("DELETE FROM contact_signals WHERE connection_id=?", (cx.id,))

    def after_connect(self, rt, cx):
        core.run_sync(rt.conn, cx)


GMAIL = "https://gmail.googleapis.com/gmail/v1/users/me"


class Gmail(MailSignal, GoogleOAuth):
    key = "gmail"
    name = "Gmail"
    scopes = ("openid", "email", "https://www.googleapis.com/auth/gmail.metadata")
    setup = ("Uses the Google OAuth client (enable the Gmail API too). gmail.metadata is a restricted scope: Google "
             "requires app verification before people outside your own Google Workspace can connect.")

    def _msg(self, rt, cx, mid: str):
        r = http.request("GET", f"{GMAIL}/messages/{mid}", bearer=core.access_token(rt.conn, cx),
                         params={"format": "metadata", "metadataHeaders": ["From", "To", "Cc", "Date"]})
        if r.status == 404:
            return None
        if not r.ok:
            raise http.HTTPError(f"Gmail failed ({r.status})", r.status)
        m = r.json()
        h = {x["name"].lower(): x["value"] for x in (m.get("payload") or {}).get("headers", [])}
        frm = (getaddresses([h.get("from", "")]) or [("", "")])[0][1]
        to = [a for _, a in getaddresses([h.get("to", ""), h.get("cc", "")]) if a]
        try:
            at = datetime.fromtimestamp(int(m["internalDate"]) / 1000, tz=timezone.utc)
        except (KeyError, ValueError, TypeError):
            try:
                at = parsedate_to_datetime(h["date"]) if h.get("date") else None
            except (TypeError, ValueError):
                at = None
        return (m.get("id", mid), frm, to, at)

    def sync(self, rt, cx, full=False):
        hist = cx.cursor.get("history_id")
        ids: list = []
        new_hist = hist
        if hist:
            page = None
            for _ in range(10):
                r = http.request("GET", f"{GMAIL}/history", bearer=core.access_token(rt.conn, cx),
                                 params={"startHistoryId": hist, "historyTypes": "messageAdded", "pageToken": page})
                if r.status == 404:  # history too old: start over from recent mail
                    hist = None
                    break
                if not r.ok:
                    raise http.HTTPError(f"Gmail history failed ({r.status})", r.status)
                b = r.json()
                for h in b.get("history", []):
                    ids += [x["message"]["id"] for x in h.get("messagesAdded", [])]
                new_hist = b.get("historyId", new_hist)
                page = b.get("nextPageToken")
                if not page:
                    break
        if not hist:
            prof = http.call("GET", f"{GMAIL}/profile", "Gmail profile", bearer=core.access_token(rt.conn, cx))
            new_hist = prof.get("historyId")
            b = http.call("GET", f"{GMAIL}/messages", "Gmail messages", bearer=core.access_token(rt.conn, cx),
                          params={"maxResults": 200})
            ids = [m["id"] for m in b.get("messages", [])]
        msgs = [m for m in (self._msg(rt, cx, i) for i in list(dict.fromkeys(ids))[:PER_SYNC]) if m]
        out = self.record(rt, cx, msgs)
        with rt.conn.tx():
            core.save(rt.conn, cx, cursor={"history_id": new_hist})
        return out


class OutlookMail(MailSignal, MicrosoftOAuth):
    key = "outlook_mail"
    name = "Outlook Mail"
    scopes = ("offline_access", "User.Read", "Mail.ReadBasic")
    setup = "Uses the Microsoft app registration (add the delegated Mail.ReadBasic permission)."

    def sync(self, rt, cx, full=False):
        since = cx.cursor.get("since") or core.iso(core.now() - timedelta(days=30))
        url = f"{GRAPH}/me/messages"
        params = {"$select": "from,toRecipients,ccRecipients,receivedDateTime,isDraft",
                  "$filter": f"receivedDateTime ge {since.replace('+00:00', 'Z')}",
                  "$orderby": "receivedDateTime asc", "$top": 100}
        msgs, latest = [], since
        for _ in range(PER_SYNC // 100):
            b = core.authed(rt.conn, cx, "GET", url, "Outlook mail", params=params)
            for m in b.get("value", []):
                if m.get("isDraft"):
                    continue
                frm = ((m.get("from") or {}).get("emailAddress") or {}).get("address", "")
                to = [((x.get("emailAddress") or {}).get("address") or "") for x in
                      (m.get("toRecipients") or []) + (m.get("ccRecipients") or [])]
                at = core.parse(m.get("receivedDateTime"))
                msgs.append((m.get("id", ""), frm, to, at))
                if at and core.iso(at) > latest:
                    latest = core.iso(at)
            url, params = b.get("@odata.nextLink"), None
            if not url:
                break
        out = self.record(rt, cx, msgs)
        with rt.conn.tx():
            core.save(rt.conn, cx, cursor={"since": latest})
        return out


GMAIL_SIGNAL = core.register(Gmail())
OUTLOOK_MAIL = core.register(OutlookMail())


def register_routes(app, d):
    conn, ctx, Ctx = d.conn, d.ctx, d.Ctx

    class DomainsIn(BaseModel):
        domains: list[str] = Field(default_factory=list, max_length=20)

    @app.put("/api/customers/{cid}/domains")
    def set_domains(cid: str, body: DomainsIn, c: Ctx = Depends(ctx)):
        if not (c.can("deployment.create") or c.can("config.edit")):
            raise HTTPException(403, "your role can't edit customers")
        if not conn.execute("SELECT 1 FROM customers WHERE id=? AND tenant_id=?", (cid, c.tenant_id)).fetchone():
            raise HTTPException(404, "customer not found")
        clean = []
        for x in body.domains:
            x = x.strip().lower().lstrip("@")
            if x.startswith("http"):
                x = x.split("//", 1)[1].split("/")[0]
            if x and "." in x and len(x) <= 200 and x not in FREE_MAIL:
                clean.append(x)
        with db.tx(conn):
            conn.execute("UPDATE customers SET domains=? WHERE id=?", (",".join(sorted(set(clean))), cid))
            c.log("customer.domains", cid, {"domains": sorted(set(clean))})
        return {"domains": sorted(set(clean))}
