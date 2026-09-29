"""Statements of work, billable milestones, and the customer's sign-off.

A statement of work splits a contract into milestones. Each milestone can be
tied to a stage: when the stage is marked done, the milestone becomes ready to
bill. Someone on the delivery side submits it, and Fieldwork freezes an
evidence packet at that moment: the stage dates, the shared work that was
finished, the confirmed results, the acceptance criteria and the amount. The
customer signs off against that exact packet (or asks for changes), and the
sign-off is what finance invoices against.

    pending ─(stage done)→ ready ─submit→ submitted ─accept→ accepted → invoiced → paid
                 ↑ (stage reopened)            └─request changes→ changes_requested ─submit→ …

Why it holds up in a dispute:
  - the packet's sha256 is written into the tamper-evident audit chain in the
    same transaction as the submit, and the customer accepts that hash;
  - each submit and sign-off leaves an anchor (audit position + hash) that the
    customer keeps on their receipt, and anyone in the workspace can check a
    receipt against the chain later (POST /api/audit/verify-anchor);
  - amounts and criteria lock once a milestone is submitted.

Two views of one engagement: the customer sees where things stand and what is
waiting on them; the delivery side also sees money (earned, ready to bill,
awaiting sign-off, at risk) and the hours and delays behind it.
"""

import csv
import hashlib
import io
import json
from datetime import date, datetime, timezone

from fastapi import Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field

from . import audit, config, db, events

STATUSES = ("pending", "ready", "submitted", "changes_requested", "accepted", "invoiced", "paid")
EDITABLE = ("pending", "ready", "changes_requested")
EARNED = ("accepted", "invoiced", "paid")
CURRENCIES = ("USD", "EUR", "GBP", "CAD", "AUD", "JPY", "CHF", "SGD", "INR")


def new_id(prefix: str) -> str:
    import secrets
    return f"{prefix}_{secrets.token_hex(6)}"


def fingerprint(obj) -> str:
    return "sha256:" + hashlib.sha256(audit.canonical(obj).encode("utf-8")).hexdigest()


def money_text(amount: float, currency: str) -> str:
    sym = {"USD": "$", "EUR": "€", "GBP": "£", "CAD": "CA$", "AUD": "A$", "JPY": "¥", "SGD": "S$",
           "INR": "₹", "CHF": "CHF "}.get(currency, currency + " ")
    return f"{sym}{amount:,.0f}" if float(amount).is_integer() else f"{sym}{amount:,.2f}"


def _row_dict(r) -> dict:
    return {k: r[k] for k in r.keys()}


def _dep(conn, dep_id: str):
    return conn.execute("SELECT * FROM deployments WHERE id=?", (dep_id,)).fetchone()


def _sow(conn, sow_id: str):
    return conn.execute("SELECT * FROM sows WHERE id=?", (sow_id,)).fetchone()


def _emit(conn, cfg, tenant_id: str, event: str, ms, actor_name: str, **extra) -> None:
    dep = _dep(conn, ms["deployment_id"])
    sow = _sow(conn, ms["sow_id"])
    events.emit(conn, cfg, tenant_id, event, {
        "deployment_id": ms["deployment_id"], "deployment": dep["name"] if dep else "",
        "milestone_id": ms["id"], "milestone": ms["name"], "amount": ms["amount"],
        "currency": sow["currency"] if sow else "USD",
        "amount_text": money_text(ms["amount"], sow["currency"] if sow else "USD"),
        "actor": actor_name, **extra})


def anchor(conn, tenant_id: str, reason: str, subject: str = "") -> dict:
    """Pin the audit chain's current head. Call inside the transaction, after the entry it covers."""
    head = conn.execute("SELECT seq, hash, at FROM audit WHERE tenant_id=? ORDER BY seq DESC LIMIT 1",
                        (tenant_id,)).fetchone()
    if not head:
        return {}
    conn.execute("INSERT INTO audit_anchors (tenant_id, seq, hash, reason, subject, at) VALUES (?,?,?,?,?,?)",
                 (tenant_id, head["seq"], head["hash"], reason, subject, audit.now()))
    return {"seq": head["seq"], "hash": head["hash"], "at": head["at"]}


def verify_anchor(conn, tenant_id: str, seq: int, hash_: str) -> dict:
    """Is the chain intact up to `seq`, and is the entry there still the one on the receipt?"""
    rows = conn.execute("SELECT * FROM audit WHERE tenant_id=? AND seq<=? ORDER BY seq", (tenant_id, seq)).fetchall()
    prev = audit.GENESIS
    for r in rows:
        expect = audit.entry_hash(prev, tenant_id, r["at"], r["actor_id"], r["action"], r["subject"],
                                  json.loads(r["detail_json"]))
        if r["prev_hash"] != prev or r["hash"] != expect:
            return {"ok": False, "reason": "the record was altered before this point", "broken_at": r["seq"]}
        prev = r["hash"]
    if not rows or rows[-1]["seq"] != seq:
        return {"ok": False, "reason": "no record entry at that position"}
    if rows[-1]["hash"] != hash_:
        return {"ok": False, "reason": "the record at that position doesn't match this receipt"}
    return {"ok": True, "seq": seq, "at": rows[-1]["at"]}


