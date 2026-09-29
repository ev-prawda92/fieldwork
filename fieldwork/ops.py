"""Delivery operations: why deployments stall, who has room, and what's coming.

  Delay ledger   every stretch of waiting, with a proposed owner a person confirms
  Capacity       hours available, planned (allocations) and logged, per person per week
  Flags          raised by rules on a sweep or by people; taken, handed back, resolved
  Portfolio      the chain view: days in stage, delay owner, burn, conformance, KPIs
  Pipeline       opportunities, and whether the team can staff each one when it lands
  Approvals      actions an agent asks a person to approve
  Checklist      the go-live checklist on each deployment

The delay ledger follows three rules:

  * Signals propose, people decide. A blocked task, a blocked tracker issue or a
    stage past its target opens a span with a *proposed* owner. Confirming or
    reassigning it is what makes it evidence.
  * Some causes are provable from the signal itself (a model provider rate
    limiting you is the vendor's delay). Those settle by rule and carry zero
    weight, because a rule agreeing with itself is not evidence.
  * Weights: rule 0, batch 0.25, one at a time 1.0, so one batch click can't
    flip a default. Once a signal has MIN_SUPPORT weighted confirmations and
    MIN_SHARE of them name the same owner, that owner becomes the proposal for
    the workspace, and the proposal says why.

Everything here is computed from rows other parts of Fieldwork keep. Figures
that depend on something the workspace hasn't set (dates, budgets, hours) are
reported as None rather than guessed.
"""

import json
import math
import secrets
import statistics
import threading
from datetime import date, datetime, timedelta, timezone

from fastapi import Depends, HTTPException
from pydantic import BaseModel, Field

from . import audit, config, db, events

OWNERS = ("customer", "team", "model_vendor", "software_vendor")

SIGNALS = {
    "task_blocked": ("team", "Task blocked"),
    "tracker_blocked": ("team", "Tracker issue blocked"),
    "stage_overrun": ("team", "Stage past its target"),
    "waiting_on_customer": ("customer", "Waiting on a customer answer"),
    "security_review": ("customer", "Customer security review"),
    "change_board": ("customer", "Customer change board"),
    "model_access": ("model_vendor", "Model access or rate limits"),
    "vendor_error": ("software_vendor", "Third-party system limit"),
    "on_hold": ("customer", "Deployment on hold"),
    "manual": ("team", "Logged by a person"),
}
DEDUCIBLE = {"model_access"}
MANUAL_SIGNALS = ("manual", "waiting_on_customer", "security_review", "change_board",
                  "model_access", "vendor_error")

WEIGHT_INDIVIDUAL = 1.0
WEIGHT_BATCH = 0.25
MIN_SUPPORT = 6.0
MIN_SHARE = 0.6

BLOCKED_DAYS = 3
BURN_MARGIN = 0.20
SEVERITIES = ("low", "med", "high")
FLAG_OPEN = ("open", "owned", "returned")

PIPELINE_STAGES = ("lead", "qualified", "proposal", "commit", "won", "lost")
PIPELINE_LABELS = {"lead": "Lead", "qualified": "Qualified", "proposal": "Proposal", "commit": "Commit",
                   "won": "Won", "lost": "Lost"}
HORIZON_WEEKS = 26

CHECKLIST_DEFAULT = [
    "Rollback plan written and rehearsed",
    "Customer on-call named for launch week",
    "Monitoring and alerts pointed at the right channel",
    "Conformance suite green on production data",
    "Agent permissions reviewed with a human gate on writes",
    "Support handoff documented",
    "Success metric and comparison group agreed for the value study",
]


def now() -> datetime:
    return datetime.now(timezone.utc)


def parse_ts(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        dt = (datetime.fromisoformat(s.replace("Z", "+00:00")) if "T" in s
              else datetime.fromisoformat(s[:10] + "T00:00:00+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def parse_day(s: str | None) -> date | None:
    if not s:
        return None
    try:
        return date.fromisoformat(str(s)[:10])
    except ValueError:
        return None


def owner_labels(tenant_name: str) -> dict:
    return {"customer": "Customer", "team": tenant_name or "Our team",
            "model_vendor": "Model vendor", "software_vendor": "Software vendor"}


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(6)}"


# ================================================================ delay ledger

def span_days(row, at: datetime | None = None) -> float:
    start = parse_ts(row["started_at"])
    end = parse_ts(row["ended_at"]) or at or now()
    return round(max(0.0, (end - start).total_seconds() / 86400), 1)


def learn(conn, tenant_id: str, tenant_name: str = "") -> dict:
    """Priors: per signal, the owner people keep confirming, once there's enough of it."""
    rows = conn.execute("SELECT signal, confirmed_owner, SUM(weight) w FROM delays"
                        " WHERE tenant_id=? AND weight > 0 AND confirmed_owner IS NOT NULL"
                        " GROUP BY signal, confirmed_owner", (tenant_id,)).fetchall()
    counts: dict = {}
    for r in rows:
        counts.setdefault(r["signal"], {})[r["confirmed_owner"]] = float(r["w"])
    labels = owner_labels(tenant_name)

    def lab(o):
        return labels[o] if o == "team" else labels[o].lower()
    priors = {}
    for signal, dist in counts.items():
        support = round(sum(dist.values()), 2)
        if support < MIN_SUPPORT:
            continue
        owner, n = max(dist.items(), key=lambda kv: kv[1])
        share = n / support
        if share < MIN_SHARE:
            continue
        default = SIGNALS.get(signal, SIGNALS["manual"])[0]
        if owner == default:
            why = f"{support:g} weighted confirmations agree with the default ({share:.0%} {lab(owner)})."
        else:
            why = (f"Proposed as {lab(owner)} instead of {lab(default)}: of {support:g} weighted "
                   f"confirmations of this signal in your workspace, {share:.0%} were {lab(owner)}.")
        priors[signal] = {"signal": signal, "owner": owner, "default": default, "support": support,
                          "share": round(share, 3), "overrides_default": owner != default, "explain": why}
    return priors


def open_span(conn, tenant_id: str, dep, *, signal: str, dedupe_key: str, evidence: str = "",
              started_at: datetime | None = None, owner_hint: str | None = None, reason: str | None = None,
              tenant_name: str = "", stage: str | None = None) -> tuple[str, bool]:
    """Open a span unless one with this key exists. Returns (id, created). Call inside a transaction."""
    if signal not in SIGNALS:
        raise HTTPException(422, f"unknown signal {signal!r}")
    ex = conn.execute("SELECT id, ended_at FROM delays WHERE tenant_id=? AND dedupe_key=?",
                      (tenant_id, dedupe_key)).fetchone()
    if ex:
        return ex["id"], False
    default_owner, default_reason = SIGNALS[signal]
    stated = owner_hint in OWNERS
    owner = owner_hint if stated else default_owner
    basis = "stated by the person who logged it" if stated else f"default for {signal.replace('_', ' ')}"
    if not stated:
        prior = learn(conn, tenant_id, tenant_name).get(signal)
        if prior and prior["overrides_default"]:
            owner, basis = prior["owner"], prior["explain"]
    sid = new_id("dly")
    ts = audit.now()
    deduced = signal in DEDUCIBLE
    conn.execute(
        "INSERT INTO delays (id, tenant_id, deployment_id, stage, signal, started_at, ended_at, proposed_owner,"
        " proposed_reason, proposal_basis, evidence, dedupe_key, status, confirmed_owner, confirmed_reason,"
        " confirmed_by, confirmed_at, weight, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (sid, tenant_id, dep["id"], stage or dep["stage"], signal, iso(started_at or now()), None, owner,
         (reason or default_reason)[:300], basis[:300], (evidence or "")[:2000], dedupe_key[:200],
         "deduced" if deduced else "open", owner if deduced else None,
         (reason or default_reason)[:300] if deduced else None, "rule" if deduced else None,
         ts if deduced else None, 0.0, ts))
    return sid, True


def close_span(conn, tenant_id: str, dedupe_key: str, at: datetime | None = None) -> bool:
    r = conn.execute("UPDATE delays SET ended_at=? WHERE tenant_id=? AND dedupe_key=? AND ended_at IS NULL",
                     (iso(at or now()), tenant_id, dedupe_key))
    return (r.rowcount or 0) > 0


def reopen_or_open(conn, tenant_id: str, dep, **kw) -> tuple[str, bool]:
    """A task blocked, unblocked and blocked again is two spans, not one stretched over the gap."""
    key = kw["dedupe_key"]
    ex = conn.execute("SELECT id, ended_at FROM delays WHERE tenant_id=? AND dedupe_key=?",
                      (tenant_id, key)).fetchone()
    if ex and ex["ended_at"]:
        conn.execute("UPDATE delays SET dedupe_key=? WHERE id=?", (f"{key}#{ex['id']}", ex["id"]))
    return open_span(conn, tenant_id, dep, **kw)


def decide(conn, span, *, actor: str, owner: str | None, reason: str, batch: bool) -> dict:
    """Confirm the proposal (owner None or the same) or reassign it. Changing your mind
    amends the same row, so re-deciding can't inflate the ledger."""
    if span["status"] == "deduced":
        raise HTTPException(409, "this delay was settled by rule and carries no weight; nothing to confirm")
    if owner is not None and owner not in OWNERS:
        raise HTTPException(422, f"owner must be one of {', '.join(OWNERS)}")
    final = owner or span["proposed_owner"]
    status = "reassigned" if final != span["proposed_owner"] else "confirmed"
    weight = WEIGHT_BATCH if batch else WEIGHT_INDIVIDUAL
    conn.execute("UPDATE delays SET confirmed_owner=?, confirmed_reason=?, status=?, confirmed_by=?,"
                 " confirmed_at=?, weight=? WHERE id=?",
                 (final, (reason or span["proposed_reason"])[:300], status, actor, audit.now(), weight, span["id"]))
    return {"owner": final, "status": status, "weight": weight, "amended": span["status"] != "open"}


def delay_out(r, labels: dict, dep_names: dict | None = None, users: dict | None = None) -> dict:
    d = {k: r[k] for k in r.keys()}
    d["days"] = span_days(r)
    d["open"] = r["ended_at"] is None
    d["owner"] = r["confirmed_owner"] or r["proposed_owner"]
    d["owner_label"] = labels.get(d["owner"], d["owner"])
    d["proposed_owner_label"] = labels.get(r["proposed_owner"], r["proposed_owner"])
    d["signal_label"] = SIGNALS.get(r["signal"], ("", r["signal"]))[1]
    if dep_names is not None:
        d["deployment"] = dep_names.get(r["deployment_id"], "")
    if users is not None and r["confirmed_by"]:
        d["confirmed_by_name"] = users.get(r["confirmed_by"], r["confirmed_by"])
    return d


def grouped_queue(rows: list) -> list:
    """Group spans waiting for a decision by signal and proposed owner: ninety pending
    rows become a handful of decisions, each of which can still be split."""
    groups: dict = {}
    for r in rows:
        if r["status"] == "open":
            groups.setdefault((r["signal"], r["proposed_owner"]), []).append(r)
    out = []
    for (signal, owner), items in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        out.append({"signal": signal, "signal_label": SIGNALS.get(signal, ("", signal))[1],
                    "proposed_owner": owner, "count": len(items),
                    "days": round(sum(span_days(x) for x in items), 1), "span_ids": [x["id"] for x in items]})
    return out


def rollup(rows: list, labels: dict) -> dict:
    """Who owns the delay across confirmed (weighted) spans, plus what's still waiting."""
    by_owner = {o: 0.0 for o in OWNERS}
    reasons: dict = {o: {} for o in OWNERS}
    confirmed = [r for r in rows if r["status"] in ("confirmed", "reassigned", "deduced")]
    for r in confirmed:
        d = span_days(r)
        o = r["confirmed_owner"]
        by_owner[o] += d
        why = r["confirmed_reason"] or r["proposed_reason"]
        reasons[o][why] = reasons[o].get(why, 0.0) + d
    total = sum(by_owner.values())
    return {
        "confirmed_rows": len(confirmed),
        "unconfirmed_rows": sum(1 for r in rows if r["status"] == "open"),
        "unconfirmed_days": round(sum(span_days(r) for r in rows if r["status"] == "open"), 1),
        "total_days": round(total, 1),
        "owners": [{"owner": o, "label": labels[o], "days": round(by_owner[o], 1),
                    "share": round(by_owner[o] / total, 3) if total else 0.0,
                    "top_reason": max(reasons[o].items(), key=lambda kv: kv[1])[0] if reasons[o] else None}
                   for o in OWNERS],
    }


def on_task_status(conn, tenant_id: str, tenant_name: str, cfg: dict, task, new_status: str,
                   origin: str = "fieldwork") -> None:
    """Open a delay when a task is blocked, close it when it isn't. Inside the task's transaction."""
    old = task["status"]
    key = f"task:{task['id']}"
    if new_status == "blocked" and old != "blocked":
        dep = conn.execute("SELECT * FROM deployments WHERE id=?", (task["deployment_id"],)).fetchone()
        owner = task.get("waiting_on") if hasattr(task, "get") else None
        stated = owner in OWNERS
        if not stated and task["assignee_id"]:
            a = conn.execute("SELECT role FROM users WHERE id=?", (task["assignee_id"],)).fetchone()
            if a and cfg["permissions"].get("task.view_internal", {}).get(a["role"]) is None:
                owner = "customer"  # it's sitting with someone who only sees shared work: the customer
        signal = "tracker_blocked" if origin != "fieldwork" else "task_blocked"
        sid, created = reopen_or_open(
            conn, tenant_id, dep, signal=signal, dedupe_key=key, owner_hint=owner, tenant_name=tenant_name,
            evidence=f"“{task['title']}” marked blocked" + (f" in {origin.title()}" if origin != "fieldwork" else ""),
            stage=task["stage"])
        if created and stated:
            conn.execute("UPDATE delays SET proposal_basis=?, proposed_reason=COALESCE(NULLIF(?, ''), proposed_reason)"
                         " WHERE id=?", ("said by the person who marked it blocked", task.get("blocked_reason") or "", sid))
        elif created and owner == "customer":
            conn.execute("UPDATE delays SET proposal_basis=? WHERE id=? AND status='open'",
                         ("assigned to someone on the customer side", sid))
        if created:
            row = conn.execute("SELECT proposed_owner, proposed_reason FROM delays WHERE id=?", (sid,)).fetchone()
            audit.record(conn, tenant_id, f"tracker:{origin}" if origin != "fieldwork" else "rule:" + signal,
                         "delay.open", sid,
                         {"deployment": task["deployment_id"], "task": task["id"], "signal": signal,
                          "proposed_owner": row["proposed_owner"], "reason": row["proposed_reason"]})
    elif old == "blocked" and new_status != "blocked":
        close_span(conn, tenant_id, key)


def on_waiting_on(conn, task, owner: str, reason: str) -> None:
    """Someone said who a blocked task is waiting on: that becomes the delay's proposal, unless a person
    already decided it."""
    conn.execute("UPDATE delays SET proposed_owner=?, proposal_basis=?,"
                 " proposed_reason=COALESCE(NULLIF(?, ''), proposed_reason)"
                 " WHERE tenant_id=? AND dedupe_key=? AND ended_at IS NULL AND status='open'",
                 (owner, "said by the person who marked it blocked", reason, task["tenant_id"], f"task:{task['id']}"))




# ==================================================================== capacity

def week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())


