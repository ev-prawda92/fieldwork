"""The ball: every open task is in exactly one person's hands, and people move it.

  Owner     the task's assignee. Owning a task means the ball comes home to you.
  Holder    whoever has the ball right now. Usually the owner; after a pass, the person it went to.

  Pass      "I need you for this step." The ball goes to someone, and comes back when they send it back.
            Passes stack: A passes to B, B passes to C; C sends it back to B, B sends it back to A.
  Send back The holder is done with their part; the ball returns to whoever passed it to them.
  Call back The owner (or a lead) pulls the ball home, closing every open pass.
  Hand off  "This is yours now." Ownership moves; any open passes close and the new owner holds it.

Nothing here gates the work, and status is untouched: a task can be in progress while the ball is
out. Every move is a row in ball_passes, so where a task's time went, and who held it, is part of
the record without anyone filling anything in. When the ball sits with someone on the customer
side, the delay ledger gets a span proposed to the customer, closed the moment the ball comes back.
"""


from datetime import datetime, timedelta, timezone

from fastapi import Depends, HTTPException
from pydantic import BaseModel, Field

from . import audit, ops

CUSTOMER_KEY = "ball:{}"


# ------------------------------------------------------------------ helpers

def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        d = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def hours_since(ts: str | None) -> float | None:
    d = _parse(ts)
    return round((datetime.now(timezone.utc) - d).total_seconds() / 3600, 1) if d else None


def holder_of(task) -> str | None:
    return task["holder_id"] or task["assignee_id"]


def is_member(conn, dep_id: str, user_id: str) -> bool:
    return conn.execute("SELECT 1 FROM deployment_members WHERE deployment_id=? AND user_id=?",
                        (dep_id, user_id)).fetchone() is not None


def _scope_ok(conn, cfg: dict, action: str, role: str, dep_id: str, user_id: str) -> bool:
    s = cfg["permissions"].get(action, {}).get(role)
    return s == "all" or (s == "own" and is_member(conn, dep_id, user_id))


def can_see_task(conn, cfg: dict, task, user) -> bool:
    """Could this person (not necessarily the caller) see this task?"""
    if not user or not user["active"]:
        return False
    if not _scope_ok(conn, cfg, "deployment.view", user["role"], task["deployment_id"], user["id"]):
        return False
    return task["visibility"] == "shared" or _scope_ok(
        conn, cfg, "task.view_internal", user["role"], task["deployment_id"], user["id"])


def customer_side(cfg: dict, role: str) -> bool:
    """Someone who only ever sees shared work is on the customer's side of the table."""
    return cfg["permissions"].get("task.view_internal", {}).get(role) is None


def _name(conn, uid: str | None) -> str | None:
    r = conn.execute("SELECT name FROM users WHERE id=?", (uid,)).fetchone() if uid else None
    return r["name"] if r else None


def open_passes(conn, tenant_id: str, task_id: str) -> list:
    """Open passes on a task, newest first. The first one is the pass the current holder got."""
    return conn.execute("SELECT * FROM ball_passes WHERE tenant_id=? AND task_id=? AND back_at IS NULL"
                        " ORDER BY id DESC", (tenant_id, task_id)).fetchall()


def describe(conn, task) -> dict:
    """Where the ball is on one task, for task rows everywhere."""
    holder = holder_of(task)
    top = open_passes(conn, task["tenant_id"], task["id"])[:1] if task["holder_id"] else []
    p = top[0] if top else None
    since = task["ball_since"] or task["created_at"]
    return {"holder_id": holder, "holder": _name(conn, holder), "since": since,
            "held_hours": hours_since(since) if task["status"] != "done" else None,
            "out": bool(task["holder_id"]) and task["holder_id"] != task["assignee_id"],
            "from_id": p["from_id"] if p else None, "from": _name(conn, p["from_id"]) if p else None,
            "note": p["note"] if p else ""}


# --------------------------------------------------------------- the moves

def _set_holder(conn, task, holder: str | None, ts: str) -> None:
    """None means the ball is home with the owner."""
    h = None if holder == task["assignee_id"] else holder
    conn.execute("UPDATE tasks SET holder_id=?, ball_since=?, updated_at=? WHERE id=?", (h, ts, ts, task["id"]))