# ------------------------------------------------------------ stage hook

def on_stage_states(conn, tenant_id: str, cfg: dict, dep_id: str, states: dict, actor: str) -> list:
    """Called by ops.apply_states inside its transaction: stage done → milestone ready; reopened → back."""
    changed = []
    ts = audit.now()
    for ms in conn.execute("SELECT * FROM milestones WHERE deployment_id=? AND stage IS NOT NULL"
                           " AND status IN ('pending','ready')", (dep_id,)).fetchall():
        st = (states.get(ms["stage"]) or {}).get("state")
        if st == "done" and ms["status"] == "pending":
            conn.execute("UPDATE milestones SET status='ready', ready_at=?, updated_at=? WHERE id=?",
                         (ts, ts, ms["id"]))
            audit.record(conn, tenant_id, actor, "milestone.ready", ms["id"],
                         {"deployment": dep_id, "stage": ms["stage"], "amount": ms["amount"]})
            actor_row = conn.execute("SELECT name FROM users WHERE id=?", (actor,)).fetchone()
            _emit(conn, cfg, tenant_id, "milestone.ready", ms, actor_row["name"] if actor_row else actor)
            changed.append(ms["id"])
        elif st != "done" and ms["status"] == "ready":
            conn.execute("UPDATE milestones SET status='pending', ready_at=NULL, updated_at=? WHERE id=?",
                         (ts, ms["id"]))
            audit.record(conn, tenant_id, actor, "milestone.unready", ms["id"],
                         {"deployment": dep_id, "stage": ms["stage"], "stage_state": st})
            changed.append(ms["id"])
    return changed


# ------------------------------------------------------------ evidence packets

def build_packets(conn, cfg: dict, ms, submitted_by: str, note: str = "") -> tuple[dict, dict]:
    """(customer packet, internal packet). The customer's holds only what's shared with them."""
    from . import ops
    dep = _dep(conn, ms["deployment_id"])
    sow = _sow(conn, ms["sow_id"])
    cust = conn.execute("SELECT name FROM customers WHERE id=?", (dep["customer_id"],)).fetchone()
    names = {s["key"]: s["name"] for s in cfg["stages"]}
    states = ops.stage_states(conn, cfg, dep)
    who = conn.execute("SELECT name FROM users WHERE id=?", (submitted_by,)).fetchone()
    stage_filter = " AND t.stage=?" if ms["stage"] else ""
    args = (dep["id"], ms["stage"]) if ms["stage"] else (dep["id"],)

    def done_tasks(shared_only: bool) -> list:
        vis = " AND t.visibility='shared'" if shared_only else ""
        return [{"id": t["id"], "title": t["title"], "stage": names.get(t["stage"], t["stage"]),
                 "done_at": t["updated_at"], **({} if shared_only else {"by": t["who"]})}
                for t in conn.execute("SELECT t.*, u.name who FROM tasks t LEFT JOIN users u ON u.id=t.assignee_id"
                                      f" WHERE t.deployment_id=? AND t.status='done'{vis}{stage_filter}"
                                      " ORDER BY t.updated_at, t.id", args)]

    def results(shared_only: bool) -> list:
        vis = " AND f.visibility='shared'" if shared_only else ""
        out = []
        for f in conn.execute("SELECT f.*, u.name confirmer FROM findings f LEFT JOIN users u ON u.id=f.confirmed_by"
                              f" WHERE f.deployment_id=? AND f.confirmed_by IS NOT NULL{vis}"
                              " ORDER BY f.confirmed_at, f.id", (dep["id"],)):
            r = json.loads(f["result_json"] or "{}")
            out.append({"id": f["id"], "title": f["title"], "summary": str(r.get("summary") or "")[:500],
                        "confirmed_by": f["confirmer"], "confirmed_at": f["confirmed_at"]})
        return out

    base = {
        "kind": "fieldwork.milestone_packet", "version": 1,
        "deployment": {"id": dep["id"], "name": dep["name"], "customer": cust["name"] if cust else ""},
        "sow": {"id": sow["id"], "reference": sow["reference"], "currency": sow["currency"],
                "total_value": sow["total_value"], "signed_on": sow["signed_on"]},
        "milestone": {"id": ms["id"], "name": ms["name"], "amount": ms["amount"], "criteria": ms["criteria"],
                      "stage": names.get(ms["stage"], ms["stage"]) if ms["stage"] else None, "due_on": ms["due_on"]},
        "stages": [{"name": names[k], "state": v["state"], "started": (v["entered_at"] or "")[:10] or None,
                    "done": (v["done_at"] or "")[:10] or None} for k, v in states.items()],
        "submitted": {"at": audit.now(), "by": who["name"] if who else submitted_by, "note": note},
    }
    customer = {**base, "audience": "customer", "work_done": done_tasks(True), "results": results(True)}
    labels = ops.owner_labels("")
    delays = [{"owner": labels.get(r["confirmed_owner"] or r["proposed_owner"], r["proposed_owner"]),
               "confirmed": r["status"] != "open", "reason": r["confirmed_reason"] or r["proposed_reason"],
               "days": round(ops.span_days(r), 1), "stage": names.get(r["stage"], r["stage"])}
              for r in conn.execute("SELECT * FROM delays WHERE deployment_id=? ORDER BY started_at", (dep["id"],))]
    internal = {**base, "audience": "internal", "work_done": done_tasks(False), "results": results(False),
                "hours_logged": round(ops.hours_spent(conn, dep["id"]), 1), "budget_hours": dep["budget_hours"],
                "delays": delays,
                "customer_packet_hash": fingerprint(customer)}
    return customer, internal


