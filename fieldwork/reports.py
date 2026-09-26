"""Status reports, drafted from what's already in Fieldwork.

Two audiences from the same deployment:
  internal   everything: blockers with owners, internal findings, what needs deciding
  customer   only what's shared with the customer, written to be forwarded as is

Deterministic: every line comes from a task, stage move or confirmed finding,
so a report never says something the record doesn't.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone


def _d(s: str | None) -> str:
    return (s or "")[:10]


def build(conn, cfg: dict, dep, audience: str) -> str:
    now = datetime.now(timezone.utc)
    week = (now - timedelta(days=7)).isoformat()
    soon = (now + timedelta(days=14)).date().isoformat()
    today = now.date().isoformat()
    shared_only = audience == "customer"
    vis = " AND t.visibility='shared'" if shared_only else ""
    fvis = " AND visibility='shared'" if shared_only else ""
    stages = cfg["stages"]
    keys = [s["key"] for s in stages]
    idx = keys.index(dep["stage"]) + 1 if dep["stage"] in keys else 0
    stage_name = next((s["name"] for s in stages if s["key"] == dep["stage"]), dep["stage"])
    cust = conn.execute("SELECT name FROM customers WHERE id=?", (dep["customer_id"],)).fetchone()
    fields = json.loads(dep["fields_json"] or "{}")

    def tasks(where: str, args: tuple = ()):
        return list(conn.execute(
            "SELECT t.*, u.name assignee_name, u.role assignee_role FROM tasks t LEFT JOIN users u ON u.id=t.assignee_id"
            f" WHERE t.deployment_id=?{vis} AND {where} ORDER BY t.due", (dep["id"], *args)))

    done = tasks("t.status='done' AND t.updated_at>=?", (week,))
    blocked = tasks("t.status='blocked'")
    upcoming = tasks("t.status IN ('open','in_progress') AND t.due IS NOT NULL AND t.due<=?", (soon,))
    overdue = {t["id"] for t in upcoming if t["due"] < today}
    moves = list(conn.execute("SELECT * FROM stage_events WHERE deployment_id=? AND at>=? AND from_stage IS NOT NULL"
                              " ORDER BY id", (dep["id"], week)))
    findings = list(conn.execute(
        f"SELECT * FROM findings WHERE deployment_id=? AND confirmed_by IS NOT NULL{fvis}"
        " ORDER BY created_at DESC LIMIT 5", (dep["id"],)))
    waiting = 0 if shared_only else conn.execute(
        "SELECT COUNT(*) n FROM findings WHERE deployment_id=? AND confirmed_by IS NULL", (dep["id"],)).fetchone()["n"]
    name = lambda k: next((s["name"] for s in stages if s["key"] == k), k)

    out = [f"# {dep['name']} · {cust['name'] if cust else ''}",
           f"Status update · {now.strftime('%B %-d, %Y')}", "",
           f"**Stage:** {stage_name} ({idx} of {len(stages)})  ",
           f"**Health:** {dep['health'].replace('_', ' ')}" +
           (f"  \n**Target go-live:** {fields['target_golive']}" if fields.get("target_golive") else ""), ""]

    out.append("## This week")
    items = [f"- Moved from {name(m['from_stage'])} to {name(m['to_stage'])}" + (f": {m['note']}" if m["note"] else "")
             for m in moves]
    items += [f"- Done: {t['title']}" + ("" if shared_only else f" ({t['assignee_name'] or 'unassigned'})") for t in done]
    out += items or ["- No completed items this week."]
    out.append("")

    if blocked:
        out.append("## Blocked")
        out += [f"- {t['title']} (owner: {t['assignee_name'] or 'unassigned'})" for t in blocked]
        out.append("")

    out.append("## Coming up")
    out += [f"- {t['title']} · due {t['due']}" + (" · **overdue**" if t["id"] in overdue else "") +
            f" ({t['assignee_name'] or 'unassigned'})" for t in upcoming] or ["- Nothing due in the next two weeks."]
    out.append("")

    if findings:
        out.append("## Findings" if not shared_only else "## Results")
        for f in findings:
            r = json.loads(f["result_json"])
            summary = r.get("summary") or (f"{r.get('classification')} ({r.get('confidence_pct')}% confidence)"
                                           if f["engine"] == "sendero" else f"{len(r.get('ranked', []))} people scored"
                                           if f["engine"] == "threshold" else "")
            out.append(f"- **{f['title']}**: {summary}")
        out.append("")

    asks = [t for t in tasks("t.status!='done'") if t["assignee_role"] == "customer"]
    if asks or (waiting and not shared_only):
        out.append("## Needed from you" if shared_only else "## Decisions and asks")
        out += [f"- {t['title']}" + (f" · due {t['due']}" if t["due"] else "") + f" ({t['assignee_name']})" for t in asks]
        if waiting and not shared_only:
            out.append(f"- {waiting} engine finding(s) waiting for confirmation")
        out.append("")
    return "\n".join(out).rstrip() + "\n"