def _dep_active(dep, ws: date) -> bool:
    s, e = parse_day(dep["start_on"]), parse_day(dep["end_on"])
    return (s is None or s <= ws + timedelta(days=6)) and (e is None or e >= ws)


def delivery_people(conn, cfg: dict, tenant_id: str) -> list:
    """Internal people who carry delivery hours: they see internal work and have hours set."""
    internal = {r for r, s in cfg["permissions"].get("task.view_internal", {}).items() if s}
    return [u for u in conn.execute("SELECT * FROM users WHERE tenant_id=? AND active=1 ORDER BY name", (tenant_id,))
            if u["role"] in internal and (u["weekly_hours"] or 0) > 0]


def logged_hours(conn, user_id: str, ws: date, dep_id: str | None = None) -> float:
    q = "SELECT COALESCE(SUM(hours), 0) h FROM time_entries WHERE user_id=? AND day>=? AND day<=?"
    args = [user_id, ws.isoformat(), (ws + timedelta(days=6)).isoformat()]
    if dep_id:
        q += " AND deployment_id=?"
        args.append(dep_id)
    return round(float(conn.execute(q, args).fetchone()["h"] or 0), 1)


def off_days(conn, user_id: str, ws: date) -> list[str]:
    """Weekdays in the week starting ws that the person is out (time off from the console or their calendar)."""
    we = ws + timedelta(days=4)
    days = set()
    for r in conn.execute("SELECT start_on, end_on FROM time_off WHERE user_id=? AND start_on<=? AND end_on>=?",
                          (user_id, we.isoformat(), ws.isoformat())):
        s, e = max(parse_day(r["start_on"]) or ws, ws), min(parse_day(r["end_on"]) or we, we)
        while s <= e:
            days.add(s.isoformat())
            s += timedelta(days=1)
    return sorted(days)


def person_week(conn, u, ws: date) -> dict:
    rows = conn.execute("SELECT d.id, d.name, d.start_on, d.end_on, m.allocation FROM deployment_members m"
                        " JOIN deployments d ON d.id=m.deployment_id WHERE m.user_id=?", (u["id"],)).fetchall()
    full = float(u["weekly_hours"] or 0)
    off = off_days(conn, u["id"], ws)
    weekly = round(full * (5 - len(off)) / 5, 1)
    allocs = [{"deployment_id": r["id"], "deployment": r["name"], "allocation": r["allocation"],
               "hours": round(r["allocation"] * full, 1)} for r in rows if r["allocation"] and _dep_active(r, ws)]
    planned = round(sum(a["hours"] for a in allocs), 1)
    return {"user_id": u["id"], "name": u["name"], "role": u["role"], "available": weekly,
            "planned": planned, "logged": logged_hours(conn, u["id"], ws),
            "free": round(max(0.0, weekly - planned), 1), "over": round(max(0.0, planned - weekly), 1),
            "allocations": allocs, "off_days": off, "full_week": full}


def team_week(conn, cfg: dict, tenant_id: str, ws: date) -> dict:
    people = [person_week(conn, u, ws) for u in delivery_people(conn, cfg, tenant_id)]
    avail = sum(p["available"] for p in people)
    planned = sum(p["planned"] for p in people)
    return {"week": ws.isoformat(), "people": people, "available": round(avail, 1),
            "planned": round(planned, 1), "logged": round(sum(p["logged"] for p in people), 1),
            "free": round(sum(p["free"] for p in people), 1),
            "utilization": round(planned / avail, 3) if avail else None}


def utilization(conn, cfg: dict, tenant_id: str, weeks: int = 4, today: date | None = None) -> float | None:
    """Logged over available hours for the last `weeks` full weeks. None if nobody logs time."""
    today = today or now().date()
    people = delivery_people(conn, cfg, tenant_id)
    ws0 = week_start(today) - timedelta(weeks=weeks)
    avail = logged = 0.0
    for i in range(weeks):
        ws = ws0 + timedelta(weeks=i)
        for u in people:
            avail += float(u["weekly_hours"] or 0) * (5 - len(off_days(conn, u["id"], ws))) / 5
            logged += logged_hours(conn, u["id"], ws)
    return round(logged / avail, 3) if avail and logged else None


def rolloff(conn, tenant_id: str, within_days: int = 60, today: date | None = None) -> list:
    today = today or now().date()
    rows = conn.execute("SELECT d.id, d.name, d.end_on, u.id uid, u.name uname, m.allocation"
                        " FROM deployment_members m JOIN deployments d ON d.id=m.deployment_id"
                        " JOIN users u ON u.id=m.user_id WHERE d.tenant_id=? AND d.end_on IS NOT NULL"
                        " AND m.allocation > 0 ORDER BY d.end_on", (tenant_id,)).fetchall()
    out = []
    for r in rows:
        e = parse_day(r["end_on"])
        if e and today <= e <= today + timedelta(days=within_days):
            out.append({"on": r["end_on"], "user": r["uname"], "user_id": r["uid"], "deployment_id": r["id"],
                        "deployment": r["name"], "frees": r["allocation"]})
    return out


def pipeline_capacity(conn, cfg: dict, tenant_id: str, opps: list, today: date | None = None) -> dict:
    """Check every open deal, most likely first, reserving hours as it goes; report
    collisions: deals that fit alone but not together."""
    today = today or now().date()
    cache: dict = {}

    def free(ws: date) -> float:
        if ws not in cache:
            cache[ws] = team_week(conn, cfg, tenant_id, ws)["free"]
        return cache[ws]

    def check(o, reserved: dict) -> dict:
        need = float(o["weekly_hours"] or 0)
        if need <= 0:
            return {"ok": None, "label": "no weekly hours estimate", "earliest": None}
        start = week_start(max(parse_day(o["expected_start"]) or today, today))

        def fits(ws):
            return all(free(ws + timedelta(weeks=k)) - reserved.get(ws + timedelta(weeks=k), 0.0) >= need
                       for k in range(4))
        if fits(start):
            return {"ok": True, "label": "staffable", "earliest": start.isoformat()}
        for i in range(1, HORIZON_WEEKS):
            ws = start + timedelta(weeks=i)
            if fits(ws):
                return {"ok": False, "label": f"team full until {ws.isoformat()}", "earliest": ws.isoformat()}
        return {"ok": False, "label": f"no room in {HORIZON_WEEKS} weeks", "earliest": None}

    ordered = sorted(opps, key=lambda o: (-(o["probability"] or 0), o["expected_start"] or "9999"))
    reserved: dict = {}
    checks, collisions = {}, []
    for o in ordered:
        alone = check(o, {})
        together = check(o, reserved)
        checks[o["id"]] = together
        if alone.get("ok") and not together.get("ok"):
            collisions.append({"opportunity": o["name"], "label": together["label"],
                               "earliest": together["earliest"]})
        if together.get("ok") and (o["probability"] or 0) >= 0.3:
            ws = date.fromisoformat(together["earliest"])
            for k in range(4):
                w = ws + timedelta(weeks=k)
                reserved[w] = reserved.get(w, 0.0) + float(o["weekly_hours"] or 0)
    return {"checks": checks, "collisions": collisions}


