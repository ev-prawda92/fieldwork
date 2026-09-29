"""Events, the outbox, and everything that leaves Fieldwork.

When something happens (a task is blocked, a finding needs confirming, a
deployment moves), the API calls emit() inside the same transaction as the
change. emit() writes rows to the outbox: one per Slack post, event webhook or
tracker sync. Nothing calls out over the network while a request is open.

A worker drains the outbox (a background thread in `serve`, or
`python -m fieldwork worker`), with exponential backoff on failure. If Slack
or GitHub is down, work queues up and goes out when they're back.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from . import config, crypto, plugins

log = logging.getLogger("fieldwork.events")
MAX_ATTEMPTS = 6


def _now() -> datetime:
    return datetime.now(timezone.utc)


def public_url() -> str:
    return os.environ.get("FIELDWORK_PUBLIC_URL", "http://127.0.0.1:8000").rstrip("/")


def dep_link(dep_id: str) -> str:
    return f"{public_url()}/#dep={dep_id}"


def secret(conn, tenant_id: str, name: str) -> str | None:
    row = conn.execute("SELECT secret FROM tenant_secrets WHERE tenant_id=? AND name=?",
                       (tenant_id, name)).fetchone()
    return crypto.decrypt(row["secret"]) if row else None


def set_secret(conn, tenant_id: str, name: str, value: str | None) -> None:
    if value is None:
        conn.execute("DELETE FROM tenant_secrets WHERE tenant_id=? AND name=?", (tenant_id, name))
        return
    conn.execute("INSERT INTO tenant_secrets (tenant_id, name, secret, created_at) VALUES (?,?,?,?)"
                 " ON CONFLICT (tenant_id, name) DO UPDATE SET secret=excluded.secret, created_at=excluded.created_at",
                 (tenant_id, name, crypto.encrypt(value), _now().isoformat()))


def enqueue(conn, tenant_id: str, kind: str, payload: dict) -> None:
    ts = _now().isoformat()
    conn.execute("INSERT INTO outbox (tenant_id, kind, payload_json, status, attempts, next_at, created_at)"
                 " VALUES (?,?,?,?,?,?,?)", (tenant_id, kind, json.dumps(payload), "pending", 0, ts, ts))


def emit(conn, cfg: dict, tenant_id: str, event: str, data: dict) -> None:
    """Fan an event out to Slack and subscribed webhooks. Call inside the change's transaction."""
    if event not in config.EVENTS:
        raise ValueError(f"unknown event {event}")
    ints = cfg.get("integrations", {})
    slack = ints.get("slack", {})
    if slack.get("enabled") and event in slack.get("events", []):
        enqueue(conn, tenant_id, "slack", {"event": event, "data": data})
    if event == "task.assigned" and data.get("assignee_id") and conn.execute(
            "SELECT 1 FROM connections WHERE tenant_id=? AND provider='slack' AND status='active' AND user_id IS NULL",
            (tenant_id,)).fetchone():
        enqueue(conn, tenant_id, "slack_dm", {"event": event, "data": data})
    for h in ints.get("webhooks", []):
        if event in h["events"]:
            enqueue(conn, tenant_id, "webhook", {"hook": h["id"], "url": h["url"], "event": event, "data": data})


# ------------------------------------------------------------------ delivery

def _allow_private() -> bool:
    return os.environ.get("FIELDWORK_ALLOW_PRIVATE_ENGINES") == "1"


def http_json(url: str, body: dict | None, headers: dict | None = None, method: str = "POST",
              timeout: int = 20) -> tuple[int, dict | str]:
    """Outbound call with the same SSRF guard, no redirects and size cap as engine webhooks.
    Goes through connect.http's transport, so tests can stand in for any service."""
    from .connect import http as chttp
    try:
        r = chttp.request(method, url, json_body=body, headers=headers, timeout=timeout)
    except chttp.HTTPError as e:
        raise plugins.PluginError(str(e))
    if not r.body:
        return r.status, {}
    try:
        return r.status, json.loads(r.body)
    except ValueError:
        return r.status, r.body.decode(errors="replace")[:500]


