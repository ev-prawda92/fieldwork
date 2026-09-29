"""The billing bridge: one path from a customer's sign-off to the ERP, whichever ERP it is.

    sign-off (sow.py) → on_accepted() queues a push in the outbox, in the same transaction
    outbox worker → push() → the ERP connector marks the milestone complete / creates the billing event
    scheduler → sync() → pull_statuses() reads invoiced / paid back, and the record moves on

Every ERP connector implements the same four calls:

    projects(rt, cx)                -> [{"id", "name", "customer", "currency"}]   (what a SOW can link to)
    lines(rt, cx, project_id)       -> [{"id", "name", "amount", "due_on", "state"}]  (its billing milestones)
    complete(rt, cx, ms, sow, pk)   -> {"external_id"?, "note"?}   (tell the ERP it's done and signed off)
    line_state(rt, cx, ms, sow)     -> {"status": "invoiced"|"paid", "invoice_ref", "on"} or None

No ERP connected, or one that can't be written to: the signed `milestone.accepted`
webhook and the finance CSV carry the same facts, packet hash included.
"""

import re
from datetime import date

from fastapi import Depends, HTTPException
from pydantic import BaseModel, Field

from .. import audit, db, events
from . import core, http

SAFE_ID = re.compile(r"^[A-Za-z0-9_.:\-/ |#]{1,120}$")


class BillingProvider(core.Provider):
    category = "billing"
    poll_minutes = 60
    reconcile_hours = 24
    pushes = True              # False: the ERP is told through its own integration (the signed webhook)

    def projects(self, rt, cx) -> list:
        return []

    def lines(self, rt, cx, project_id: str) -> list:
        return []

    def complete(self, rt, cx, ms, sow, packet: dict) -> dict:
        raise core.ConnectError(f"{self.name} is told about sign-offs by your own integration")

    def line_state(self, rt, cx, ms, sow) -> dict | None:
        return None

    def sync(self, rt, cx, full: bool = False) -> dict:
        return pull_statuses(rt, cx)


def safe_id(v: str, what: str = "id") -> str:
    v = (v or "").strip()
    if not SAFE_ID.match(v):
        raise core.ConnectError(f"that doesn't look like a {what}")
    return v


def quote(v: str) -> str:
    """A value for a query string literal in SOQL / SuiteQL / Oracle q= / OData filters."""
    return safe_id(v).replace("'", "")


# ---------------------------------------------------------------- the bridge

def _billing_cx(conn, sow):
    if not sow or not sow["connection_id"]:
        return None
    cx = core.get(conn, sow["connection_id"], sow["tenant_id"])
    if not cx or cx["status"] != "active":
        return None
    try:
        return cx if isinstance(cx.p, BillingProvider) else None
    except core.ConnectError:
        return None


def on_accepted(conn, tenant_id: str, mid: str) -> None:
    """Inside the sign-off's transaction: queue the push to the ERP the SOW is linked to, if any."""
    ms = conn.execute("SELECT * FROM milestones WHERE id=? AND tenant_id=?", (mid, tenant_id)).fetchone()
    sow = conn.execute("SELECT * FROM sows WHERE id=?", (ms["sow_id"],)).fetchone() if ms else None
    cx = _billing_cx(conn, sow)
    if not cx:
        return
    if not cx.p.pushes:
        conn.execute("UPDATE milestones SET erp_status='notified', erp_error=NULL WHERE id=?", (mid,))
        return
    conn.execute("UPDATE milestones SET erp_status='queued', erp_error=NULL WHERE id=?", (mid,))
    events.enqueue(conn, tenant_id, "billing_push", {"milestone_id": mid})