# =============================================================== stage states
# Deployments don't move in a straight line. Each stage carries its own state,
# more than one can be in progress, stages can be skipped or reopened, and a
# deployment can be put on hold. Nothing here gates work: states are a record
# of what happened, and the clock is the only thing they drive.

STAGE_STATES = ("not_started", "in_progress", "done", "skipped")


def _history_states(conn, cfg: dict, dep) -> dict:
    """States derived from the stage history, for deployments that predate per-stage state."""
    events = conn.execute("SELECT to_stage, at FROM stage_events WHERE deployment_id=? ORDER BY id",
                          (dep["id"],)).fetchall()
    keys = config.stage_keys(cfg)
    ci = keys.index(dep["stage"]) if dep["stage"] in keys else -1
    out = {}
    for i, k in enumerate(keys):
        entries = [j for j, e in enumerate(events) if e["to_stage"] == k]
        if i == ci:
            out[k] = {"state": "in_progress", "entered_at": events[entries[-1]]["at"] if entries else dep["created_at"],
                      "done_at": None}
        elif i < ci:
            if entries:
                j = entries[0]
                nxt = next((e["at"] for e in events[j + 1:] if e["to_stage"] != k), None)
                out[k] = {"state": "done", "entered_at": events[j]["at"], "done_at": nxt}
            else:
                out[k] = {"state": "skipped", "entered_at": None, "done_at": None}
        else:
            out[k] = {"state": "not_started", "entered_at": None, "done_at": None}
    return out


def stage_states(conn, cfg: dict, dep) -> dict:
    """{stage key: {state, entered_at, done_at}} in the workspace's stage order."""
    rows = {r["stage"]: {"state": r["state"], "entered_at": r["entered_at"], "done_at": r["done_at"]}
            for r in conn.execute("SELECT * FROM deployment_stages WHERE deployment_id=?", (dep["id"],))}
    base = rows or _history_states(conn, cfg, dep)
    return {k: dict(base.get(k) or {"state": "not_started", "entered_at": None, "done_at": None})
            for k in config.stage_keys(cfg)}


def primary_stage(cfg: dict, states: dict) -> str:
    """The stage a deployment is 'in': the furthest one in progress, else the next one not started."""
    keys = config.stage_keys(cfg)
    live = [k for k in keys if states[k]["state"] == "in_progress"]
    if live:
        return live[-1]
    return next((k for k in keys if states[k]["state"] == "not_started"), keys[-1])


def apply_states(conn, tenant_id: str, cfg: dict, dep, changes: dict, actor: str, note: str = "") -> str:
    """Write stage state changes, keep deployments.stage and the stage history in step. Inside a transaction.
    Returns the deployment's primary stage afterwards."""
    ts = audit.now()
    states = stage_states(conn, cfg, dep)
    for k, new in changes.items():
        cur = states[k]
        if new == cur["state"]:
            continue
        if new == "in_progress":
            cur.update(entered_at=ts, done_at=None)
        elif new == "done":
            cur.update(entered_at=cur["entered_at"] or ts, done_at=ts)
        elif new == "not_started":
            cur.update(entered_at=None, done_at=None)
        if cur["state"] == "in_progress":  # leaving: its overrun delay ends here
            close_span(conn, tenant_id, f"{dep['id']}:stage_overrun:{k}")
        cur["state"] = new
    for k, v in states.items():
        conn.execute("INSERT INTO deployment_stages (deployment_id, tenant_id, stage, state, entered_at, done_at,"
                     " updated_at) VALUES (?,?,?,?,?,?,?) ON CONFLICT (deployment_id, stage) DO UPDATE SET"
                     " state=excluded.state, entered_at=excluded.entered_at, done_at=excluded.done_at,"
                     " updated_at=excluded.updated_at",
                     (dep["id"], tenant_id, k, v["state"], v["entered_at"], v["done_at"], ts))
    prim = primary_stage(cfg, states)
    conn.execute("UPDATE deployments SET stage=?, updated_at=? WHERE id=?", (prim, ts, dep["id"]))
    if prim != dep["stage"]:
        conn.execute("INSERT INTO stage_events (tenant_id, deployment_id, from_stage, to_stage, actor_id, note, at)"
                     " VALUES (?,?,?,?,?,?,?)", (tenant_id, dep["id"], dep["stage"], prim, actor, note, ts))
    from . import sow  # billable milestones follow their stage
    sow.on_stage_states(conn, tenant_id, cfg, dep["id"], states, actor)
    return prim


def move_to(conn, tenant_id: str, cfg: dict, dep, to: str, actor: str, note: str = "") -> str:
    """'Move to a stage', either direction: earlier work in progress is done, later work reopens."""
    keys = config.stage_keys(cfg)
    states = stage_states(conn, cfg, dep)
    i = keys.index(to)
    changes = {to: "in_progress"}
    for j, k in enumerate(keys):
        if j < i and states[k]["state"] == "in_progress":
            changes[k] = "done"
        if j > i and states[k]["state"] == "in_progress":
            changes[k] = "not_started"
    return apply_states(conn, tenant_id, cfg, dep, changes, actor, note)


def held_days(conn, dep_id: str, since: datetime, at: datetime) -> float:
    """Days on hold between two moments: they don't count against a stage's clock."""
    total = 0.0
    for r in conn.execute("SELECT started_at, ended_at FROM delays WHERE deployment_id=? AND signal='on_hold'",
                          (dep_id,)):
        a, b = max(parse_ts(r["started_at"]), since), min(parse_ts(r["ended_at"]) or at, at)
        if b > a:
            total += (b - a).total_seconds() / 86400
    return total


def active_days(conn, dep_id: str, entered: str | None, until: str | None = None, at: datetime | None = None) -> float | None:
    if not entered:
        return None
    at = at or now()
    a, b = parse_ts(entered), parse_ts(until) if until else at
    return round(max(0.0, (b - a).total_seconds() / 86400 - held_days(conn, dep_id, a, b)), 1)


def days_in_stage(conn, cfg: dict, dep, key: str | None = None, at: datetime | None = None) -> float:
    st = stage_states(conn, cfg, dep)[key or dep["stage"]]
    return active_days(conn, dep["id"], st["entered_at"] or dep["created_at"], None, at) or 0.0


def stage_durations(conn, cfg: dict, deps: list) -> dict:
    """Finished stage durations per stage key, not counting time on hold."""
    out: dict = {}
    for dep in deps:
        for k, v in stage_states(conn, cfg, dep).items():
            if v["state"] == "done" and v["entered_at"] and v["done_at"]:
                out.setdefault(k, []).append(active_days(conn, dep["id"], v["entered_at"], v["done_at"]))
    return out


def live_stage(cfg: dict) -> str | None:
    """The stage after the one that carries the go-live engine (or the second-to-last)."""
    keys = config.stage_keys(cfg)
    for i, s in enumerate(cfg["stages"]):
        if "golive" in s.get("engines", []) and i + 1 < len(keys):
            return keys[i + 1]
    return keys[-2] if len(keys) >= 2 else None


# ======================================================================= health

def hours_spent(conn, dep_id: str) -> float:
    return round(float(conn.execute("SELECT COALESCE(SUM(hours),0) h FROM time_entries WHERE deployment_id=?",
                                    (dep_id,)).fetchone()["h"] or 0), 1)


def progress(dep, today: date | None = None) -> float | None:
    s, e = parse_day(dep["start_on"]), parse_day(dep["end_on"])
    if not s or not e or e <= s:
        return None
    today = today or now().date()
    return round(min(1.0, max(0.0, (today - s).days / (e - s).days)), 3)


def days_left(dep, today: date | None = None) -> int | None:
    e = parse_day(dep["end_on"])
    return (e - (today or now().date())).days if e else None


def burn(conn, dep) -> float | None:
    if not dep["budget_hours"]:
        return None
    return round(hours_spent(conn, dep["id"]) / float(dep["budget_hours"]), 3)


def latest_finding(conn, dep_id: str, engine: str):
    return conn.execute("SELECT * FROM findings WHERE deployment_id=? AND engine=? ORDER BY created_at DESC LIMIT 1",
                        (dep_id, engine)).fetchone()


def _conditions(conn, cfg: dict, dep, at: datetime) -> list[dict]:
    out = []
    states = stage_states(conn, cfg, dep)
    last = cfg["stages"][-1]["key"]
    for st in cfg["stages"]:
        k = st["key"]
        # The last stage is where finished work rests, and a deployment on hold isn't on the clock.
        if states[k]["state"] != "in_progress" or k == last or dep["hold_since"]:
            continue
        dis = active_days(conn, dep["id"], states[k]["entered_at"] or dep["created_at"], None, at)
        tgt = st.get("target_days")
        if tgt and dis > tgt:
            out.append({"rule": "stage_overrun", "key": f"stage_overrun:{k}", "stage": k,
                        "entered_at": states[k]["entered_at"] or dep["created_at"],
                        "severity": "high" if dis > 1.5 * tgt else "med",
                        "text": f"{st['name']} is past its target: {dis:g} days against {tgt}."})
    b, p = burn(conn, dep), progress(dep, at.date())
    if b is not None and p is not None and b - p > BURN_MARGIN:
        out.append({"rule": "budget_burn", "key": "budget_burn", "severity": "high" if b >= 0.9 else "med",
                    "text": f"Budget burning ahead of schedule: {b:.0%} of hours spent at {p:.0%} of the period."})
    keys = config.stage_keys(cfg)
    conf_stage = next((s["key"] for s in cfg["stages"] if "conformance" in s.get("engines", [])), None)
    live_near = [k for k in keys if states[k]["state"] == "in_progress"]
    if conf_stage and any(0 <= keys.index(k) - keys.index(conf_stage) <= 1 for k in live_near):
        f = latest_finding(conn, dep["id"], "conformance")
        if f and json.loads(f["result_json"]).get("status") == "fail":
            out.append({"rule": "conformance_gate", "key": "conformance_gate", "severity": "high",
                        "text": "The latest conformance run is below its bar, with a critical case failing."})
    for t in conn.execute("SELECT t.id, t.title, t.updated_at, d.started_at FROM tasks t LEFT JOIN delays d"
                          " ON d.tenant_id=t.tenant_id AND d.dedupe_key='task:' || t.id AND d.ended_at IS NULL"
                          " WHERE t.deployment_id=? AND t.status='blocked'", (dep["id"],)):
        since = parse_ts(t["started_at"] or t["updated_at"])
        days = (at - since).days
        if days > BLOCKED_DAYS:
            out.append({"rule": "blocked_task", "key": f"blocked_task:{t['id']}", "severity": "med",
                        "text": f"Blocked {days} days: {t['title'][:120]}"})
    # Quiet customer: only where someone has connected their mailbox and this customer's mail is being seen.
    if not dep["hold_since"] and any(states[k]["state"] == "in_progress" for k in keys if k != last):
        lc = last_contact(conn, dep["customer_id"], at)
        if lc and lc["inbound_days"] is not None and lc["inbound_days"] > QUIET_DAYS:
            out.append({"rule": "quiet_customer", "key": "quiet_customer", "severity": "low",
                        "text": f"No word from the customer in {int(lc['inbound_days'])} days."})
    return out