def slack_text(event: str, d: dict) -> str:
    link = f"<{dep_link(d['deployment_id'])}|{d.get('deployment', 'deployment')}>" if d.get("deployment_id") else ""
    who = d.get("actor", "Someone")
    return {
        "task.assigned": f":inbox_tray: *{d.get('assignee')}* was assigned “{d.get('title')}” · {link}",
        "task.blocked": f":no_entry: *Blocked:* “{d.get('title')}” ({d.get('assignee') or 'unassigned'}) · {link}",
        "task.done": f":white_check_mark: {who} finished “{d.get('title')}” · {link}",
        "finding.created": f":mag: *{d.get('engine_name')}* result waiting for confirmation: {d.get('summary', '')} · {link}",
        "finding.confirmed": f":ballot_box_with_check: {who} confirmed “{d.get('title')}” · {link}",
        "finding.shared": f":handshake: {who} shared “{d.get('title')}” with the customer · {link}",
        "deployment.advanced": f":arrow_right: {link} moved to *{d.get('to_name')}* ({who})",
        "deployment.health": f":warning: {link} is now *{str(d.get('health', '')).replace('_', ' ')}* ({who})",
        "report.created": f":memo: {who} drafted a {d.get('audience')} status report for {link}",
        "flag.raised": f":triangular_flag_on_post: *{str(d.get('severity', '')).upper()}* flag on {link}: {d.get('text')} ({who})",
        "delay.opened": f":hourglass_flowing_sand: Delay on {link}: {d.get('reason')}. Proposed owner *{d.get('owner')}*; confirm or reassign it in the console.",
        "approval.requested": f":raised_hand: *{d.get('agent')}* is asking to {d.get('request')} on {link} ({who}); approve or reject in the console.",
        "milestone.ready": f":moneybag: *{d.get('milestone')}* ({d.get('amount_text')}) is ready to submit for sign-off · {link}",
        "milestone.submitted": f":envelope_with_arrow: {who} submitted *{d.get('milestone')}* ({d.get('amount_text')}) for the customer's sign-off · {link}",
        "milestone.accepted": f":white_check_mark: *{d.get('milestone')}* was signed off by {who}. {d.get('amount_text')} is ready to invoice · {link}",
        "milestone.changes_requested": f":leftwards_arrow_with_hook: {who} asked for changes to *{d.get('milestone')}*: {d.get('note')} · {link}",
        "digest": d.get("text", ""),
    }[event]


def sign_webhook(secret_value: str, ts: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret_value.encode(), ts.encode() + b"." + body, hashlib.sha256).hexdigest()


def _deliver(conn, row) -> None:
    p = json.loads(row["payload_json"])
    kind = row["kind"]
    if kind in ("slack", "slack_dm"):
        from .connect import slack as slack_app
        if slack_app.deliver(conn, row["tenant_id"], kind, p) or kind == "slack_dm":
            return
        url = secret(conn, row["tenant_id"], "slack_webhook_url")
        if not url:
            raise plugins.PluginError("Slack isn't connected (no webhook URL)")
        code, resp = http_json(url, {"text": slack_text(p["event"], p["data"])})
        if code >= 300:
            raise plugins.PluginError(f"Slack returned {code}: {resp}")
    elif kind == "webhook":
        s = secret(conn, row["tenant_id"], "webhook:" + p["hook"]) or ""
        body = json.dumps({"event": p["event"], "data": p["data"], "workspace": row["tenant_id"],
                           "id": row["id"]}, sort_keys=True).encode()
        ts = str(int(time.time()))
        plugins._guard_host(p["url"], _allow_private())
        req = urllib.request.Request(p["url"], data=body, method="POST", headers={
            "Content-Type": "application/json", "User-Agent": "Fieldwork/1",
            "X-Fieldwork-Event": p["event"], "X-Fieldwork-Timestamp": ts,
            "X-Fieldwork-Signature": sign_webhook(s, ts, body)})
        try:
            with plugins._opener.open(req, timeout=20) as r:
                if r.status >= 300:
                    raise plugins.PluginError(f"webhook returned {r.status}")
        except urllib.error.HTTPError as e:
            raise plugins.PluginError(f"webhook returned {e.code}")
        except (urllib.error.URLError, OSError) as e:
            raise plugins.PluginError(f"couldn't reach webhook: {getattr(e, 'reason', e)}")
    elif kind == "tracker":
        from . import trackers
        trackers.deliver(conn, row["tenant_id"], p)
    elif kind == "billing_push":
        from .connect import billing
        billing.push(conn, row["tenant_id"], p)
    else:
        raise plugins.PluginError(f"unknown outbox kind {kind}")


