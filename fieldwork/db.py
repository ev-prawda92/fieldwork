"""Storage for Fieldwork.

SQLite by default so the whole platform runs from one file with no services.
The SQL is kept portable (no SQLite-only features beyond AUTOINCREMENT) so the
same schema moves to Postgres when a customer needs it.

Every row that belongs to a customer workspace carries tenant_id. Queries in
the API layer always filter on it; the tests prove one tenant can never read
another's rows.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS tenants (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    config_json TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id          TEXT PRIMARY KEY,
    tenant_id   TEXT NOT NULL REFERENCES tenants(id),
    name        TEXT NOT NULL,
    email       TEXT NOT NULL,
    role        TEXT NOT NULL,              -- a role key from the tenant's config
    manager_id  TEXT,
    token_hash  TEXT NOT NULL UNIQUE,
    profile_json TEXT NOT NULL DEFAULT '{}', -- bench evidence corpus (Threshold)
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS customers (
    id          TEXT PRIMARY KEY,
    tenant_id   TEXT NOT NULL REFERENCES tenants(id),
    name        TEXT NOT NULL,
    industry    TEXT NOT NULL DEFAULT '',
    fields_json TEXT NOT NULL DEFAULT '{}',
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS deployments (
    id           TEXT PRIMARY KEY,
    tenant_id    TEXT NOT NULL REFERENCES tenants(id),
    customer_id  TEXT NOT NULL REFERENCES customers(id),
    name         TEXT NOT NULL,
    stage        TEXT NOT NULL,
    health       TEXT NOT NULL DEFAULT 'on_track',  -- on_track | at_risk | blocked
    lead_id      TEXT REFERENCES users(id),
    fields_json  TEXT NOT NULL DEFAULT '{}',
    staffing_req TEXT NOT NULL DEFAULT '',          -- requirement text for Bench
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS deployment_members (
    deployment_id TEXT NOT NULL REFERENCES deployments(id),
    user_id       TEXT NOT NULL REFERENCES users(id),
    PRIMARY KEY (deployment_id, user_id)
);

CREATE TABLE IF NOT EXISTS stage_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id     TEXT NOT NULL,
    deployment_id TEXT NOT NULL REFERENCES deployments(id),
    from_stage    TEXT,
    to_stage      TEXT NOT NULL,
    actor_id      TEXT NOT NULL,
    note          TEXT NOT NULL DEFAULT '',
    at            TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    id            TEXT PRIMARY KEY,
    tenant_id     TEXT NOT NULL,
    deployment_id TEXT NOT NULL REFERENCES deployments(id),
    stage         TEXT NOT NULL,
    title         TEXT NOT NULL,
    assignee_id   TEXT REFERENCES users(id),
    status        TEXT NOT NULL DEFAULT 'open',   -- open | in_progress | blocked | done
    due           TEXT,
    created_by    TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS findings (
    id            TEXT PRIMARY KEY,
    tenant_id     TEXT NOT NULL,
    deployment_id TEXT NOT NULL REFERENCES deployments(id),
    engine        TEXT NOT NULL,                  -- sendero | threshold | ...
    title         TEXT NOT NULL,
    result_json   TEXT NOT NULL,
    confirmed_by  TEXT,
    confirmed_at  TEXT,
    created_by    TEXT NOT NULL,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit (
    seq        INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id  TEXT NOT NULL,
    at         TEXT NOT NULL,
    actor_id   TEXT NOT NULL,
    action     TEXT NOT NULL,
    subject    TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    prev_hash  TEXT NOT NULL,
    hash       TEXT NOT NULL
);

-- Credentials for customer-registered engines. Webhook engines need the raw
-- signing secret to sign calls (encrypt at rest with a KMS key in production);
-- push engines store only a hash of their token.
CREATE TABLE IF NOT EXISTS engine_credentials (
    tenant_id   TEXT NOT NULL REFERENCES tenants(id),
    engine_key  TEXT NOT NULL,
    secret      TEXT,
    token_hash  TEXT UNIQUE,
    created_at  TEXT NOT NULL,
    PRIMARY KEY (tenant_id, engine_key)
);

CREATE INDEX IF NOT EXISTS ix_users_tenant ON users(tenant_id);
CREATE INDEX IF NOT EXISTS ix_deploy_tenant ON deployments(tenant_id);
CREATE INDEX IF NOT EXISTS ix_tasks_tenant ON tasks(tenant_id, assignee_id);
CREATE INDEX IF NOT EXISTS ix_findings_dep ON findings(tenant_id, deployment_id);
CREATE INDEX IF NOT EXISTS ix_audit_tenant ON audit(tenant_id, seq);
"""

_lock = threading.RLock()


def db_path() -> str:
    return os.environ.get("FIELDWORK_DB", str(Path.cwd() / "fieldwork.db"))


def connect(path: str | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(path or db_path(), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


@contextmanager
def tx(conn: sqlite3.Connection):
    """One write transaction. The lock keeps the audit chain strictly ordered."""
    with _lock:
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