QUIET_DAYS = 10


def last_contact(conn, customer_id: str, at: datetime | None = None) -> dict | None:
    """When the customer last emailed the team, and the team them (from opted-in mailboxes)."""
    at = at or now()
    rows = {r["direction"]: r["at"] for r in conn.execute(
        "SELECT direction, MAX(at) at FROM contact_signals WHERE customer_id=? GROUP BY direction", (customer_id,))}
    if not rows:
        return None

    def days(s):
        return round((at - parse_ts(s)).total_seconds() / 86400, 1) if s else None
    latest = max(rows.values())
    return {"at": latest, "days": days(latest), "last_inbound": rows.get("in"), "inbound_days": days(rows.get("in")),
            "last_outbound": rows.get("out"), "outbound_days": days(rows.get("out"))}


def evaluate(conn, tenant_id: str, tenant_name: str, cfg: dict, dep, at: datetime | None = None) -> dict:
    """Raise, keep or resolve rule flags for one deployment. Inside a transaction.
    Rule flags carry a rule_key so re-running never duplicates; people's flags are never touched."""
    at = at or now()
    conds = {c["key"]: c for c in _conditions(conn, cfg, dep, at)}
    existing = {f["rule_key"]: f for f in conn.execute(
        "SELECT * FROM flags WHERE deployment_id=? AND rule_key IS NOT NULL AND status IN ('open','owned','returned')",
        (dep["id"],))}
    raised, resolved, opened = [], [], []
    for key, c in conds.items():
        if key not in existing:
            fid = new_id("flg")
            conn.execute("INSERT INTO flags (id, tenant_id, deployment_id, severity, text, rule_key, raised_by,"
                         " status, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                         (fid, tenant_id, dep["id"], c["severity"], c["text"], key, "rule:" + c["rule"],
                          "open", audit.now()))
            audit.record(conn, tenant_id, "rule:" + c["rule"], "flag.raise", fid,
                         {"deployment": dep["id"], "text": c["text"], "severity": c["severity"]})
            events.emit(conn, cfg, tenant_id, "flag.raised",
                        {"deployment_id": dep["id"], "deployment": dep["name"], "text": c["text"],
                         "severity": c["severity"], "actor": "Rules", "flag_id": fid})
            raised.append(key)
        elif (existing[key]["text"], existing[key]["severity"]) != (c["text"], c["severity"]):
            conn.execute("UPDATE flags SET text=?, severity=? WHERE id=?",
                         (c["text"], c["severity"], existing[key]["id"]))
            if existing[key]["severity"] != c["severity"]:
                audit.record(conn, tenant_id, "rule:" + c["rule"], "flag.update", existing[key]["id"],
                             {"severity": c["severity"], "text": c["text"]})
        if c["rule"] == "stage_overrun":
            tgt = next(s["target_days"] for s in cfg["stages"] if s["key"] == c["stage"])
            sid, created = reopen_or_open(conn, tenant_id, dep, signal="stage_overrun",
                                          dedupe_key=f"{dep['id']}:{key}", evidence=c["text"],
                                          started_at=parse_ts(c["entered_at"]) + timedelta(days=tgt),
                                          tenant_name=tenant_name, stage=c["stage"])
            if created:
                opened.append(sid)
                audit.record(conn, tenant_id, "rule:stage_overrun", "delay.open", sid,
                             {"deployment": dep["id"], "stage": c["stage"], "signal": "stage_overrun"})
                events.emit(conn, cfg, tenant_id, "delay.opened",
                            {"deployment_id": dep["id"], "deployment": dep["name"], "delay_id": sid,
                             "reason": SIGNALS["stage_overrun"][1], "actor": "Rules",
                             "owner": owner_labels(tenant_name)["team"]})
    for key, f in existing.items():
        if key not in conds:
            conn.execute("UPDATE flags SET status='resolved', handled_by=?, handled_at=?, note=? WHERE id=?",
                         ("rule", audit.now(), "Condition cleared.", f["id"]))
            audit.record(conn, tenant_id, "rule", "flag.resolve", f["id"], {"note": "condition cleared"})
            resolved.append(key)
    return {"raised": raised, "resolved": resolved, "delays_opened": opened}


def sweep(conn, tenant_id: str, at: datetime | None = None) -> dict:
    """Re-evaluate every deployment's rules. Idempotent: a second run changes nothing."""
    t = conn.execute("SELECT * FROM tenants WHERE id=?", (tenant_id,)).fetchone()
    cfg = config.upgrade(json.loads(t["config_json"]))
    summary = {"deployments": 0, "flags_raised": 0, "flags_resolved": 0, "delays_opened": 0}
    for dep in conn.execute("SELECT * FROM deployments WHERE tenant_id=?", (tenant_id,)).fetchall():
        with db.tx(conn):
            r = evaluate(conn, tenant_id, t["name"], cfg, dep, at)
        summary["deployments"] += 1
        summary["flags_raised"] += len(r["raised"])
        summary["flags_resolved"] += len(r["resolved"])
        summary["delays_opened"] += len(r["delays_opened"])
    return summary


def sweep_all(conn) -> dict:
    return {t["id"]: sweep(conn, t["id"]) for t in conn.execute("SELECT id FROM tenants").fetchall()}


def start_sweeper(conn, stop: threading.Event, minutes: float = 30.0) -> None:
    import logging
    log = logging.getLogger("fieldwork.sweep")

    def loop():
        while not stop.wait(minutes * 60):
            try:
                sweep_all(conn)
            except Exception as e:  # keep sweeping next time
                log.warning("sweep failed: %s", e)
    threading.Thread(target=loop, name="fieldwork-sweep", daemon=True).start()


def status_of(dep, open_flags: list) -> str:
    if dep["hold_since"]:
        return "on_hold"
    if dep["health"] == "blocked" or any(f["severity"] == "high" for f in open_flags):
        return "off_track"
    if dep["health"] == "at_risk" or open_flags:
        return "at_risk"
    return "on_track"


# ======================================================================= routes