def _sync_customer_delay(conn, tenant_id: str, tenant_name: str, cfg: dict, task, holder: str | None,
                         note: str = "") -> None:
    """Ball with the customer: a delay span proposed to them. Ball back: the span closes."""
    key = CUSTOMER_KEY.format(task["id"])
    u = conn.execute("SELECT * FROM users WHERE id=?", (holder,)).fetchone() if holder else None
    with_customer = bool(u) and holder != task["assignee_id"] and customer_side(cfg, u["role"])
    if not with_customer:
        ops.close_span(conn, tenant_id, key)
        return
    dep = conn.execute("SELECT * FROM deployments WHERE id=?", (task["deployment_id"],)).fetchone()
    sid, created = ops.reopen_or_open(
        conn, tenant_id, dep, signal="waiting_on_customer", dedupe_key=key, owner_hint="customer",
        reason=note or f"Waiting on {u['name']}", tenant_name=tenant_name, stage=task["stage"],
        evidence=f"Ball passed to {u['name']} on “{task['title']}”")
    if created:
        conn.execute("UPDATE delays SET proposal_basis=? WHERE id=? AND status='open'",
                     ("the ball is with someone on the customer side", sid))
        audit.record(conn, tenant_id, "rule:ball", "delay.open", sid,
                     {"deployment": task["deployment_id"], "task": task["id"], "signal": "waiting_on_customer",
                      "proposed_owner": "customer", "reason": note or f"Waiting on {u['name']}"})


def settle(conn, tenant_id: str, tenant_name: str, cfg: dict, task, actor: str, how: str) -> int:
    """Close every open pass and bring the ball home to the owner. Inside the caller's transaction.
    Used when a task is done, handed off or reassigned, and when someone holding it leaves."""
    ts = audit.now()
    r = conn.execute("UPDATE ball_passes SET back_at=?, back_by=?, back_how=? WHERE tenant_id=? AND task_id=?"
                     " AND back_at IS NULL", (ts, actor, how, tenant_id, task["id"]))
    fresh = conn.execute("SELECT * FROM tasks WHERE id=?", (task["id"],)).fetchone()
    if fresh["holder_id"] or (r.rowcount or 0):
        conn.execute("UPDATE tasks SET holder_id=NULL, ball_since=? WHERE id=?", (ts, task["id"]))
    ops.close_span(conn, tenant_id, CUSTOMER_KEY.format(task["id"]))
    return r.rowcount or 0


# ------------------------------------------------------------------- routes