def current_packet(conn, ms_id: str):
    return conn.execute("SELECT * FROM milestone_packets WHERE milestone_id=? ORDER BY created_at DESC, id DESC"
                        " LIMIT 1", (ms_id,)).fetchone()


def freeze(conn, tenant_id: str, cfg: dict, ms, actor: str, note: str, action: str) -> dict:
    customer, internal = build_packets(conn, cfg, ms, actor, note)
    ch, ih = fingerprint(customer), fingerprint(internal)
    audit.record(conn, tenant_id, actor, action, ms["id"],
                 {"deployment": ms["deployment_id"], "amount": ms["amount"], "customer_packet": ch,
                  "internal_packet": ih})
    anc = anchor(conn, tenant_id, action, ms["id"])
    pid = new_id("pkt")
    conn.execute("INSERT INTO milestone_packets (id, tenant_id, milestone_id, customer_json, internal_json,"
                 " customer_hash, internal_hash, audit_seq, audit_hash, created_by, created_at)"
                 " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                 (pid, tenant_id, ms["id"], audit.canonical(customer), audit.canonical(internal), ch, ih,
                  anc.get("seq"), anc.get("hash"), actor, audit.now()))
    return {"packet_id": pid, "customer_hash": ch, "internal_hash": ih, "anchor": anc}


# ------------------------------------------------------------ read-outs

def where_we_are(conn, cfg: dict, dep) -> dict:
    """The engagement at a glance: every stage and its state, where it is now, what's next."""
    from . import ops
    states = ops.stage_states(conn, cfg, dep)
    stages = [{"key": s["key"], "name": s["name"], "state": states[s["key"]]["state"],
               "started": (states[s["key"]]["entered_at"] or "")[:10] or None,
               "done": (states[s["key"]]["done_at"] or "")[:10] or None,
               "exit_criteria": s.get("exit_criteria", "")} for s in cfg["stages"]]
    finished = sum(1 for s in stages if s["state"] in ("done", "skipped"))
    cur = next((s for s in stages if s["key"] == dep["stage"]), None)
    return {"stages": stages, "current": cur["name"] if cur else dep["stage"], "current_key": dep["stage"],
            "done": finished, "total": len(stages), "on_hold": bool(dep["hold_since"]),
            "health": dep["health"]}


def milestone_out(conn, ms, sow, *, money: bool, internal: bool, names: dict, users: dict) -> dict:
    pk = current_packet(conn, ms["id"])
    today = date.today().isoformat()
    out = {"id": ms["id"], "sow_id": ms["sow_id"], "name": ms["name"], "criteria": ms["criteria"],
           "stage": ms["stage"], "stage_name": names.get(ms["stage"]) if ms["stage"] else None,
           "due_on": ms["due_on"], "position": ms["position"], "status": ms["status"],
           "ready_at": ms["ready_at"], "submitted_at": ms["submitted_at"], "decided_at": ms["decided_at"],
           "decided_by": users.get(ms["decided_by"]), "decision_note": ms["decision_note"],
           "proxy": bool(ms["proxy"]), "currency": sow["currency"],
           "overdue": bool(ms["due_on"] and ms["due_on"] < today and ms["status"] not in EARNED),
           "packet": {"customer_hash": pk["customer_hash"], "anchor_seq": pk["audit_seq"],
                      "anchor_hash": pk["audit_hash"], "at": pk["created_at"]} if pk else None}
    if money:
        out["amount"] = ms["amount"]
    if internal and money:
        out.update(invoice_ref=ms["invoice_ref"], invoiced_at=ms["invoiced_at"], paid_at=ms["paid_at"],
                   erp_status=ms["erp_status"], erp_error=ms["erp_error"], external_id=ms["external_id"])
    return out


def totals(rows: list) -> dict:
    t = {"contract": 0.0, "earned": 0.0, "ready_to_invoice": 0.0, "invoiced": 0.0, "paid": 0.0,
         "awaiting_signoff": 0.0, "ready_to_submit": 0.0, "in_delivery": 0.0, "at_risk": 0.0}
    for m in rows:
        a = float(m["amount"] or 0)
        s = m["status"]
        t["earned"] += a if s in EARNED else 0
        t["ready_to_invoice"] += a if s == "accepted" else 0
        t["invoiced"] += a if s in ("invoiced", "paid") else 0
        t["paid"] += a if s == "paid" else 0
        t["awaiting_signoff"] += a if s == "submitted" else 0
        t["ready_to_submit"] += a if s in ("ready", "changes_requested") else 0
        t["in_delivery"] += a if s == "pending" else 0
        t["at_risk"] += a if m.get("overdue") or s == "changes_requested" else 0
    return {k: round(v, 2) for k, v in t.items()}