def process(conn, limit: int = 50) -> dict:
    """Deliver due outbox rows. Safe to call from several workers (rows are claimed first)."""
    now = _now().isoformat()
    stale = (_now() - timedelta(minutes=15)).isoformat()
    with conn.tx():  # recover rows a crashed worker had claimed
        conn.execute("UPDATE outbox SET status='pending' WHERE status='working' AND next_at<?", (stale,))
    rows = list(conn.execute("SELECT * FROM outbox WHERE status='pending' AND next_at<=? ORDER BY id LIMIT ?",
                             (now, limit)))
    sent = failed = 0
    for row in rows:
        with conn.tx():
            claimed = conn.execute("UPDATE outbox SET status='working' WHERE id=? AND status='pending'",
                                   (row["id"],)).rowcount
        if not claimed:
            continue
        try:
            _deliver(conn, row)
            with conn.tx():
                conn.execute("UPDATE outbox SET status='sent', attempts=attempts+1, last_error=NULL WHERE id=?",
                             (row["id"],))
            sent += 1
        except Exception as e:  # any failure is retried, then parked with its reason
            attempts = row["attempts"] + 1
            status = "failed" if attempts >= MAX_ATTEMPTS else "pending"
            delay = timedelta(seconds=min(3600, 30 * 2 ** attempts))
            with conn.tx():
                conn.execute("UPDATE outbox SET status=?, attempts=?, next_at=?, last_error=? WHERE id=?",
                             (status, attempts, (_now() + delay).isoformat(), str(e)[:500], row["id"]))
            failed += 1
            log.warning("outbox %s (%s) failed: %s", row["id"], row["kind"], e)
    return {"sent": sent, "failed": failed}


def start_worker(conn, interval: float = 5.0) -> threading.Event:
    stop = threading.Event()

    def loop():
        while not stop.is_set():
            try:
                process(conn)
            except Exception as e:
                log.warning("outbox worker error: %s", e)
            stop.wait(interval)

    threading.Thread(target=loop, name="fieldwork-outbox", daemon=True).start()
    return stop


# -------------------------------------------------------------------- digest

def digest_text(conn, tenant_id: str, cfg: dict) -> str:
    today = _now().date().isoformat()
    lines = [f"*{cfg['branding']['product_name']} · daily digest*"]
    deps = list(conn.execute("SELECT * FROM deployments WHERE tenant_id=? ORDER BY name", (tenant_id,)))
    stage_names = {s["key"]: s["name"] for s in cfg["stages"]}
    for d in deps:
        blocked = list(conn.execute("SELECT t.title, u.name FROM tasks t LEFT JOIN users u ON u.id=t.assignee_id"
                                    " WHERE t.deployment_id=? AND t.status='blocked'", (d["id"],)))
        overdue = conn.execute("SELECT COUNT(*) n FROM tasks WHERE deployment_id=? AND status!='done'"
                               " AND due IS NOT NULL AND due<?", (d["id"], today)).fetchone()["n"]
        waiting = conn.execute("SELECT COUNT(*) n FROM findings WHERE deployment_id=? AND confirmed_by IS NULL",
                               (d["id"],)).fetchone()["n"]
        if not (blocked or overdue or waiting or d["health"] != "on_track"):
            continue
        bits = [f"*<{dep_link(d['id'])}|{d['name']}>* · {stage_names.get(d['stage'], d['stage'])} · "
                f"{d['health'].replace('_', ' ')}"]
        bits += [f"    :no_entry: {b['title']} ({b['name'] or 'unassigned'})" for b in blocked]
        if overdue:
            bits.append(f"    :hourglass: {overdue} overdue task(s)")
        if waiting:
            bits.append(f"    :mag: {waiting} finding(s) waiting for confirmation")
        lines += bits
    if len(lines) == 1:
        lines.append("All deployments on track. Nothing blocked, overdue or waiting.")
    return "\n".join(lines)


def queue_digests(conn) -> int:
    n = 0
    for t in list(conn.execute("SELECT * FROM tenants")):
        cfg = config.upgrade(json.loads(t["config_json"]))
        if cfg["integrations"]["slack"]["enabled"]:
            with conn.tx():
                enqueue(conn, t["id"], "slack", {"event": "digest", "data": {"text": digest_text(conn, t["id"], cfg)}})
            n += 1
    return n