def push(conn, tenant_id: str, payload: dict) -> None:
    """Outbox delivery: tell the ERP. Raises to be retried with backoff."""
    mid = payload["milestone_id"]
    ms = conn.execute("SELECT * FROM milestones WHERE id=? AND tenant_id=?", (mid, tenant_id)).fetchone()
    if not ms or ms["status"] not in ("accepted", "invoiced", "paid") or ms["erp_status"] == "sent":
        return
    sow = conn.execute("SELECT * FROM sows WHERE id=?", (ms["sow_id"],)).fetchone()
    cx = _billing_cx(conn, sow)
    if not cx:
        with conn.tx():
            conn.execute("UPDATE milestones SET erp_status='not_linked' WHERE id=?", (mid,))
        return
    pk = conn.execute("SELECT * FROM milestone_packets WHERE milestone_id=? ORDER BY created_at DESC, id DESC LIMIT 1",
                      (mid,)).fetchone()
    packet = {"customer_hash": pk["customer_hash"] if pk else None, "record_position": pk["audit_seq"] if pk else None,
              "accepted_on": (ms["decided_at"] or "")[:10] or date.today().isoformat(),
              "accepted_by": _name(conn, ms["decided_by"]), "proxy": bool(ms["proxy"])}
    rt = core.Runtime(conn, tenant_id)
    try:
        res = cx.p.complete(rt, cx, ms, sow, packet) or {}
    except Exception as e:
        with conn.tx():
            conn.execute("UPDATE milestones SET erp_status='error', erp_error=? WHERE id=?", (str(e)[:500], mid))
            core.save(conn, core.get(conn, cx.id), last_error=f"billing: {str(e)[:400]}")
        raise
    with conn.tx():
        conn.execute("UPDATE milestones SET erp_status='sent', erp_error=NULL, external_id=COALESCE(?, external_id)"
                     " WHERE id=?", (res.get("external_id"), mid))
        rt.log(f"erp:{cx.provider}", "milestone.erp_push", mid,
               {"connection": cx.id, "external_id": res.get("external_id") or ms["external_id"],
                "note": res.get("note", ""), "customer_packet": packet["customer_hash"]})


def _name(conn, uid):
    r = conn.execute("SELECT name FROM users WHERE id=?", (uid,)).fetchone() if uid else None
    return r["name"] if r else ""


def pull_statuses(rt, cx) -> dict:
    """Read invoiced / paid back for this connection's signed-off milestones."""
    conn = rt.conn
    rows = conn.execute("SELECT m.* FROM milestones m JOIN sows s ON s.id=m.sow_id WHERE s.connection_id=?"
                        " AND m.status IN ('accepted','invoiced')", (cx.id,)).fetchall()
    moved = {"invoiced": 0, "paid": 0, "checked": len(rows)}
    for ms in rows:
        sow = conn.execute("SELECT * FROM sows WHERE id=?", (ms["sow_id"],)).fetchone()
        st = cx.p.line_state(rt, cx, ms, sow) or {}
        to = st.get("status")
        if to not in ("invoiced", "paid") or to == ms["status"]:
            continue
        on = (st.get("on") or date.today().isoformat())[:10]
        ref = (st.get("invoice_ref") or ms["invoice_ref"] or "")[:120] or None
        with conn.tx():
            if to == "invoiced":
                conn.execute("UPDATE milestones SET status='invoiced', invoice_ref=?, invoiced_at=?, updated_at=?"
                             " WHERE id=? AND status='accepted'", (ref, on, audit.now(), ms["id"]))
            else:
                conn.execute("UPDATE milestones SET status='paid', invoice_ref=?, paid_at=?,"
                             " invoiced_at=COALESCE(invoiced_at, ?), updated_at=? WHERE id=?",
                             (ref, on, on, audit.now(), ms["id"]))
            rt.log(f"erp:{cx.provider}", f"milestone.{to}", ms["id"],
                   {"deployment": ms["deployment_id"], "amount": ms["amount"], "invoice_ref": ref, "on": on,
                    "via": cx.provider})
        moved[to] += 1
    return moved


# ---------------------------------------------------------------- routes