def register(app, d) -> None:
    conn, ctx, Ctx = d.conn, d.ctx, d.Ctx

    def users_map(c) -> dict:
        return {r["id"]: r["name"] for r in conn.execute("SELECT id, name FROM users WHERE tenant_id=?",
                                                          (c.tenant_id,))}

    def stage_names(c) -> dict:
        return {s["key"]: s["name"] for s in c.cfg["stages"]}

    def is_customer_side(c, dep_id: str) -> bool:
        return not c.can_on("task.view_internal", dep_id)

    def sees_money(c, dep_id: str) -> bool:
        return c.can_on("billing.view", dep_id) or is_customer_side(c, dep_id)

    def ms_for(c, mid: str):
        ms = conn.execute("SELECT * FROM milestones WHERE id=? AND tenant_id=?", (mid, c.tenant_id)).fetchone()
        if not ms:
            raise HTTPException(404, "milestone not found")
        c.deployment(ms["deployment_id"])
        return ms

    def sow_for(c, sid: str):
        s = conn.execute("SELECT * FROM sows WHERE id=? AND tenant_id=?", (sid, c.tenant_id)).fetchone()
        if not s:
            raise HTTPException(404, "statement of work not found")
        c.deployment(s["deployment_id"])
        return s

    def check_stage(c, stage: str | None) -> str | None:
        if stage and stage not in config.stage_keys(c.cfg):
            raise HTTPException(422, f"unknown stage {stage!r}")
        return stage or None

    def check_day(v: str | None, name: str) -> str | None:
        if not v:
            return None
        try:
            return date.fromisoformat(v[:10]).isoformat()
        except ValueError:
            raise HTTPException(422, f"{name} must be YYYY-MM-DD")

    def sync_ready(c, dep_id: str) -> None:
        from . import ops
        dep = _dep(conn, dep_id)
        on_stage_states(conn, c.tenant_id, c.cfg, dep_id, ops.stage_states(conn, c.cfg, dep), c.uid)

    # ----------------------------------------------------------- read

    def commercials_for(c, dep) -> dict:
        dep_id = dep["id"]
        internal = not is_customer_side(c, dep_id)
        money = sees_money(c, dep_id)
        names, users = stage_names(c), users_map(c)
        sows = conn.execute("SELECT * FROM sows WHERE deployment_id=? ORDER BY created_at, id", (dep_id,)).fetchall()
        out_sows, all_ms = [], []
        for s in sows:
            ms = [milestone_out(conn, m, s, money=money, internal=internal, names=names, users=users)
                  for m in conn.execute("SELECT * FROM milestones WHERE sow_id=? ORDER BY position, created_at, id",
                                        (s["id"],))]
            all_ms += ms
            so = {"id": s["id"], "reference": s["reference"], "currency": s["currency"], "signed_on": s["signed_on"],
                  "milestones": ms}
            if money:
                so["total_value"] = s["total_value"]
                so["allocated"] = round(sum(m["amount"] for m in ms), 2)
            if internal:
                so.update(notes=s["notes"], external_id=s["external_id"], connection_id=s["connection_id"])
            out_sows.append(so)
        out = {"deployment_id": dep_id, "where": where_we_are(conn, c.cfg, dep), "sows": out_sows,
               "audience": "internal" if internal else "customer",
               "you_can": {a: c.can_on(a, dep_id) for a in ("sow.edit", "milestone.submit", "milestone.accept")}}
        waiting = [m for m in all_ms if m["status"] == "submitted"]
        out["waiting_on_customer"] = [{"id": m["id"], "name": m["name"]} for m in waiting]
        if money and all_ms:
            t = totals([{**m, "amount": m.get("amount", 0)} for m in all_ms])
            t["contract"] = round(sum(s["total_value"] for s in sows), 2)
            out["totals"] = t
            out["currency"] = sows[0]["currency"]
        if internal:
            from . import ops
            dl = conn.execute("SELECT * FROM delays WHERE deployment_id=?", (dep_id,)).fetchall()
            by: dict = {}
            for r in dl:
                o = r["confirmed_owner"] or r["proposed_owner"]
                by[o] = round(by.get(o, 0) + ops.span_days(r), 1)
            out["delay_days_by_owner"] = by
            out["hours_logged"] = round(ops.hours_spent(conn, dep_id), 1)
            out["budget_hours"] = dep["budget_hours"]
        return out

    @app.get("/api/deployments/{dep_id}/commercials")
    def commercials(dep_id: str, c: Ctx = Depends(ctx)):
        return commercials_for(c, c.deployment(dep_id))

    def portfolio_rows(c) -> list:
        deps = [r for r in d.visible_deployments(c) if c.can_on("billing.view", r["id"])]
        if not deps:
            return []
        ids = [r["id"] for r in deps]
        q = ("SELECT m.*, s.reference, s.currency, d.name deployment, cu.name customer FROM milestones m"
             " JOIN sows s ON s.id=m.sow_id JOIN deployments d ON d.id=m.deployment_id"
             " LEFT JOIN customers cu ON cu.id=d.customer_id"
             f" WHERE m.tenant_id=? AND m.deployment_id IN ({','.join('?' * len(ids))})"
             " ORDER BY d.name, s.created_at, m.position, m.id")
        return conn.execute(q, (c.tenant_id, *ids)).fetchall()

    @app.get("/api/commercials")
    def portfolio(c: Ctx = Depends(ctx)):
        c.require("billing.view")
        rows = portfolio_rows(c)
        today = date.today().isoformat()
        items = [{**_row_dict(r), "overdue": bool(r["due_on"] and r["due_on"] < today and r["status"] not in EARNED)}
                 for r in rows]
        dep_ids = sorted({r["deployment_id"] for r in rows})
        contract = 0.0
        if dep_ids:
            contract = conn.execute(f"SELECT COALESCE(SUM(total_value),0) v FROM sows WHERE deployment_id IN"
                                    f" ({','.join('?' * len(dep_ids))})", dep_ids).fetchone()["v"]
        t = totals(items)
        t["contract"] = round(float(contract), 2)
        pick = lambda *st: [{k: i[k] for k in ("id", "name", "amount", "currency", "deployment", "deployment_id",
                                                "customer", "status", "decided_at", "submitted_at", "due_on",
                                                "overdue", "proxy", "invoice_ref")}
                            for i in items if i["status"] in st]
        currencies = sorted({i["currency"] for i in items})
        return {"totals": t, "currencies": currencies,
                "ready_to_invoice": pick("accepted"), "awaiting_signoff": pick("submitted"),
                "ready_to_submit": pick("ready", "changes_requested"),
                "overdue": [x for x in pick("pending", "ready", "submitted", "changes_requested") if x["overdue"]]}

    @app.get("/api/commercials.csv")
    def portfolio_csv(c: Ctx = Depends(ctx)):
        c.require("billing.view")
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["customer", "deployment", "sow_reference", "milestone", "amount", "currency", "status",
                    "stage", "due_on", "submitted_at", "accepted_at", "accepted_by", "signed_off_by_proxy",
                    "customer_packet_hash", "record_position", "record_hash", "invoice_ref", "invoiced_at", "paid_at",
                    "milestone_id", "erp_id"])
        users = users_map(c)
        for r in portfolio_rows(c):
            pk = current_packet(conn, r["id"])
            accepted = r["status"] in EARNED
            w.writerow([r["customer"] or "", r["deployment"], r["reference"], r["name"], r["amount"], r["currency"],
                        r["status"], r["stage"] or "", r["due_on"] or "", r["submitted_at"] or "",
                        r["decided_at"] if accepted else "", users.get(r["decided_by"], "") if accepted else "",
                        "yes" if r["proxy"] else "", pk["customer_hash"] if pk else "",
                        pk["audit_seq"] if pk else "", pk["audit_hash"] if pk else "", r["invoice_ref"] or "",
                        r["invoiced_at"] or "", r["paid_at"] or "", r["id"], r["external_id"] or ""])
        with db.tx(conn):
            c.log("billing.export", "commercials.csv", {"rows": buf.getvalue().count("\n") - 1})
        return Response(buf.getvalue(), media_type="text/csv",
                        headers={"Content-Disposition": 'attachment; filename="fieldwork-billing.csv"'})

    # ----------------------------------------------------------- set up

    class MilestoneIn(BaseModel):
        name: str = Field(min_length=1, max_length=160)
        amount: float = Field(default=0, ge=0, le=1e10)
        stage: str | None = None
        criteria: str = Field(default="", max_length=2000)
        due_on: str | None = None

    class SowIn(BaseModel):
        reference: str = Field(default="", max_length=120)
        total_value: float = Field(default=0, ge=0, le=1e11)
        currency: str = "USD"
        signed_on: str | None = None
        notes: str = Field(default="", max_length=4000)
        milestones: list[MilestoneIn] = Field(default=[], max_length=100)

    def insert_ms(c, s, m: MilestoneIn, pos: int) -> str:
        mid = new_id("ms")
        ts = audit.now()
        conn.execute("INSERT INTO milestones (id, tenant_id, deployment_id, sow_id, name, amount, stage, criteria,"
                     " due_on, position, status, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (mid, c.tenant_id, s["deployment_id"], s["id"], m.name.strip(), round(m.amount, 2),
                      check_stage(c, m.stage), m.criteria.strip(), check_day(m.due_on, "due_on"), pos, "pending",
                      ts, ts))
        return mid

    @app.post("/api/deployments/{dep_id}/sows", status_code=201)
    def add_sow(dep_id: str, body: SowIn, c: Ctx = Depends(ctx)):
        c.deployment(dep_id)
        c.require_on("sow.edit", dep_id)
        cur = body.currency.upper().strip()
        if cur not in CURRENCIES:
            raise HTTPException(422, f"currency must be one of {', '.join(CURRENCIES)}")
        total = body.total_value or round(sum(m.amount for m in body.milestones), 2)
        sid = new_id("sow")
        ts = audit.now()
        with db.tx(conn):
            conn.execute("INSERT INTO sows (id, tenant_id, deployment_id, reference, total_value, currency, signed_on,"
                         " notes, created_by, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                         (sid, c.tenant_id, dep_id, body.reference.strip(), total, cur,
                          check_day(body.signed_on, "signed_on"), body.notes, c.uid, ts, ts))
            s = _sow(conn, sid)
            ids = [insert_ms(c, s, m, i) for i, m in enumerate(body.milestones)]
            c.log("sow.create", sid, {"deployment": dep_id, "reference": body.reference, "total_value": total,
                                      "currency": cur, "milestones": len(ids)})
            sync_ready(c, dep_id)
        allocated = round(sum(m.amount for m in body.milestones), 2)
        return {"id": sid, "milestones": ids, "total_value": total, "unallocated": round(total - allocated, 2)}

    class SowPatch(BaseModel):
        reference: str | None = Field(default=None, max_length=120)
        total_value: float | None = Field(default=None, ge=0, le=1e11)
        signed_on: str | None = None
        notes: str | None = Field(default=None, max_length=4000)

    @app.patch("/api/sows/{sid}")
    def patch_sow(sid: str, body: SowPatch, c: Ctx = Depends(ctx)):
        s = sow_for(c, sid)
        c.require_on("sow.edit", s["deployment_id"])
        ch = {k: v for k, v in body.model_dump().items() if v is not None}
        if "signed_on" in ch:
            ch["signed_on"] = check_day(ch["signed_on"], "signed_on")
        if not ch:
            return {"changed": []}
        with db.tx(conn):
            conn.execute(f"UPDATE sows SET {', '.join(k + '=?' for k in ch)}, updated_at=? WHERE id=?",
                         (*ch.values(), audit.now(), sid))
            c.log("sow.update", sid, {k: v for k, v in ch.items() if k != "notes"})
        return {"changed": list(ch)}

    @app.post("/api/sows/{sid}/milestones", status_code=201)
    def add_milestone(sid: str, body: MilestoneIn, c: Ctx = Depends(ctx)):
        s = sow_for(c, sid)
        c.require_on("sow.edit", s["deployment_id"])
        pos = conn.execute("SELECT COALESCE(MAX(position), -1) p FROM milestones WHERE sow_id=?",
                           (sid,)).fetchone()["p"] + 1
        with db.tx(conn):
            mid = insert_ms(c, s, body, pos)
            c.log("milestone.create", mid, {"sow": sid, "name": body.name, "amount": body.amount,
                                            "stage": body.stage})
            sync_ready(c, s["deployment_id"])
        return {"id": mid}

    class MilestonePatch(BaseModel):
        name: str | None = Field(default=None, min_length=1, max_length=160)
        amount: float | None = Field(default=None, ge=0, le=1e10)
        stage: str | None = None
        criteria: str | None = Field(default=None, max_length=2000)
        due_on: str | None = None
        position: int | None = Field(default=None, ge=0, le=1000)

    @app.patch("/api/milestones/{mid}")
    def patch_milestone(mid: str, body: MilestonePatch, c: Ctx = Depends(ctx)):
        ms = ms_for(c, mid)
        c.require_on("sow.edit", ms["deployment_id"])
        ch = body.model_dump(exclude_unset=True)
        locked = {"name", "amount", "criteria", "stage"} & set(ch)
        if locked and ms["status"] not in EDITABLE:
            raise HTTPException(409, "the amount, name, stage and criteria lock once a milestone is submitted")
        if "stage" in ch:
            ch["stage"] = check_stage(c, ch["stage"])
        if "due_on" in ch:
            ch["due_on"] = check_day(ch["due_on"], "due_on")
        if "amount" in ch and ch["amount"] is not None:
            ch["amount"] = round(ch["amount"], 2)
        ch = {k: v for k, v in ch.items() if v is not None or k in ("stage", "due_on")}
        if not ch:
            return {"changed": []}
        with db.tx(conn):
            if "stage" in ch and ms["status"] == "ready" and ch["stage"] != ms["stage"]:
                ch["status"], ch["ready_at"] = "pending", None
            conn.execute(f"UPDATE milestones SET {', '.join(k + '=?' for k in ch)}, updated_at=? WHERE id=?",
                         (*ch.values(), audit.now(), mid))
            c.log("milestone.update", mid, {k: v for k, v in ch.items() if k != "criteria"}
                  | ({"criteria": "edited"} if "criteria" in ch else {}))
            sync_ready(c, ms["deployment_id"])
        return {"changed": [k for k in ch if k not in ("status", "ready_at")]}

    @app.delete("/api/milestones/{mid}")
    def delete_milestone(mid: str, c: Ctx = Depends(ctx)):
        ms = ms_for(c, mid)
        c.require_on("sow.edit", ms["deployment_id"])
        if ms["status"] not in ("pending", "ready"):
            raise HTTPException(409, "a milestone that's been submitted stays on the record")
        with db.tx(conn):
            conn.execute("DELETE FROM milestones WHERE id=?", (mid,))
            c.log("milestone.delete", mid, {"name": ms["name"], "amount": ms["amount"]})
        return {"ok": True}

    # ----------------------------------------------------------- sign-off

    class NoteIn(BaseModel):
        note: str = Field(default="", max_length=2000)

    @app.post("/api/milestones/{mid}/submit")
    def submit(mid: str, body: NoteIn, c: Ctx = Depends(ctx)):
        ms = ms_for(c, mid)
        c.require_on("milestone.submit", ms["deployment_id"])
        ok = ms["status"] in ("ready", "changes_requested") or (ms["status"] == "pending" and not ms["stage"])
        if not ok:
            raise HTTPException(409, "this milestone isn't ready: its stage isn't done yet" if ms["status"] == "pending"
                                else f"this milestone is already {ms['status'].replace('_', ' ')}")
        ts = audit.now()
        with db.tx(conn):
            conn.execute("UPDATE milestones SET status='submitted', submitted_at=?, submitted_by=?, decided_at=NULL,"
                         " decided_by=NULL, decision_note='', updated_at=? WHERE id=?", (ts, c.uid, ts, mid))
            res = freeze(conn, c.tenant_id, c.cfg, ms, c.uid, body.note.strip(), "milestone.submit")
            _emit(conn, c.cfg, c.tenant_id, "milestone.submitted", ms, c.user["name"])
        return {"status": "submitted", **res}

    def packet_view(c, ms, pk) -> dict:
        internal = not is_customer_side(c, ms["deployment_id"]) and c.can_on("billing.view", ms["deployment_id"])
        body = pk["internal_json"] if internal else pk["customer_json"]
        h = pk["internal_hash"] if internal else pk["customer_hash"]
        packet = json.loads(body)
        intact = fingerprint(packet) == h
        entry = conn.execute("SELECT detail_json FROM audit WHERE tenant_id=? AND seq=?",
                             (c.tenant_id, pk["audit_seq"])).fetchone() if pk["audit_seq"] else None
        on_record = bool(entry and json.loads(entry["detail_json"]).get("customer_packet") == pk["customer_hash"])
        return {"packet": packet, "hash": h, "customer_hash": pk["customer_hash"], "audience": packet["audience"],
                "intact": intact and on_record,
                "receipt": {"milestone_id": ms["id"], "customer_packet_hash": pk["customer_hash"],
                            "record_position": pk["audit_seq"], "record_hash": pk["audit_hash"],
                            "frozen_at": pk["created_at"]}}

    @app.get("/api/milestones/{mid}/packet")
    def packet(mid: str, c: Ctx = Depends(ctx)):
        ms = ms_for(c, mid)
        pk = current_packet(conn, mid)
        if not pk:
            raise HTTPException(404, "nothing has been submitted for this milestone yet")
        return packet_view(c, ms, pk)

    class AcceptIn(BaseModel):
        packet_hash: str | None = None
        note: str = Field(default="", max_length=2000)

    def decide(c, ms, status: str, note: str, packet_hash: str | None, proxy: bool) -> dict:
        pk = current_packet(conn, ms["id"])
        if packet_hash and (not pk or packet_hash != pk["customer_hash"]):
            raise HTTPException(409, "the evidence was resubmitted since you opened it: reload and review it again")
        ts = audit.now()
        conn.execute("UPDATE milestones SET status=?, decided_at=?, decided_by=?, decision_note=?, proxy=?,"
                     " updated_at=? WHERE id=?", (status, ts, c.uid, note, 1 if proxy else 0, ts, ms["id"]))
        action = {"accepted": "milestone.accept", "changes_requested": "milestone.request_changes"}[status]
        c.log(action, ms["id"], {"deployment": ms["deployment_id"], "amount": ms["amount"],
                                 "customer_packet": pk["customer_hash"] if pk else None, "note": note,
                                 "on_behalf_of_customer": proxy})
        anc = anchor(conn, c.tenant_id, action, ms["id"])
        event = "milestone.accepted" if status == "accepted" else "milestone.changes_requested"
        _emit(conn, c.cfg, c.tenant_id, event, ms, c.user["name"], note=note, proxy=proxy,
              customer_packet_hash=pk["customer_hash"] if pk else None,
              record_position=anc.get("seq"), record_hash=anc.get("hash"))
        if status == "accepted":
            from .connect import billing
            billing.on_accepted(conn, c.tenant_id, ms["id"])
        return {"status": status, "anchor": anc, "customer_packet_hash": pk["customer_hash"] if pk else None}

    @app.post("/api/milestones/{mid}/accept")
    def accept(mid: str, body: AcceptIn, c: Ctx = Depends(ctx)):
        ms = ms_for(c, mid)
        c.require_on("milestone.accept", ms["deployment_id"])
        if ms["status"] != "submitted":
            raise HTTPException(409, "only a submitted milestone can be signed off")
        with db.tx(conn):
            return decide(c, ms, "accepted", body.note.strip(), body.packet_hash, proxy=False)

    @app.post("/api/milestones/{mid}/request-changes")
    def request_changes(mid: str, body: AcceptIn, c: Ctx = Depends(ctx)):
        ms = ms_for(c, mid)
        c.require_on("milestone.accept", ms["deployment_id"])
        if ms["status"] != "submitted":
            raise HTTPException(409, "only a submitted milestone can be sent back")
        if not body.note.strip():
            raise HTTPException(422, "say what needs to change")
        with db.tx(conn):
            return decide(c, ms, "changes_requested", body.note.strip(), body.packet_hash, proxy=False)

    class ProxyIn(BaseModel):
        note: str = Field(min_length=3, max_length=2000)

    @app.post("/api/milestones/{mid}/record-acceptance")
    def record_acceptance(mid: str, body: ProxyIn, c: Ctx = Depends(ctx)):
        """The customer signed off outside Fieldwork (email, a signed form). Recorded as such, with the reason."""
        ms = ms_for(c, mid)
        c.require_on("sow.edit", ms["deployment_id"])
        if ms["status"] not in ("submitted", "ready", "changes_requested") and not (
                ms["status"] == "pending" and not ms["stage"]):
            raise HTTPException(409, f"this milestone is {ms['status'].replace('_', ' ')}")
        with db.tx(conn):
            if ms["status"] != "submitted":
                ts = audit.now()
                conn.execute("UPDATE milestones SET submitted_at=?, submitted_by=?, updated_at=? WHERE id=?",
                             (ts, c.uid, ts, mid))
                freeze(conn, c.tenant_id, c.cfg, ms, c.uid, body.note.strip(), "milestone.submit")
            return decide(c, ms, "accepted", body.note.strip(), None, proxy=True)

    class InvoiceIn(BaseModel):
        invoice_ref: str = Field(default="", max_length=120)
        on: str | None = None

    def billing_step(c, mid: str, frm: tuple, to: str, body: InvoiceIn) -> dict:
        ms = ms_for(c, mid)
        c.require_on("sow.edit", ms["deployment_id"])
        if ms["status"] not in frm:
            raise HTTPException(409, f"this milestone is {ms['status'].replace('_', ' ')}")
        ts = audit.now()
        when = check_day(body.on, "on") or ts[:10]
        with db.tx(conn):
            if to == "invoiced":
                conn.execute("UPDATE milestones SET status='invoiced', invoice_ref=?, invoiced_at=?, updated_at=?"
                             " WHERE id=?", (body.invoice_ref.strip() or ms["invoice_ref"], when, ts, mid))
            else:
                conn.execute("UPDATE milestones SET status='paid', paid_at=?, invoiced_at=COALESCE(invoiced_at, ?),"
                             " updated_at=? WHERE id=?", (when, when, ts, mid))
            c.log(f"milestone.{to}", mid, {"deployment": ms["deployment_id"], "amount": ms["amount"],
                                           "invoice_ref": body.invoice_ref or ms["invoice_ref"], "on": when})
        return {"status": to}

    @app.post("/api/milestones/{mid}/invoiced")
    def mark_invoiced(mid: str, body: InvoiceIn, c: Ctx = Depends(ctx)):
        return billing_step(c, mid, ("accepted",), "invoiced", body)

    @app.post("/api/milestones/{mid}/paid")
    def mark_paid(mid: str, body: InvoiceIn, c: Ctx = Depends(ctx)):
        return billing_step(c, mid, ("accepted", "invoiced"), "paid", body)

    # ----------------------------------------------------------- the record's anchors

    @app.get("/api/audit/anchors")
    def anchors(limit: int = 50, c: Ctx = Depends(ctx)):
        c.require("audit.read")
        return [_row_dict(r) for r in conn.execute(
            "SELECT seq, hash, reason, subject, at FROM audit_anchors WHERE tenant_id=? ORDER BY id DESC LIMIT ?",
            (c.tenant_id, max(1, min(limit, 500))))]

    class AnchorIn(BaseModel):
        seq: int = Field(ge=1)
        hash: str = Field(min_length=10, max_length=80)

    @app.post("/api/audit/verify-anchor")
    def check_anchor(body: AnchorIn, c: Ctx = Depends(ctx)):
        """Anyone in the workspace, customers included, can check a receipt against the record."""
        return verify_anchor(conn, c.tenant_id, body.seq, body.hash.strip())


def report_footer(conn, tenant_id: str) -> str:
    head = conn.execute("SELECT seq, hash FROM audit WHERE tenant_id=? ORDER BY seq DESC LIMIT 1",
                        (tenant_id,)).fetchone()
    if not head:
        return ""
    return (f"\n---\nRecord fingerprint: position {head['seq']} · `{head['hash']}` · "
            f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}. Keep this line: anyone in the workspace can "
            "check it against the record later.\n")