def register(app, d) -> None:
    conn, ctx, Ctx, emit = d.conn, d.ctx, d.Ctx, d.emit

    def load(c: Ctx, task_id: str):
        t = conn.execute("SELECT * FROM tasks WHERE id=? AND tenant_id=?", (task_id, c.tenant_id)).fetchone()
        if not t:
            raise HTTPException(404, "task not found")
        c.deployment(t["deployment_id"])
        if t["visibility"] != "shared" and not c.can_on("task.view_internal", t["deployment_id"]):
            raise HTTPException(404, "task not found")
        return t

    def catcher(c: Ctx, t, uid: str):
        u = conn.execute("SELECT * FROM users WHERE id=? AND tenant_id=?", (uid, c.tenant_id)).fetchone()
        if not u or not u["active"]:
            raise HTTPException(404, "person not found")
        if not can_see_task(conn, c.cfg, t, u):
            why = ("share the task with the customer first" if t["visibility"] != "shared"
                   and customer_side(c.cfg, u["role"]) else "add them to this deployment first")
            raise HTTPException(422, f"{u['name']} can't see this task: {why}")
        return u

    def can_steer(c: Ctx, t) -> bool:
        """The owner, or a lead who can update anyone's tasks."""
        return t["assignee_id"] == c.uid or c.can_on("task.update_any", t["deployment_id"])

    def notify(c: Ctx, event: str, t, to_id: str, note: str) -> None:
        emit(c, event, t["deployment_id"], title=t["title"], task_id=t["id"], to=_name(conn, to_id),
             to_id=to_id, note=note)

    class PassIn(BaseModel):
        to: str
        note: str = Field(default="", max_length=300)

    class BackIn(BaseModel):
        note: str = Field(default="", max_length=300)

    @app.post("/api/tasks/{task_id}/pass")
    def pass_ball(task_id: str, body: PassIn, c: Ctx = Depends(ctx)):
        """Pass the ball for a step. It comes back when they send it back."""
        t = load(c, task_id)
        if t["status"] == "done":
            raise HTTPException(422, "this task is done; reopen it to pass the ball")
        if not t["assignee_id"]:
            raise HTTPException(422, "give the task an owner first, so the ball has somewhere to come home to")
        holder = holder_of(t)
        if c.uid != holder and not can_steer(c, t):
            raise HTTPException(403, f"the ball is with {_name(conn, holder)}; only they or the task's owner can pass it")
        to = catcher(c, t, body.to)
        if to["id"] == holder:
            raise HTTPException(422, f"{to['name']} already has the ball")
        note = body.note.strip()
        ts = audit.now()
        with conn.tx():
            top = (open_passes(conn, c.tenant_id, t["id"]) or [None])[0]
            frm, how = holder, "pass"
            if c.uid != holder and top:
                # The owner or a lead redirects a ball someone else holds: that pass closes, and the
                # ball still comes back to whoever passed it out in the first place.
                frm, how = top["from_id"], "redirect"
                conn.execute("UPDATE ball_passes SET back_at=?, back_by=?, back_how=? WHERE id=?",
                             (ts, c.uid, "redirected", top["id"]))
            elif top and to["id"] == top["from_id"]:
                # Passing it straight back to whoever passed it to you is sending it back.
                frm, how = top["from_id"], "return"
                conn.execute("UPDATE ball_passes SET back_at=?, back_by=?, back_how=?, back_note=? WHERE id=?",
                             (ts, c.uid, "returned", note, top["id"]))
            if to["id"] != frm:
                conn.execute("INSERT INTO ball_passes (tenant_id, deployment_id, task_id, kind, from_id, to_id,"
                             " note, at) VALUES (?,?,?,?,?,?,?,?)",
                             (c.tenant_id, t["deployment_id"], t["id"], "pass", frm, to["id"], note, ts))
            _set_holder(conn, t, to["id"], ts)
            _sync_customer_delay(conn, c.tenant_id, c.tenant_name, c.cfg, t, to["id"], note)
            c.log("ball.return" if how == "return" else "ball.pass", t["id"],
                  {"from": holder, "to": to["id"], "note": note, "by": c.uid, "how": how})
            if to["id"] != c.uid:
                notify(c, "task.returned" if how == "return" else "task.passed", t, to["id"], note)
        return {"holder_id": to["id"], "holder": to["name"]}

    @app.post("/api/tasks/{task_id}/return")
    def return_ball(task_id: str, body: BackIn, c: Ctx = Depends(ctx)):
        """The holder sends the ball back to whoever passed it to them. The owner (or a lead)
        calling it back while someone else holds it brings it all the way home."""
        t = load(c, task_id)
        holder = holder_of(t)
        if not t["holder_id"] or holder == t["assignee_id"]:
            raise HTTPException(422, "the ball is already home with the task's owner")
        note = body.note.strip()
        ts = audit.now()
        with conn.tx():
            if c.uid == holder:
                top = open_passes(conn, c.tenant_id, t["id"])[:1]
                back_to = top[0]["from_id"] if top else t["assignee_id"]
                if top:
                    conn.execute("UPDATE ball_passes SET back_at=?, back_by=?, back_how=?, back_note=? WHERE id=?",
                                 (ts, c.uid, "returned", note, top[0]["id"]))
                # Someone who left, or no longer sees the task, can't catch it: it goes home instead.
                catcher_row = conn.execute("SELECT * FROM users WHERE id=?", (back_to,)).fetchone()
                if back_to != t["assignee_id"] and not can_see_task(conn, c.cfg, t, catcher_row):
                    settle(conn, c.tenant_id, c.tenant_name, c.cfg, t, c.uid, "returned")
                    back_to = t["assignee_id"]
                else:
                    _set_holder(conn, t, back_to, ts)
                    _sync_customer_delay(conn, c.tenant_id, c.tenant_name, c.cfg, t, back_to)
                c.log("ball.return", t["id"], {"from": c.uid, "to": back_to, "note": note})
                if back_to != c.uid:
                    notify(c, "task.returned", t, back_to, note)
            elif can_steer(c, t):
                settle(conn, c.tenant_id, c.tenant_name, c.cfg, t, c.uid, "called_back")
                back_to = t["assignee_id"]
                c.log("ball.call_back", t["id"], {"from": holder, "to": back_to, "note": note})
            else:
                raise HTTPException(403, f"the ball is with {_name(conn, holder)}; only they can send it back")
        return {"holder_id": back_to, "holder": _name(conn, back_to)}

    @app.post("/api/tasks/{task_id}/handoff")
    def hand_off(task_id: str, body: PassIn, c: Ctx = Depends(ctx)):
        """'This is yours now.' The task's owner can hand it to anyone who can see it."""
        t = load(c, task_id)
        if t["assignee_id"] != c.uid:
            c.require_on("task.assign", t["deployment_id"])
        to = catcher(c, t, body.to)
        if to["id"] == t["assignee_id"]:
            raise HTTPException(422, f"{to['name']} already owns it")
        note = body.note.strip()
        ts = audit.now()
        with conn.tx():
            settle(conn, c.tenant_id, c.tenant_name, c.cfg, t, c.uid, "handed_off")
            conn.execute("INSERT INTO ball_passes (tenant_id, deployment_id, task_id, kind, from_id, to_id, note, at,"
                         " back_at, back_by, back_how) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                         (c.tenant_id, t["deployment_id"], t["id"], "handoff", t["assignee_id"] or c.uid, to["id"],
                          note, ts, ts, c.uid, "handed_off"))
            conn.execute("UPDATE tasks SET assignee_id=?, holder_id=NULL, ball_since=?, updated_at=? WHERE id=?",
                         (to["id"], ts, ts, t["id"]))
            c.log("ball.handoff", t["id"], {"from": t["assignee_id"], "to": to["id"], "note": note})
            c.log("task.update", t["id"], {"assignee_id": to["id"]})
            emit(c, "task.assigned", t["deployment_id"], title=t["title"], assignee=to["name"], task_id=t["id"],
                 assignee_id=to["id"])
        return {"assignee_id": to["id"], "assignee": to["name"]}

    @app.get("/api/tasks/{task_id}/catchers")
    def catchers(task_id: str, c: Ctx = Depends(ctx)):
        """Who could take the ball on this task: everyone active who can see it."""
        t = load(c, task_id)
        holder = holder_of(t)
        out = []
        for u in conn.execute("SELECT * FROM users WHERE tenant_id=? AND active=1 ORDER BY name", (c.tenant_id,)):
            if can_see_task(conn, c.cfg, t, u):
                out.append({"id": u["id"], "name": u["name"], "role": u["role"],
                            "on_deployment": is_member(conn, t["deployment_id"], u["id"]),
                            "holding": u["id"] == holder, "owner": u["id"] == t["assignee_id"],
                            "customer_side": customer_side(c.cfg, u["role"]), "you": u["id"] == c.uid})
        out.sort(key=lambda x: (not x["on_deployment"], x["customer_side"], x["name"]))
        return out

    @app.get("/api/tasks/{task_id}/ball")
    def trail(task_id: str, c: Ctx = Depends(ctx)):
        """Every move of this task's ball, oldest first."""
        t = load(c, task_id)
        rows = conn.execute("SELECT * FROM ball_passes WHERE tenant_id=? AND task_id=? ORDER BY id",
                            (c.tenant_id, t["id"])).fetchall()
        return {"ball": describe(conn, t),
                "moves": [{"kind": r["kind"], "from_id": r["from_id"], "from": _name(conn, r["from_id"]),
                           "to_id": r["to_id"], "to": _name(conn, r["to_id"]), "note": r["note"], "at": r["at"],
                           "back_at": r["back_at"], "back_how": r["back_how"], "back_note": r["back_note"],
                           "held_hours": round(((_parse(r["back_at"]) or datetime.now(timezone.utc))
                                                - _parse(r["at"])).total_seconds() / 3600, 1)
                           if r["kind"] == "pass" else None}
                          for r in rows]}

    def visible_tasks(c: Ctx, where: str, args: tuple) -> list:
        rows = conn.execute(f"SELECT * FROM tasks WHERE tenant_id=? AND status!='done' AND {where}",
                            (c.tenant_id, *args)).fetchall()
        out = []
        for t in rows:
            if not c.can_on("deployment.view", t["deployment_id"]):
                continue
            if t["visibility"] != "shared" and not c.can_on("task.view_internal", t["deployment_id"]):
                continue
            out.append(t)
        return out

    def row(t) -> dict:
        dep = conn.execute("SELECT name FROM deployments WHERE id=?", (t["deployment_id"],)).fetchone()
        return {"id": t["id"], "title": t["title"], "status": t["status"], "stage": t["stage"], "due": t["due"],
                "deployment_id": t["deployment_id"], "deployment": dep["name"] if dep else "",
                "assignee_id": t["assignee_id"], "assignee": _name(conn, t["assignee_id"]),
                "visibility": t["visibility"], "ball": describe(conn, t)}

    @app.get("/api/ball")
    def my_ball(c: Ctx = Depends(ctx)):
        """In your hands, and out with others waiting to come back to you."""
        holding = [row(t) for t in visible_tasks(
            c, "(holder_id=? OR (holder_id IS NULL AND assignee_id=?))", (c.uid, c.uid))]
        out_ids = {r["task_id"] for r in conn.execute(
            "SELECT task_id FROM ball_passes WHERE tenant_id=? AND from_id=? AND back_at IS NULL AND kind='pass'",
            (c.tenant_id, c.uid))}
        waiting = [row(t) for t in visible_tasks(c, "holder_id IS NOT NULL AND holder_id!=?", (c.uid,))
                   if t["id"] in out_ids]
        age = lambda x: -(x["ball"]["held_hours"] or 0)
        holding.sort(key=lambda x: (not x["ball"]["out"], age(x)))  # passed to you first, then oldest
        waiting.sort(key=age)
        return {"holding": holding, "waiting": waiting}

    @app.get("/api/deployments/{dep_id}/ball")
    def deployment_ball(dep_id: str, c: Ctx = Depends(ctx)):
        """Who has the ball on this deployment, and who held it longest in the last 30 days."""
        c.deployment(dep_id)
        tasks = visible_tasks(c, "deployment_id=?", (dep_id,))
        by: dict = {}
        for t in tasks:
            b = describe(conn, t)
            if not b["holder_id"]:
                continue
            p = by.setdefault(b["holder_id"], {"id": b["holder_id"], "name": b["holder"], "holding": 0,
                                               "passed_to_them": 0, "oldest_hours": 0.0})
            p["holding"] += 1
            p["passed_to_them"] += 1 if b["out"] else 0
            if b["out"]:  # only a passed ball counts as waiting on someone
                p["oldest_hours"] = max(p["oldest_hours"], b["held_hours"] or 0)
        cutoff = ops.iso(ops.now() - timedelta(days=30))
        held: dict = {}
        seen: dict = {}
        for r in conn.execute("SELECT * FROM ball_passes WHERE tenant_id=? AND deployment_id=? AND kind='pass'"
                              " AND (back_at IS NULL OR back_at>=?)", (c.tenant_id, dep_id, cutoff)):
            if r["task_id"] not in seen:
                vis = conn.execute("SELECT visibility FROM tasks WHERE id=?", (r["task_id"],)).fetchone()
                seen[r["task_id"]] = bool(vis) and (vis["visibility"] == "shared"
                                                     or c.can_on("task.view_internal", dep_id))
            if not seen[r["task_id"]]:
                continue
            start = max(_parse(r["at"]), _parse(cutoff))
            end = _parse(r["back_at"]) or datetime.now(timezone.utc)
            held[r["to_id"]] = held.get(r["to_id"], 0.0) + max(0.0, (end - start).total_seconds() / 3600)
        passes = conn.execute("SELECT COUNT(*) n FROM ball_passes WHERE tenant_id=? AND deployment_id=?"
                              " AND kind='pass' AND at>=?", (c.tenant_id, dep_id, cutoff)).fetchone()["n"]
        people = sorted(by.values(), key=lambda p: (-p["holding"], -p["oldest_hours"]))
        return {"holders": people, "passes_30d": passes,
                "held_30d": sorted([{"id": k, "name": _name(conn, k), "hours": round(v, 1)} for k, v in held.items()],
                                   key=lambda x: -x["hours"])}