def register(app, d) -> None:
    conn, ctx, Ctx = d.conn, d.ctx, d.Ctx

    def labels(c) -> dict:
        return owner_labels(c.tenant_name)

    def internal_deps(c, action: str = "task.view_internal") -> list:
        return [r for r in d.visible_deployments(c)
                if c.can_on("task.view_internal", r["id"]) and c.can_on(action, r["id"])]

    def users_map(c) -> dict:
        return {r["id"]: r["name"] for r in conn.execute("SELECT id, name FROM users WHERE tenant_id=?",
                                                          (c.tenant_id,))}

    def delay_rows(dep_ids: list, status: str | None = None) -> list:
        if not dep_ids:
            return []
        q = f"SELECT * FROM delays WHERE deployment_id IN ({','.join('?' * len(dep_ids))})"
        args = list(dep_ids)
        if status == "open":
            q += " AND ended_at IS NULL"
        elif status == "unconfirmed":
            q += " AND status='open'"
        return conn.execute(q + " ORDER BY started_at DESC", args).fetchall()

    def span_for(c, sid: str):
        r = conn.execute("SELECT * FROM delays WHERE id=? AND tenant_id=?", (sid, c.tenant_id)).fetchone()
        if not r:
            raise HTTPException(404, "delay not found")
        c.deployment(r["deployment_id"])
        if not c.can_on("task.view_internal", r["deployment_id"]):
            raise HTTPException(404, "delay not found")
        return r

    # ----------------------------------------------------------------- delays

    @app.get("/api/delays")
    def list_delays(deployment_id: str | None = None, status: str | None = None, c: Ctx = Depends(ctx)):
        if deployment_id:
            c.deployment(deployment_id)
            if not c.can_on("task.view_internal", deployment_id):
                return []
            ids = [deployment_id]
        else:
            ids = [r["id"] for r in internal_deps(c)]
        names = {r["id"]: r["name"] for r in d.visible_deployments(c)}
        return [delay_out(r, labels(c), names, users_map(c)) for r in delay_rows(ids, status)]

    @app.get("/api/delays/queue")
    def delay_queue(c: Ctx = Depends(ctx)):
        ids = [r["id"] for r in internal_deps(c, "delay.confirm")]
        rows = delay_rows(ids, "unconfirmed")
        names = {r["id"]: r["name"] for r in d.visible_deployments(c)}
        return {"groups": grouped_queue(rows), "items": [delay_out(r, labels(c), names) for r in rows],
                "owners": [{"key": o, "label": labels(c)[o]} for o in OWNERS]}

    @app.get("/api/delays/rollup")
    def delay_rollup(deployment_id: str | None = None, c: Ctx = Depends(ctx)):
        ids = [r["id"] for r in internal_deps(c)]
        if deployment_id:
            ids = [i for i in ids if i == deployment_id]
        out = rollup(delay_rows(ids), labels(c))
        # Priors describe the whole workspace, so only people who see internal work workspace-wide get them.
        out["priors"] = (list(learn(conn, c.tenant_id, c.tenant_name).values())
                         if c.scope("task.view_internal") == "all" or (ids and c.can("delay.confirm")) else [])
        return out

    class DelayIn(BaseModel):
        signal: str = "manual"
        owner: str | None = None
        reason: str = Field(default="", max_length=300)
        evidence: str = Field(default="", max_length=2000)
        started_on: str | None = None

    @app.post("/api/deployments/{dep_id}/delays", status_code=201)
    def log_delay(dep_id: str, body: DelayIn, c: Ctx = Depends(ctx)):
        dep = c.deployment(dep_id)
        c.require_on("deployment.edit", dep_id)
        if not c.can_on("task.view_internal", dep_id):
            raise HTTPException(403, "delays are internal")
        if body.signal not in MANUAL_SIGNALS:
            raise HTTPException(422, f"signal must be one of {', '.join(MANUAL_SIGNALS)}")
        if body.owner is not None and body.owner not in OWNERS:
            raise HTTPException(422, f"owner must be one of {', '.join(OWNERS)}")
        start = parse_ts(body.started_on) if body.started_on else None
        if body.started_on and not start:
            raise HTTPException(422, "started_on must be YYYY-MM-DD")
        with db.tx(conn):
            sid, _ = open_span(conn, c.tenant_id, dep, signal=body.signal, dedupe_key=new_id("manual"),
                               evidence=body.evidence, started_at=start, owner_hint=body.owner,
                               reason=body.reason or None, tenant_name=c.tenant_name)
            row = conn.execute("SELECT * FROM delays WHERE id=?", (sid,)).fetchone()
            c.log("delay.open", sid, {"deployment": dep_id, "signal": body.signal,
                                      "proposed_owner": row["proposed_owner"], "reason": row["proposed_reason"]})
            if row["status"] == "open":
                d.emit(c, "delay.opened", dep_id, reason=row["proposed_reason"], delay_id=sid,
                       owner=labels(c)[row["proposed_owner"]])
        return delay_out(row, labels(c))

    class DecideIn(BaseModel):
        owner: str | None = None
        reason: str = Field(default="", max_length=300)

    @app.post("/api/delays/{sid}/decide")
    def decide_delay(sid: str, body: DecideIn, c: Ctx = Depends(ctx)):
        r = span_for(c, sid)
        c.require_on("delay.confirm", r["deployment_id"])
        with db.tx(conn):
            res = decide(conn, r, actor=c.uid, owner=body.owner, reason=body.reason, batch=False)
            c.log("delay.amend" if res["amended"] else "delay.decide", sid,
                  {"deployment": r["deployment_id"], **res, "reason": body.reason or r["proposed_reason"],
                   "days": span_days(r)})
        return res

    class BatchIn(BaseModel):
        span_ids: list[str] = Field(min_length=1, max_length=500)
        owner: str | None = None
        reason: str = Field(default="", max_length=300)

    @app.post("/api/delays/batch")
    def decide_batch(body: BatchIn, c: Ctx = Depends(ctx)):
        rows = [span_for(c, s) for s in dict.fromkeys(body.span_ids)]
        for r in rows:
            c.require_on("delay.confirm", r["deployment_id"])
        done = skipped = 0
        with db.tx(conn):
            for r in rows:
                if r["status"] == "deduced":
                    skipped += 1
                    continue
                res = decide(conn, r, actor=c.uid, owner=body.owner, reason=body.reason, batch=True)
                c.log("delay.amend" if res["amended"] else "delay.decide", r["id"],
                      {"deployment": r["deployment_id"], **res, "batch": True})
                done += 1
        return {"decided": done, "skipped": skipped, "weight_each": WEIGHT_BATCH}

    @app.post("/api/delays/{sid}/close")
    def close_delay(sid: str, c: Ctx = Depends(ctx)):
        r = span_for(c, sid)
        c.require_on("deployment.edit", r["deployment_id"])
        if r["ended_at"]:
            raise HTTPException(409, "already closed")
        with db.tx(conn):
            conn.execute("UPDATE delays SET ended_at=? WHERE id=?", (iso(now()), sid))
            c.log("delay.close", sid, {"deployment": r["deployment_id"]})
        return {"ok": True}

    # ------------------------------------------------------------------ flags

    def flag_out(f, names: dict, users: dict) -> dict:
        x = {k: f[k] for k in f.keys()}
        x["deployment"] = names.get(f["deployment_id"], "")
        x["raised_by_name"] = users.get(f["raised_by"]) or ("Rules" if f["raised_by"].startswith("rule:") else f["raised_by"])
        x["handled_by_name"] = users.get(f["handled_by"]) if f["handled_by"] else None
        x["age_days"] = round((now() - parse_ts(f["created_at"])).total_seconds() / 86400, 1)
        return x

    @app.get("/api/flags")
    def list_flags(deployment_id: str | None = None, include_resolved: bool = False, c: Ctx = Depends(ctx)):
        deps = internal_deps(c)
        if deployment_id:
            c.deployment(deployment_id)
            deps = [x for x in deps if x["id"] == deployment_id]
        if not deps:
            return []
        ids = [x["id"] for x in deps]
        q = f"SELECT * FROM flags WHERE deployment_id IN ({','.join('?' * len(ids))})"
        if not include_resolved:
            q += " AND status IN ('open','owned','returned')"
        q += " ORDER BY CASE severity WHEN 'high' THEN 0 WHEN 'med' THEN 1 ELSE 2 END, created_at DESC"
        names = {x["id"]: x["name"] for x in deps}
        users = users_map(c)
        return [{**flag_out(f, names, users), "you_can_handle": c.can_on("flag.handle", f["deployment_id"])}
                for f in conn.execute(q, ids).fetchall()]

    class FlagIn(BaseModel):
        text: str = Field(min_length=1, max_length=500)
        severity: str = "med"

    @app.post("/api/deployments/{dep_id}/flags", status_code=201)
    def raise_flag(dep_id: str, body: FlagIn, c: Ctx = Depends(ctx)):
        c.deployment(dep_id)
        c.require_on("deployment.edit", dep_id)
        if not c.can_on("task.view_internal", dep_id):
            raise HTTPException(403, "flags are internal")
        if body.severity not in SEVERITIES:
            raise HTTPException(422, "severity must be low, med or high")
        fid = new_id("flg")
        with db.tx(conn):
            conn.execute("INSERT INTO flags (id, tenant_id, deployment_id, severity, text, rule_key, raised_by,"
                         " status, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                         (fid, c.tenant_id, dep_id, body.severity, body.text, None, c.uid, "open", audit.now()))
            c.log("flag.raise", fid, {"deployment": dep_id, "text": body.text, "severity": body.severity})
            d.emit(c, "flag.raised", dep_id, text=body.text, severity=body.severity, flag_id=fid)
        return {"id": fid}

    class FlagAct(BaseModel):
        note: str = Field(default="", max_length=500)

    def flag_for(c, fid: str):
        f = conn.execute("SELECT * FROM flags WHERE id=? AND tenant_id=?", (fid, c.tenant_id)).fetchone()
        if not f:
            raise HTTPException(404, "flag not found")
        c.deployment(f["deployment_id"])
        if not c.can_on("task.view_internal", f["deployment_id"]):
            raise HTTPException(404, "flag not found")
        c.require_on("flag.handle", f["deployment_id"])
        if f["status"] == "resolved":
            raise HTTPException(409, "already resolved")
        return f

    def act(fid: str, c, status: str, note: str, action: str):
        f = flag_for(c, fid)
        with db.tx(conn):
            conn.execute("UPDATE flags SET status=?, handled_by=?, handled_at=?, note=? WHERE id=?",
                         (status, c.uid, audit.now(), note or f["note"], fid))
            c.log(action, fid, {"deployment": f["deployment_id"], "note": note})
        return {"status": status}

    @app.post("/api/flags/{fid}/take")
    def take_flag(fid: str, body: FlagAct, c: Ctx = Depends(ctx)):
        return act(fid, c, "owned", body.note, "flag.take")

    @app.post("/api/flags/{fid}/return")
    def return_flag(fid: str, body: FlagAct, c: Ctx = Depends(ctx)):
        if not body.note.strip():
            raise HTTPException(422, "say what the team lead should do with it")
        return act(fid, c, "returned", body.note, "flag.return")

    @app.post("/api/flags/{fid}/resolve")
    def resolve_flag(fid: str, body: FlagAct, c: Ctx = Depends(ctx)):
        return act(fid, c, "resolved", body.note, "flag.resolve")

    @app.post("/api/sweep")
    def run_sweep(c: Ctx = Depends(ctx)):
        if c.scope("flag.handle") != "all":
            raise HTTPException(403, "running the rules across the workspace needs workspace-wide flag handling")
        res = sweep(conn, c.tenant_id)
        with db.tx(conn):
            c.log("sweep.run", c.tenant_id, res)
        return res

    # --------------------------------------------------------------- portfolio

    def stage_rows(c, dep, internal: bool) -> list:
        """What happened on each stage: state, days, and whether it's past its target."""
        states = stage_states(conn, c.cfg, dep)
        last = c.cfg["stages"][-1]["key"]
        out = []
        for s_ in c.cfg["stages"]:
            k = s_["key"]; v = states[k]
            days = active_days(conn, dep["id"], v["entered_at"], v["done_at"]) if v["entered_at"] else None
            live = v["state"] == "in_progress"
            over = bool(internal and live and k != last and not dep["hold_since"] and days is not None
                        and s_["target_days"] and days > s_["target_days"])
            if live and dep["hold_since"]:
                cell = "hold"
            elif live:
                cell = "stalled" if over else "progress"  # red means past target, nothing else
            else:
                cell = {"done": "done", "skipped": "skipped"}.get(v["state"], "pending")
            out.append({"key": k, "name": s_["name"], "state": cell, "status": v["state"], "days": days,
                        "target_days": s_["target_days"] if internal else None, "overrun": over,
                        "entered_at": v["entered_at"], "done_at": v["done_at"]})
        return out

    def portfolio_row(c, dep, flags_by: dict, delays_by: dict, cust: dict) -> dict:
        stage = next((s for s in c.cfg["stages"] if s["key"] == dep["stage"]), None)
        internal = c.can_on("task.view_internal", dep["id"])
        srows = stage_rows(c, dep, internal)
        here = next((x for x in srows if x["key"] == dep["stage"]), {})
        dis = here.get("days") or 0.0
        target = stage["target_days"] if stage else None
        overrun = any(x["overrun"] for x in srows)  # "stalled" is an internal signal; stage_rows only sets it inside
        keys = config.stage_keys(c.cfg)
        i = keys.index(dep["stage"]) if dep["stage"] in keys else -1
        fields = json.loads(dep["fields_json"] or "{}")
        nxt = c.cfg["stages"][i + 1]["name"] if 0 <= i < len(keys) - 1 else None
        milestone = None
        if fields.get("target_golive") and live_stage(c.cfg) and i < keys.index(live_stage(c.cfg)):
            milestone = {"label": "Go-live", "on": fields["target_golive"]}
        elif nxt:
            milestone = {"label": nxt, "on": None}
        row = {"id": dep["id"], "name": dep["name"], "customer": cust.get(dep["customer_id"], {}).get("name", ""),
               "stage": dep["stage"], "stage_name": stage["name"] if stage else dep["stage"],
               "days_in_stage": dis, "target_days": target if internal else None, "overrun": overrun,
               "cells": [{"key": x["key"], "name": x["name"], "state": x["state"]} for x in srows],
               "stages": srows, "next_milestone": milestone, "health": dep["health"],
               "on_hold": ({"since": dep["hold_since"], "reason": dep["hold_reason"],
                            "days": round((now() - parse_ts(dep["hold_since"])).total_seconds() / 86400, 1)}
                           if dep["hold_since"] else None)}
        if not internal:
            return row
        fl = flags_by.get(dep["id"], [])
        dl = delays_by.get(dep["id"], [])
        open_dl = [x for x in dl if x["ended_at"] is None]
        top = max(open_dl, key=lambda x: span_days(x), default=None)
        conf = latest_finding(conn, dep["id"], "conformance")
        lead = conn.execute("SELECT name FROM users WHERE id=?", (dep["lead_id"],)).fetchone() if dep["lead_id"] else None
        row.update({
            "status": status_of(dep, fl),
            "flags": len(fl), "flags_high": sum(1 for f in fl if f["severity"] == "high"),
            "delay": ({"owner": top["confirmed_owner"] or top["proposed_owner"],
                       "label": labels(c)[top["confirmed_owner"] or top["proposed_owner"]],
                       "days": span_days(top), "reason": top["confirmed_reason"] or top["proposed_reason"],
                       "confirmed": top["status"] != "open"} if top else None),
            "delay_days": round(sum(span_days(x) for x in dl), 1),
            "arr": cust.get(dep["customer_id"], {}).get("arr"),
            "contact": cust.get(dep["customer_id"], {}).get("exec_sponsor"),
            "lead": lead["name"] if lead else None,
            "start_on": dep["start_on"], "end_on": dep["end_on"], "budget_hours": dep["budget_hours"],
            "progress": progress(dep), "days_left": days_left(dep), "hours_spent": hours_spent(conn, dep["id"]),
            "burn": burn(conn, dep), "last_contact": last_contact(conn, dep["customer_id"]),
            "conformance": ({"status": json.loads(conf["result_json"]).get("status"),
                             "summary": json.loads(conf["result_json"]).get("summary"),
                             "at": conf["created_at"]} if conf else None),
        })
        return row

    def customers_info(c) -> dict:
        out = {}
        for r in conn.execute("SELECT id, name, fields_json FROM customers WHERE tenant_id=?", (c.tenant_id,)):
            f = json.loads(r["fields_json"] or "{}")
            out[r["id"]] = {"name": r["name"], "arr": f.get("arr"), "exec_sponsor": f.get("exec_sponsor")}
        return out

    @app.get("/api/portfolio")
    def portfolio(c: Ctx = Depends(ctx)):
        deps = d.visible_deployments(c)
        ids = [x["id"] for x in deps if c.can_on("task.view_internal", x["id"])]
        flags_by: dict = {}
        delays_by: dict = {}
        if ids:
            ph = ",".join("?" * len(ids))
            for f in conn.execute(f"SELECT * FROM flags WHERE deployment_id IN ({ph})"
                                  " AND status IN ('open','owned','returned')", ids):
                flags_by.setdefault(f["deployment_id"], []).append(f)
            for x in conn.execute(f"SELECT * FROM delays WHERE deployment_id IN ({ph})", ids):
                delays_by.setdefault(x["deployment_id"], []).append(x)
        cust = customers_info(c)
        rows = [portfolio_row(c, x, flags_by, delays_by, cust) for x in deps]
        out: dict = {"stages": [{"key": s["key"], "name": s["name"], "target_days": s["target_days"]}
                                for s in c.cfg["stages"]],
                     "deployments": rows}
        if not ids:
            return out
        last = config.stage_keys(c.cfg)[-1]
        # headline numbers
        visible_ids = set(ids)
        durations = stage_durations(conn, c.cfg, [x for x in deps if x["id"] in visible_ids])
        medians = []
        for s in c.cfg["stages"]:
            ds = durations.get(s["key"], [])
            medians.append({"key": s["key"], "name": s["name"], "target_days": s["target_days"],
                            "median_days": round(statistics.median(ds), 1) if ds else None, "n": len(ds)})
        ls = live_stage(c.cfg)
        ttl = []
        if ls:
            for dep in deps:
                if dep["id"] not in visible_ids:
                    continue
                st = stage_states(conn, c.cfg, dep)
                starts = [v["entered_at"] for v in st.values() if v["entered_at"]]
                hit = st[ls]["entered_at"]
                if starts and hit:
                    ttl.append(active_days(conn, dep["id"], min(starts), hit))
        keys = config.stage_keys(c.cfg)
        ttl_target = sum(s["target_days"] for s in c.cfg["stages"][:keys.index(ls)]) if ls else None
        active = [r for r in rows if r["id"] in visible_ids and r["stage"] != last]
        arr_seen: dict = {}
        for r in rows:
            if r["id"] in visible_ids and r.get("arr"):
                arr_seen[r["customer"]] = float(r["arr"])
        out["kpis"] = {
            "active_deployments": len(active),
            "off_track": sum(1 for r in rows if r.get("status") == "off_track"),
            "on_hold": sum(1 for r in rows if r.get("status") == "on_hold"),
            "at_risk": sum(1 for r in rows if r.get("status") == "at_risk"),
            "open_flags": sum(r.get("flags", 0) for r in rows),
            "contract_value": round(sum(arr_seen.values()), 2) if arr_seen else None,
            "median_days_to_live": round(statistics.median(ttl), 1) if ttl else None,
            "days_to_live_target": ttl_target,
            "live_stage": ls,
            "utilization": utilization(conn, c.cfg, c.tenant_id) if c.can("people.read") else None,
        }
        out["stage_medians"] = medians
        out["delays"] = rollup([x for v in delays_by.values() for x in v], labels(c))
        return out

    # ------------------------------------------------------ stage state, hold

    class StageStateIn(BaseModel):
        state: str
        note: str = Field(default="", max_length=500)

    @app.put("/api/deployments/{dep_id}/stages/{key}")
    def set_stage_state(dep_id: str, key: str, body: StageStateIn, c: Ctx = Depends(ctx)):
        """Mark one stage done, skipped, in progress or not started. Several can be in progress."""
        dep = c.deployment(dep_id)
        c.require_on("deployment.advance", dep_id)
        if key not in config.stage_keys(c.cfg):
            raise HTTPException(404, "no such stage")
        if body.state not in STAGE_STATES:
            raise HTTPException(422, f"state must be one of {', '.join(STAGE_STATES)}")
        with db.tx(conn):
            prim = apply_states(conn, c.tenant_id, c.cfg, dep, {key: body.state}, c.uid, body.note.strip())
            c.log("stage.state", dep_id, {"stage": key, "state": body.state, "note": body.note, "primary": prim})
            if prim != dep["stage"]:
                d.emit(c, "deployment.advanced", dep_id, to=prim,
                       to_name=next(x["name"] for x in c.cfg["stages"] if x["key"] == prim))
        return {"stage": prim, "stages": stage_rows(c, conn.execute("SELECT * FROM deployments WHERE id=?",
                                                                     (dep_id,)).fetchone(), True)}

    class HoldIn(BaseModel):
        on: bool
        reason: str = Field(default="", max_length=300)
        waiting_on: str | None = None

    @app.post("/api/deployments/{dep_id}/hold")
    def hold(dep_id: str, body: HoldIn, c: Ctx = Depends(ctx)):
        """Pause a deployment: the stage clock stops, and the pause is recorded as a delay."""
        dep = c.deployment(dep_id)
        c.require_on("deployment.edit", dep_id)
        if not c.can_on("task.view_internal", dep_id):
            raise HTTPException(403, "holds are set by the delivery team")
        if body.waiting_on is not None and body.waiting_on not in OWNERS:
            raise HTTPException(422, f"waiting_on must be one of {', '.join(OWNERS)}")
        key = f"hold:{dep_id}"
        with db.tx(conn):
            if body.on and not dep["hold_since"]:
                ts = audit.now()
                conn.execute("UPDATE deployments SET hold_since=?, hold_reason=?, updated_at=? WHERE id=?",
                             (ts, body.reason, ts, dep_id))
                sid, _ = reopen_or_open(conn, c.tenant_id, dep, signal="on_hold", dedupe_key=key,
                                        owner_hint=body.waiting_on, reason=body.reason or None,
                                        evidence=body.reason, tenant_name=c.tenant_name)
                c.log("deployment.hold", dep_id, {"on": True, "reason": body.reason, "waiting_on": body.waiting_on,
                                                  "delay": sid})
                d.emit(c, "deployment.health", dep_id, health="on hold")
            elif not body.on and dep["hold_since"]:
                conn.execute("UPDATE deployments SET hold_since=NULL, hold_reason='', updated_at=? WHERE id=?",
                             (audit.now(), dep_id))
                close_span(conn, c.tenant_id, key)
                c.log("deployment.hold", dep_id, {"on": False})
        return {"on_hold": body.on}

    # ------------------------------------------------------------------- team

    @app.get("/api/team")
    def team(week: str | None = None, c: Ctx = Depends(ctx)):
        c.require("people.read")
        ws = week_start(parse_day(week) or now().date())
        tw = team_week(conn, c.cfg, c.tenant_id, ws)
        vis = {x["id"] for x in internal_deps(c)}
        for p in tw["people"]:
            p["role_name"] = config.role_name(c.cfg, p["role"])
            for a in p["allocations"]:
                if a["deployment_id"] not in vis:  # hours count; the name isn't yours to see
                    a["deployment_id"], a["deployment"] = None, "Another deployment"
        un = []
        if vis:
            ph = ",".join("?" * len(vis))
            un = [dict(r) for r in conn.execute(
                "SELECT t.id, t.title, t.status, t.due, t.stage, t.deployment_id, d.name deployment FROM tasks t"
                f" JOIN deployments d ON d.id=t.deployment_id WHERE t.deployment_id IN ({ph})"
                " AND t.assignee_id IS NULL AND t.status!='done' ORDER BY t.due, t.created_at", list(vis))]
        for t in un:
            t["you_can_assign"] = c.can_on("task.assign", t["deployment_id"])
        return {**tw, "unassigned": un, "rolloff": [r for r in rolloff(conn, c.tenant_id) if r["deployment_id"] in vis],
                "utilization_4w": utilization(conn, c.cfg, c.tenant_id)}

    @app.get("/api/me/week")
    def my_week(week: str | None = None, c: Ctx = Depends(ctx)):
        ws = week_start(parse_day(week) or now().date())
        pw = person_week(conn, c.user, ws)
        by_dep = [dict(r) for r in conn.execute(
            "SELECT t.deployment_id, d.name deployment, SUM(t.hours) hours FROM time_entries t"
            " LEFT JOIN deployments d ON d.id=t.deployment_id WHERE t.user_id=? AND t.day>=? AND t.day<=?"
            " GROUP BY t.deployment_id, d.name", (c.uid, ws.isoformat(), (ws + timedelta(days=6)).isoformat()))]
        days = [dict(r) for r in conn.execute(
            "SELECT day, SUM(hours) hours FROM time_entries WHERE user_id=? AND day>=? AND day<=?"
            " GROUP BY day ORDER BY day", (c.uid, ws.isoformat(), (ws + timedelta(days=6)).isoformat()))]
        today = now().date().isoformat()
        planned_today = round(sum(round(x["hours"] / 5 * 2) / 2 for x in pw["allocations"]), 1)
        return {**pw, "week": ws.isoformat(), "by_deployment": by_dep, "by_day": days,
                "today": {"day": today, "logged": round(sum(x["hours"] for x in days if x["day"] == today), 1),
                          "planned": planned_today}}

    class TimeIn(BaseModel):
        day: str
        hours: float = Field(gt=0, le=24)
        deployment_id: str | None = None

    @app.post("/api/time", status_code=201)
    def log_time(body: TimeIn, c: Ctx = Depends(ctx)):
        if not parse_day(body.day):
            raise HTTPException(422, "day must be YYYY-MM-DD")
        if not c.can("task.view_internal"):
            raise HTTPException(403, "time is logged by the delivery team")
        if body.deployment_id:
            c.deployment(body.deployment_id)
            if not c.is_member(body.deployment_id) or not c.can_on("task.view_internal", body.deployment_id):
                raise HTTPException(403, "log time on deployments you're staffed on")
        with db.tx(conn):
            conn.execute("INSERT INTO time_entries (tenant_id, user_id, deployment_id, day, hours, source, created_at)"
                         " VALUES (?,?,?,?,?,?,?)", (c.tenant_id, c.uid, body.deployment_id, body.day[:10],
                                                     body.hours, "console", audit.now()))
            c.log("time.log", c.uid, {"day": body.day[:10], "hours": body.hours, "deployment": body.deployment_id})
        return {"ok": True}

    @app.post("/api/time/today", status_code=201)
    def log_today(c: Ctx = Depends(ctx)):
        """One click: log today's planned hours on each deployment you're allocated to, unless already logged."""
        if not c.can("task.view_internal"):
            raise HTTPException(403, "time is logged by the delivery team")
        day = now().date()
        pw = person_week(conn, c.user, week_start(day))
        if day.isoformat() in pw["off_days"]:
            return {"logged": [], "hours": 0, "note": "you're out today"}
        logged = []
        with db.tx(conn):
            for a in pw["allocations"]:
                if conn.execute("SELECT 1 FROM time_entries WHERE user_id=? AND deployment_id=? AND day=?",
                                (c.uid, a["deployment_id"], day.isoformat())).fetchone():
                    continue
                h = round(a["hours"] / 5 * 2) / 2  # a day's share, to the half hour
                if h <= 0:
                    continue
                conn.execute("INSERT INTO time_entries (tenant_id, user_id, deployment_id, day, hours, source,"
                             " created_at) VALUES (?,?,?,?,?,?,?)",
                             (c.tenant_id, c.uid, a["deployment_id"], day.isoformat(), h, "planned", audit.now()))
                logged.append({"deployment_id": a["deployment_id"], "deployment": a["deployment"], "hours": h})
            if logged:
                c.log("time.log", c.uid, {"day": day.isoformat(), "planned": logged})
        return {"logged": logged, "hours": sum(x["hours"] for x in logged)}

    class AllocIn(BaseModel):
        allocation: float = Field(ge=0, le=1.5)

    @app.put("/api/deployments/{dep_id}/members/{user_id}")
    def set_allocation(dep_id: str, user_id: str, body: AllocIn, c: Ctx = Depends(ctx)):
        c.deployment(dep_id)
        c.require_on("deployment.staff", dep_id)
        if not conn.execute("SELECT 1 FROM users WHERE id=? AND tenant_id=?", (user_id, c.tenant_id)).fetchone():
            raise HTTPException(404, "person not found")
        with db.tx(conn):
            conn.execute("INSERT INTO deployment_members (deployment_id, user_id, allocation) VALUES (?,?,?)"
                         " ON CONFLICT (deployment_id, user_id) DO UPDATE SET allocation=excluded.allocation",
                         (dep_id, user_id, body.allocation))
            c.log("deployment.staff", dep_id, {"user": user_id, "allocation": body.allocation})
        return {"ok": True}

    # --------------------------------------------------------------- pipeline

    def opp_out(o, check: dict | None) -> dict:
        x = {k: o[k] for k in o.keys()}
        x["stage_label"] = PIPELINE_LABELS.get(o["stage"], o["stage"])
        x["staffing"] = check
        return x

    @app.get("/api/pipeline")
    def pipeline(c: Ctx = Depends(ctx)):
        c.require("pipeline.view")
        opps = conn.execute("SELECT * FROM opportunities WHERE tenant_id=?", (c.tenant_id,)).fetchall()
        live = [o for o in opps if o["stage"] not in ("won", "lost")]
        cap = pipeline_capacity(conn, c.cfg, c.tenant_id, live)
        cols = []
        for st in PIPELINE_STAGES[:4]:
            items = sorted([o for o in live if o["stage"] == st], key=lambda o: o["expected_start"] or "9999")
            cols.append({"stage": st, "label": PIPELINE_LABELS[st], "total": round(sum(o["value"] for o in items), 2),
                         "deals": [opp_out(o, cap["checks"].get(o["id"])) for o in items]})
        closed = [o for o in opps if o["stage"] in ("won", "lost")]
        won = [o for o in closed if o["stage"] == "won"]
        return {"columns": cols, "total": round(sum(o["value"] for o in live), 2),
                "weighted": round(sum(o["value"] * o["probability"] for o in live), 2),
                "win_rate": round(len(won) / len(closed), 3) if closed else None,
                "won": [opp_out(o, None) for o in won], "collisions": cap["collisions"],
                "rolloff": [r for r in rolloff(conn, c.tenant_id)
                            if r["deployment_id"] in {x["id"] for x in internal_deps(c)}]}

    class OppIn(BaseModel):
        name: str = Field(min_length=1, max_length=160)
        customer: str = Field(default="", max_length=120)
        use_case: str = Field(default="", max_length=200)
        value: float = Field(default=0, ge=0, le=1e12)
        probability: float = Field(default=0.2, ge=0, le=1)
        stage: str = "lead"
        expected_start: str | None = None
        weekly_hours: float = Field(default=0, ge=0, le=400)

    def check_opp(stage: str, start: str | None) -> None:
        if stage not in PIPELINE_STAGES:
            raise HTTPException(422, f"stage must be one of {', '.join(PIPELINE_STAGES)}")
        if start and not parse_day(start):
            raise HTTPException(422, "expected_start must be YYYY-MM-DD")

    @app.post("/api/opportunities", status_code=201)
    def add_opp(body: OppIn, c: Ctx = Depends(ctx)):
        c.require("pipeline.edit")
        check_opp(body.stage, body.expected_start)
        oid = new_id("opp")
        with db.tx(conn):
            conn.execute("INSERT INTO opportunities (id, tenant_id, name, customer, use_case, value, probability, stage,"
                         " expected_start, weekly_hours, source, external_id, updated_at)"
                         " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (oid, c.tenant_id, body.name, body.customer, body.use_case, body.value, body.probability,
                          body.stage, body.expected_start, body.weekly_hours, "console", None, audit.now()))
            c.log("opportunity.create", oid, {"name": body.name, "stage": body.stage, "value": body.value})
        return {"id": oid}

    class OppPatch(BaseModel):
        stage: str | None = None
        probability: float | None = Field(default=None, ge=0, le=1)
        value: float | None = Field(default=None, ge=0, le=1e12)
        expected_start: str | None = None
        weekly_hours: float | None = Field(default=None, ge=0, le=400)

    @app.patch("/api/opportunities/{oid}")
    def patch_opp(oid: str, body: OppPatch, c: Ctx = Depends(ctx)):
        c.require("pipeline.edit")
        o = conn.execute("SELECT * FROM opportunities WHERE id=? AND tenant_id=?", (oid, c.tenant_id)).fetchone()
        if not o:
            raise HTTPException(404, "opportunity not found")
        ch = {k: v for k, v in body.model_dump().items() if v is not None}
        check_opp(ch.get("stage", o["stage"]), ch.get("expected_start"))
        if not ch:
            return {"changed": []}
        ch["updated_at"] = audit.now()
        with db.tx(conn):
            conn.execute(f"UPDATE opportunities SET {', '.join(k + '=?' for k in ch)} WHERE id=?", (*ch.values(), oid))
            c.log("opportunity.update", oid, {k: v for k, v in ch.items() if k != "updated_at"})
        return {"changed": [k for k in ch if k != "updated_at"]}

    # -------------------------------------------------------------- approvals

    def approval_out(a, names: dict, users: dict, c) -> dict:
        x = {k: a[k] for k in a.keys()}
        x["deployment"] = names.get(a["deployment_id"], "")
        x["requested_by_name"] = users.get(a["requested_by"], a["requested_by"])
        x["decided_by_name"] = users.get(a["decided_by"]) if a["decided_by"] else None
        x["you_can_decide"] = (a["status"] == "pending" and a["requested_by"] != c.uid
                               and c.can_on("approval.decide", a["deployment_id"]))
        return x

    @app.get("/api/approvals")
    def approvals(status: str = "pending", deployment_id: str | None = None, c: Ctx = Depends(ctx)):
        deps = internal_deps(c)
        if deployment_id:
            c.deployment(deployment_id)
            deps = [x for x in deps if x["id"] == deployment_id]
        if not deps:
            return []
        ids = [x["id"] for x in deps]
        q = f"SELECT * FROM approvals WHERE deployment_id IN ({','.join('?' * len(ids))})"
        args = list(ids)
        if status != "all":
            q += " AND status=?"
            args.append(status)
        names = {x["id"]: x["name"] for x in deps}
        users = users_map(c)
        return [approval_out(a, names, users, c) for a in conn.execute(q + " ORDER BY created_at DESC", args)]

    class ApprovalIn(BaseModel):
        agent: str = Field(min_length=1, max_length=80)
        request: str = Field(min_length=1, max_length=300)
        detail: str = Field(default="", max_length=4000)

    @app.post("/api/deployments/{dep_id}/approvals", status_code=201)
    def request_approval(dep_id: str, body: ApprovalIn, c: Ctx = Depends(ctx)):
        c.deployment(dep_id)
        c.require_on("engine.run", dep_id)
        if not c.can_on("task.view_internal", dep_id):
            raise HTTPException(403, "approvals are internal")
        aid = new_id("apr")
        with db.tx(conn):
            conn.execute("INSERT INTO approvals (id, tenant_id, deployment_id, agent, request, detail, requested_by,"
                         " status, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                         (aid, c.tenant_id, dep_id, body.agent, body.request, body.detail, c.uid, "pending",
                          audit.now()))
            c.log("approval.request", aid, {"deployment": dep_id, "agent": body.agent, "request": body.request})
            d.emit(c, "approval.requested", dep_id, agent=body.agent, request=body.request, approval_id=aid)
        return {"id": aid, "status": "pending"}

    class DecideApproval(BaseModel):
        approve: bool
        note: str = Field(default="", max_length=500)

    @app.post("/api/approvals/{aid}/decide")
    def decide_approval(aid: str, body: DecideApproval, c: Ctx = Depends(ctx)):
        a = conn.execute("SELECT * FROM approvals WHERE id=? AND tenant_id=?", (aid, c.tenant_id)).fetchone()
        if not a:
            raise HTTPException(404, "approval not found")
        c.deployment(a["deployment_id"])
        if not c.can_on("task.view_internal", a["deployment_id"]):
            raise HTTPException(404, "approval not found")
        c.require_on("approval.decide", a["deployment_id"])
        if a["status"] != "pending":
            raise HTTPException(409, f"already {a['status']}")
        if a["requested_by"] == c.uid:
            raise HTTPException(403, "someone other than the person who asked has to decide")
        st = "approved" if body.approve else "rejected"
        with db.tx(conn):
            conn.execute("UPDATE approvals SET status=?, decided_by=?, decided_at=? WHERE id=?",
                         (st, c.uid, audit.now(), aid))
            c.log("approval.decide", aid, {"deployment": a["deployment_id"], "status": st, "note": body.note})
        return {"status": st}

    @app.get("/api/approvals/{aid}")
    def get_approval(aid: str, c: Ctx = Depends(ctx)):
        """Agents poll this after asking."""
        a = conn.execute("SELECT * FROM approvals WHERE id=? AND tenant_id=?", (aid, c.tenant_id)).fetchone()
        if not a:
            raise HTTPException(404, "approval not found")
        c.deployment(a["deployment_id"])
        if not c.can_on("task.view_internal", a["deployment_id"]):
            raise HTTPException(404, "approval not found")
        return approval_out(a, {}, users_map(c), c)

    # --------------------------------------------------------------- coverage

    @app.get("/api/coverage")
    def coverage(c: Ctx = Depends(ctx)):
        """What each screen is running on, for the integrations page."""
        c.require("integrations.manage")
        T = c.tenant_id

        def n(sql, *args):
            return conn.execute(sql, (T, *args)).fetchone()["n"]
        since = (now().date() - timedelta(days=30)).isoformat()
        secrets_set = {r["name"] for r in conn.execute("SELECT name FROM tenant_secrets WHERE tenant_id=?", (T,))}
        ints = c.cfg["integrations"]
        return {
            "deployments": n("SELECT COUNT(*) n FROM deployments WHERE tenant_id=?"),
            "tasks": n("SELECT COUNT(*) n FROM tasks WHERE tenant_id=?"),
            "tracker_links": {r["provider"]: r["n"] for r in conn.execute(
                "SELECT provider, COUNT(*) n FROM task_links WHERE tenant_id=? GROUP BY provider", (T,))},
            "synced_deployments": sum(1 for r in conn.execute("SELECT sync_json FROM deployments WHERE tenant_id=?", (T,))
                                      if json.loads(r["sync_json"] or "{}").get("provider")),
            "time_entries_30d": n("SELECT COUNT(*) n FROM time_entries WHERE tenant_id=? AND day>=?", since),
            "people_with_hours": len(delivery_people(conn, c.cfg, T)),
            "allocations": n("SELECT COUNT(*) n FROM deployment_members m JOIN deployments d ON d.id=m.deployment_id"
                             " WHERE d.tenant_id=? AND m.allocation > 0"),
            "opportunities": n("SELECT COUNT(*) n FROM opportunities WHERE tenant_id=?"),
            "delays_confirmed": n("SELECT COUNT(*) n FROM delays WHERE tenant_id=? AND status IN ('confirmed','reassigned')"),
            "delays_waiting": n("SELECT COUNT(*) n FROM delays WHERE tenant_id=? AND status='open'"),
            "findings": n("SELECT COUNT(*) n FROM findings WHERE tenant_id=?"),
            "personal_tokens": n("SELECT COUNT(*) n FROM personal_tokens WHERE tenant_id=?"),
            "custom_engines": len(c.cfg.get("engines", [])),
            "slack": {"enabled": bool(ints["slack"]["enabled"]), "connected": "slack_webhook_url" in secrets_set},
            "trackers": {p: {"enabled": bool(ints[p]["enabled"]),
                             "connected": {"github": "github_token", "linear": "linear_api_key",
                                           "jira": "jira_api_token"}[p] in secrets_set,
                             "inbound": f"{p}_webhook_secret" in secrets_set} for p in ("github", "linear", "jira")},
            "webhooks": len(ints.get("webhooks", [])),
            "sso": {"enabled": bool(c.cfg["sso"]["enabled"]), "required": bool(c.cfg["sso"]["required"])},
        }

    # -------------------------------------------------------------- checklist

    @app.get("/api/deployments/{dep_id}/checklist")
    def checklist(dep_id: str, c: Ctx = Depends(ctx)):
        c.deployment(dep_id)
        if not c.can_on("task.view_internal", dep_id):
            return []
        users = users_map(c)
        return [{**{k: r[k] for k in r.keys()}, "done": bool(r["done"]),
                 "done_by_name": users.get(r["done_by"]) if r["done_by"] else None}
                for r in conn.execute("SELECT * FROM checklist_items WHERE deployment_id=? ORDER BY position, created_at",
                                      (dep_id,))]

    class ItemIn(BaseModel):
        label: str | None = Field(default=None, max_length=200)
        defaults: bool = False

    @app.post("/api/deployments/{dep_id}/checklist", status_code=201)
    def add_item(dep_id: str, body: ItemIn, c: Ctx = Depends(ctx)):
        c.deployment(dep_id)
        c.require_on("deployment.edit", dep_id)
        if not c.can_on("task.view_internal", dep_id):
            raise HTTPException(403, "the checklist is internal")
        labels_ = CHECKLIST_DEFAULT if body.defaults else ([body.label.strip()] if body.label and body.label.strip() else [])
        if not labels_:
            raise HTTPException(422, "give the item a label, or ask for the defaults")
        pos = conn.execute("SELECT COALESCE(MAX(position), 0) p FROM checklist_items WHERE deployment_id=?",
                           (dep_id,)).fetchone()["p"]
        ids = []
        with db.tx(conn):
            for i, lab in enumerate(labels_, 1):
                iid = new_id("chk")
                conn.execute("INSERT INTO checklist_items (id, tenant_id, deployment_id, label, position, done,"
                             " created_at) VALUES (?,?,?,?,?,?,?)", (iid, c.tenant_id, dep_id, lab, pos + i, 0,
                                                                     audit.now()))
                ids.append(iid)
            c.log("checklist.add", dep_id, {"items": labels_})
        return {"ids": ids}

    class ItemPatch(BaseModel):
        done: bool

    def item_for(c, iid: str):
        r = conn.execute("SELECT * FROM checklist_items WHERE id=? AND tenant_id=?", (iid, c.tenant_id)).fetchone()
        if not r:
            raise HTTPException(404, "item not found")
        c.deployment(r["deployment_id"])
        if not c.can_on("task.view_internal", r["deployment_id"]):
            raise HTTPException(404, "item not found")
        c.require_on("deployment.edit", r["deployment_id"])
        return r

    @app.patch("/api/checklist/{iid}")
    def tick(iid: str, body: ItemPatch, c: Ctx = Depends(ctx)):
        r = item_for(c, iid)
        with db.tx(conn):
            conn.execute("UPDATE checklist_items SET done=?, done_by=?, done_at=? WHERE id=?",
                         (1 if body.done else 0, c.uid if body.done else None,
                          audit.now() if body.done else None, iid))
            c.log("checklist.tick", r["deployment_id"], {"item": r["label"], "done": body.done})
        return {"ok": True}

    @app.delete("/api/checklist/{iid}")
    def remove_item(iid: str, c: Ctx = Depends(ctx)):
        r = item_for(c, iid)
        with db.tx(conn):
            conn.execute("DELETE FROM checklist_items WHERE id=?", (iid,))
            c.log("checklist.remove", r["deployment_id"], {"item": r["label"]})
        return {"ok": True}