def register_routes(app, d) -> None:
    conn, ctx, Ctx = d.conn, d.ctx, d.Ctx

    def billing_conn(c, cid: str):
        cx = core.get(conn, cid, c.tenant_id)
        if not cx or cx["status"] == "disconnected" or cx.user_id:
            raise HTTPException(404, "connection not found")
        try:
            if not isinstance(cx.p, BillingProvider):
                raise HTTPException(422, "that connection isn't a billing system")
        except core.ConnectError:
            raise HTTPException(404, "connection not found")
        return cx

    def sow_editor(c):
        if not c.can("sow.edit"):
            raise HTTPException(403, "your role can't set up statements of work")

    def ask(fn):
        try:
            return fn()
        except http.HTTPError as e:
            raise HTTPException(502, str(e)[:400])

    @app.get("/api/billing/connections")
    def billing_connections(c: Ctx = Depends(ctx)):
        sow_editor(c)
        out = []
        for r in conn.execute("SELECT * FROM connections WHERE tenant_id=? AND status='active' AND user_id IS NULL",
                              (c.tenant_id,)).fetchall():
            cx = core.Conn(r)
            try:
                if isinstance(cx.p, BillingProvider):
                    out.append({"id": cx.id, "provider": cx.provider, "name": cx.p.name,
                                "account_name": cx["account_name"], "pushes": cx.p.pushes})
            except core.ConnectError:
                continue
        return out

    @app.get("/api/billing/connections/{cid}/projects")
    def erp_projects(cid: str, c: Ctx = Depends(ctx)):
        sow_editor(c)
        cx = billing_conn(c, cid)
        return ask(lambda: cx.p.projects(core.Runtime(conn, c.tenant_id), cx))

    @app.get("/api/billing/connections/{cid}/projects/{pid:path}/lines")
    def erp_lines(cid: str, pid: str, c: Ctx = Depends(ctx)):
        sow_editor(c)
        cx = billing_conn(c, cid)
        return ask(lambda: cx.p.lines(core.Runtime(conn, c.tenant_id), cx, safe_id(pid, "project id")))

    class ImportIn(BaseModel):
        connection_id: str
        project_id: str = Field(min_length=1, max_length=120)
        reference: str = Field(default="", max_length=120)
        currency: str = "USD"
        signed_on: str | None = None

    @app.post("/api/deployments/{dep_id}/sows/import", status_code=201)
    def import_sow(dep_id: str, body: ImportIn, c: Ctx = Depends(ctx)):
        """Create a SOW from the ERP's own project/contract and its billing milestones, linked both ways."""
        c.deployment(dep_id)
        c.require_on("sow.edit", dep_id)
        cx = billing_conn(c, body.connection_id)
        pid = safe_id(body.project_id, "project id")
        rt = core.Runtime(conn, c.tenant_id)
        lines = ask(lambda: cx.p.lines(rt, cx, pid))
        proj = next((p for p in ask(lambda: cx.p.projects(rt, cx)) if str(p["id"]) == pid), {"id": pid, "name": pid})
        from .. import sow as sow_mod
        cur = (proj.get("currency") or body.currency or "USD").upper()
        if cur not in sow_mod.CURRENCIES:
            cur = "USD"
        ts = audit.now()
        sid = sow_mod.new_id("sow")
        total = round(sum(float(x.get("amount") or 0) for x in lines), 2)
        with db.tx(conn):
            if conn.execute("SELECT 1 FROM sows WHERE tenant_id=? AND connection_id=? AND external_id=?",
                            (c.tenant_id, cx.id, pid)).fetchone():
                raise HTTPException(409, "that project is already linked to a statement of work")
            conn.execute("INSERT INTO sows (id, tenant_id, deployment_id, reference, total_value, currency, signed_on,"
                         " notes, external_id, connection_id, created_by, created_at, updated_at)"
                         " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (sid, c.tenant_id, dep_id, body.reference or proj.get("name") or pid, total, cur,
                          body.signed_on, f"Imported from {cx.p.name}", pid, cx.id, c.uid, ts, ts))
            for i, ln in enumerate(lines):
                state = ln.get("state") or "open"
                status = {"invoiced": "invoiced", "paid": "paid"}.get(state, "pending")
                conn.execute("INSERT INTO milestones (id, tenant_id, deployment_id, sow_id, name, amount, due_on,"
                             " position, status, external_id, erp_status, invoice_ref, created_at, updated_at)"
                             " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                             (sow_mod.new_id("ms"), c.tenant_id, dep_id, sid, (ln.get("name") or "Milestone")[:160],
                              round(float(ln.get("amount") or 0), 2), (ln.get("due_on") or None) and ln["due_on"][:10],
                              i, status, str(ln["id"])[:120], "linked", ln.get("invoice_ref"), ts, ts))
            c.log("sow.import", sid, {"deployment": dep_id, "connection": cx.id, "provider": cx.provider,
                                      "project": pid, "milestones": len(lines), "total_value": total})
        return {"id": sid, "milestones": len(lines), "total_value": total,
                "note": "Tie each milestone to a stage so it's ready to bill when the stage is done."}

    class LinkIn(BaseModel):
        connection_id: str | None = None
        external_id: str | None = Field(default=None, max_length=120)

    @app.put("/api/sows/{sid}/link")
    def link_sow(sid: str, body: LinkIn, c: Ctx = Depends(ctx)):
        s = conn.execute("SELECT * FROM sows WHERE id=? AND tenant_id=?", (sid, c.tenant_id)).fetchone()
        if not s:
            raise HTTPException(404, "statement of work not found")
        c.deployment(s["deployment_id"])
        c.require_on("sow.edit", s["deployment_id"])
        cid = None
        if body.connection_id:
            cid = billing_conn(c, body.connection_id).id
        ext = safe_id(body.external_id, "project id") if body.external_id else None
        with db.tx(conn):
            conn.execute("UPDATE sows SET connection_id=?, external_id=?, updated_at=? WHERE id=?",
                         (cid, ext, audit.now(), sid))
            c.log("sow.link", sid, {"connection": cid, "external_id": ext})
        return {"ok": True}

    @app.put("/api/milestones/{mid}/link")
    def link_milestone(mid: str, body: LinkIn, c: Ctx = Depends(ctx)):
        ms = conn.execute("SELECT * FROM milestones WHERE id=? AND tenant_id=?", (mid, c.tenant_id)).fetchone()
        if not ms:
            raise HTTPException(404, "milestone not found")
        c.deployment(ms["deployment_id"])
        c.require_on("sow.edit", ms["deployment_id"])
        ext = safe_id(body.external_id, "milestone id") if body.external_id else None
        with db.tx(conn):
            conn.execute("UPDATE milestones SET external_id=?, updated_at=? WHERE id=?", (ext, audit.now(), mid))
            c.log("milestone.link", mid, {"external_id": ext})
        return {"ok": True}

    @app.post("/api/milestones/{mid}/push")
    def repush(mid: str, c: Ctx = Depends(ctx)):
        ms = conn.execute("SELECT * FROM milestones WHERE id=? AND tenant_id=?", (mid, c.tenant_id)).fetchone()
        if not ms:
            raise HTTPException(404, "milestone not found")
        c.deployment(ms["deployment_id"])
        c.require_on("sow.edit", ms["deployment_id"])
        if ms["status"] not in ("accepted", "invoiced", "paid"):
            raise HTTPException(409, "only a signed-off milestone goes to the ERP")
        sow = conn.execute("SELECT * FROM sows WHERE id=?", (ms["sow_id"],)).fetchone()
        if not _billing_cx(conn, sow):
            raise HTTPException(409, "link this statement of work to a connected billing system first")
        with db.tx(conn):
            conn.execute("UPDATE milestones SET erp_status=NULL WHERE id=?", (mid,))
            on_accepted(conn, c.tenant_id, mid)
        try:  # try now; if the ERP is down, the queued push retries with backoff
            push(conn, c.tenant_id, {"milestone_id": mid})
        except Exception:
            pass
        r = conn.execute("SELECT erp_status, erp_error, external_id FROM milestones WHERE id=?", (mid,)).fetchone()
        return {k: r[k] for k in r.keys()}
