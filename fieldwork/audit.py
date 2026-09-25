"""Tamper-evident audit trail, one hash chain per tenant.

Same construction Arbiter uses for resolution records: each entry's hash covers
its own canonical content plus the previous entry's hash, so editing or
deleting any row breaks every hash after it. verify() walks the chain and
reports the first break.

Every state change in Fieldwork (stage moves, assignments, engine runs,
confirmations, config edits) goes through record(), inside the same
transaction as the change itself. No change, no entry; no entry, no change.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

GENESIS = "sha256:" + "0" * 64


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def entry_hash(prev_hash: str, tenant_id: str, at: str, actor_id: str,
               action: str, subject: str, detail: dict) -> str:
    body = canonical({
        "prev": prev_hash, "tenant": tenant_id, "at": at, "actor": actor_id,
        "action": action, "subject": subject, "detail": detail,
    })
    return "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest()


def _head(conn: sqlite3.Connection, tenant_id: str) -> str:
    row = conn.execute(
        "SELECT hash FROM audit WHERE tenant_id=? ORDER BY seq DESC LIMIT 1",
        (tenant_id,),
    ).fetchone()
    return row["hash"] if row else GENESIS


def record(conn: sqlite3.Connection, tenant_id: str, actor_id: str,
           action: str, subject: str, detail: dict | None = None) -> str:
    """Append one entry. Call inside db.tx() with the change it describes."""
    detail = detail or {}
    at = now()
    prev = _head(conn, tenant_id)
    h = entry_hash(prev, tenant_id, at, actor_id, action, subject, detail)
    conn.execute(
        "INSERT INTO audit (tenant_id, at, actor_id, action, subject, detail_json, prev_hash, hash)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (tenant_id, at, actor_id, action, subject, canonical(detail), prev, h),
    )
    return h


def verify(conn: sqlite3.Connection, tenant_id: str) -> dict:
    rows = conn.execute(
        "SELECT * FROM audit WHERE tenant_id=? ORDER BY seq", (tenant_id,)
    ).fetchall()
    prev = GENESIS
    for i, r in enumerate(rows):
        detail = json.loads(r["detail_json"])
        expect = entry_hash(prev, tenant_id, r["at"], r["actor_id"], r["action"],
                            r["subject"], detail)
        if r["prev_hash"] != prev or r["hash"] != expect:
            return {"ok": False, "entries": len(rows), "broken_at": r["seq"],
                    "position": i, "head": prev}
        prev = r["hash"]
    return {"ok": True, "entries": len(rows), "broken_at": None, "head": prev}