# ======================================================================= import

def import_rows(conn, c, kind: str, rows: list, people: dict, visible: list = ()) -> tuple[int, list]:
    """Extra /api/import kinds: time (timesheets) and pipeline (CRM export).
    `visible` is the caller's visible deployments; time can only land on ones they can edit."""
    created, errors = 0, []
    if kind == "time":
        c.require("people.read")
        need = {"email", "day", "hours"}
        if need - set(rows[0]):
            raise HTTPException(422, f"missing column(s): {', '.join(sorted(need - set(rows[0])))}")
        deps = {r["name"].lower(): r for r in visible
                if c.can_on("task.view_internal", r["id"]) and c.can_on("deployment.edit", r["id"])}
        with db.tx(conn):
            for i, r in enumerate(rows, 2):
                u = people.get(r["email"].lower())
                if not u:
                    errors.append(f"row {i}: nobody with email {r['email']}"); continue
                if not parse_day(r["day"]):
                    errors.append(f"row {i}: day must be YYYY-MM-DD"); continue
                try:
                    h = float(r["hours"])
                    assert 0 < h <= 24
                except (ValueError, AssertionError):
                    errors.append(f"row {i}: hours must be between 0 and 24"); continue
                dep = deps.get(r.get("deployment", "").lower()) if r.get("deployment") else None
                if r.get("deployment") and not dep:
                    errors.append(f"row {i}: no deployment {r['deployment']!r} you can edit"); continue
                if not dep and not c.can("people.manage"):
                    errors.append(f"row {i}: name a deployment you run, or ask an admin to import internal time"); continue
                conn.execute("INSERT INTO time_entries (tenant_id, user_id, deployment_id, day, hours, source,"
                             " created_at) VALUES (?,?,?,?,?,?,?)",
                             (c.tenant_id, u["id"], dep["id"] if dep else None, r["day"][:10], h, "import",
                              audit.now()))
                created += 1
            c.log("time.import", c.tenant_id, {"rows": created})
        return created, errors
    if kind == "pipeline":
        c.require("pipeline.edit")
        if "name" not in rows[0]:
            raise HTTPException(422, "missing column(s): name")
        with db.tx(conn):
            for i, r in enumerate(rows, 2):
                stage = (r.get("stage") or "lead").lower()
                stage = next((k for k, v in PIPELINE_LABELS.items() if stage in (k, v.lower())), None)
                if not r["name"]:
                    errors.append(f"row {i}: name is required"); continue
                if not stage:
                    errors.append(f"row {i}: unknown stage {r.get('stage')!r}"); continue
                try:
                    value = float((r.get("value") or "0").replace(",", "").replace("$", ""))
                    prob = float((r.get("probability") or "0").rstrip("%"))
                    prob = prob / 100 if prob > 1 else prob
                    hours = float(r.get("weekly_hours") or 0)
                    if not all(math.isfinite(x) for x in (value, prob, hours)):
                        raise ValueError
                except ValueError:
                    errors.append(f"row {i}: value, probability and weekly_hours must be numbers"); continue
                if not (0 <= value <= 1e12 and 0 <= prob <= 1 and 0 <= hours <= 400):
                    errors.append(f"row {i}: value must be 0 or more, probability 0-100%, weekly_hours 0-400"); continue
                start = r.get("expected_start") or None
                if start and not parse_day(start):
                    errors.append(f"row {i}: expected_start must be YYYY-MM-DD"); continue
                ext = r.get("external_id") or None
                ex = (conn.execute("SELECT id FROM opportunities WHERE tenant_id=? AND external_id=?",
                                   (c.tenant_id, ext)).fetchone() if ext else None) or conn.execute(
                    "SELECT id FROM opportunities WHERE tenant_id=? AND lower(name)=?",
                    (c.tenant_id, r["name"].lower())).fetchone()
                vals = (r["name"], r.get("customer", ""), r.get("use_case", ""), value, prob, stage, start, hours,
                        "import", ext, audit.now())
                if ex:
                    conn.execute("UPDATE opportunities SET name=?, customer=?, use_case=?, value=?, probability=?,"
                                 " stage=?, expected_start=?, weekly_hours=?, source=?, external_id=?, updated_at=?"
                                 " WHERE id=?", (*vals, ex["id"]))
                else:
                    conn.execute("INSERT INTO opportunities (id, tenant_id, name, customer, use_case, value,"
                                 " probability, stage, expected_start, weekly_hours, source, external_id, updated_at)"
                                 " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (new_id("opp"), c.tenant_id, *vals))
                created += 1
            c.log("pipeline.import", c.tenant_id, {"rows": created})
        return created, errors
    raise HTTPException(422, "kind must be deployments, tasks, time or pipeline")
