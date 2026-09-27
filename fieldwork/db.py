"""Storage for Fieldwork: SQLite for demos and single-box installs, Postgres for production.

    FIELDWORK_DATABASE_URL=postgresql://user:pass@host/db    # Postgres
    FIELDWORK_DB=/path/fieldwork.db                          # SQLite (default ./fieldwork.db)

The API writes portable SQL with "?" placeholders; the Postgres adapter
translates them. Schema changes are versioned migrations recorded in
schema_migrations, applied in order at startup, each in its own transaction.

Every row that belongs to a customer workspace carries tenant_id. Queries in
the API layer always filter on it; the tests prove one tenant can never read
another's rows, on both databases.

Concurrency: one shared connection per process, serialized by a lock, with each
write in an explicit transaction. That's correct and simple; a connection pool
is the next step when one API process isn't enough. The audit chain also takes
a per-tenant Postgres advisory lock (see audit.py), so several API processes
can't fork a tenant's chain.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

# ----------------------------------------------------------------- migrations
# {AUTO} becomes the dialect's auto-increment primary key.

MIGRATIONS: list[tuple[int, str, str]] = [
    (1, "initial schema", """
CREATE TABLE IF NOT EXISTS tenants (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    config_json TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS users (
    id           TEXT PRIMARY KEY,
    tenant_id    TEXT NOT NULL REFERENCES tenants(id),
    name         TEXT NOT NULL,
    email        TEXT NOT NULL,
    role         TEXT NOT NULL,
    manager_id   TEXT,
    token_hash   TEXT NOT NULL UNIQUE,
    profile_json TEXT NOT NULL DEFAULT '{}',
    created_at   TEXT NOT NULL
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
    health       TEXT NOT NULL DEFAULT 'on_track',
    lead_id      TEXT REFERENCES users(id),
    fields_json  TEXT NOT NULL DEFAULT '{}',
    staffing_req TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS deployment_members (
    deployment_id TEXT NOT NULL REFERENCES deployments(id),
    user_id       TEXT NOT NULL REFERENCES users(id),
    PRIMARY KEY (deployment_id, user_id)
);
CREATE TABLE IF NOT EXISTS stage_events (
    id            {AUTO},
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
    status        TEXT NOT NULL DEFAULT 'open',
    due           TEXT,
    created_by    TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS findings (
    id            TEXT PRIMARY KEY,
    tenant_id     TEXT NOT NULL,
    deployment_id TEXT NOT NULL REFERENCES deployments(id),
    engine        TEXT NOT NULL,
    title         TEXT NOT NULL,
    result_json   TEXT NOT NULL,
    confirmed_by  TEXT,
    confirmed_at  TEXT,
    created_by    TEXT NOT NULL,
    created_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit (
    seq         {AUTO},
    tenant_id   TEXT NOT NULL,
    at          TEXT NOT NULL,
    actor_id    TEXT NOT NULL,
    action      TEXT NOT NULL,
    subject     TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    prev_hash   TEXT NOT NULL,
    hash        TEXT NOT NULL
);
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
CREATE INDEX IF NOT EXISTS ix_audit_tenant ON audit(tenant_id, seq)
"""),
    (2, "customer-safe visibility", """
ALTER TABLE tasks ADD COLUMN visibility TEXT NOT NULL DEFAULT 'internal';
ALTER TABLE findings ADD COLUMN visibility TEXT NOT NULL DEFAULT 'internal'
"""),
    (3, "company sign-in", """
ALTER TABLE tenants ADD COLUMN slug TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS ux_tenants_slug ON tenants(slug);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash  TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL REFERENCES users(id),
    tenant_id   TEXT NOT NULL,
    method      TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    expires_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sso_states (
    state         TEXT PRIMARY KEY,
    tenant_id     TEXT NOT NULL,
    nonce         TEXT NOT NULL,
    code_verifier TEXT NOT NULL,
    created_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tenant_secrets (
    tenant_id  TEXT NOT NULL REFERENCES tenants(id),
    name       TEXT NOT NULL,
    secret     TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, name)
);
CREATE INDEX IF NOT EXISTS ix_sessions_user ON sessions(user_id)
"""),
    (4, "launch: notifications, tracker sync, reports, personal tokens", """
CREATE TABLE IF NOT EXISTS outbox (
    id          {AUTO},
    tenant_id   TEXT NOT NULL,
    kind        TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending',
    attempts    INTEGER NOT NULL DEFAULT 0,
    next_at     TEXT NOT NULL,
    last_error  TEXT,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_outbox_due ON outbox(status, next_at);
CREATE TABLE IF NOT EXISTS task_links (
    task_id     TEXT NOT NULL REFERENCES tasks(id),
    tenant_id   TEXT NOT NULL,
    provider    TEXT NOT NULL,
    external_id TEXT NOT NULL,
    url         TEXT NOT NULL DEFAULT '',
    synced_at   TEXT NOT NULL,
    PRIMARY KEY (task_id, provider)
);
CREATE INDEX IF NOT EXISTS ix_links_ext ON task_links(tenant_id, provider, external_id);
CREATE TABLE IF NOT EXISTS reports (
    id            TEXT PRIMARY KEY,
    tenant_id     TEXT NOT NULL,
    deployment_id TEXT NOT NULL REFERENCES deployments(id),
    audience      TEXT NOT NULL,
    body_md       TEXT NOT NULL,
    visibility    TEXT NOT NULL DEFAULT 'internal',
    created_by    TEXT NOT NULL,
    created_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS personal_tokens (
    token_hash  TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL REFERENCES users(id),
    tenant_id   TEXT NOT NULL,
    name        TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    last_used_at TEXT
);
ALTER TABLE deployments ADD COLUMN sync_json TEXT NOT NULL DEFAULT '{}'
"""),
    (5, "delivery operations: delay ledger, capacity, pipeline, flags, approvals, checklist", """
ALTER TABLE users ADD COLUMN weekly_hours REAL NOT NULL DEFAULT 40;
ALTER TABLE deployments ADD COLUMN start_on TEXT;
ALTER TABLE deployments ADD COLUMN end_on TEXT;
ALTER TABLE deployments ADD COLUMN budget_hours REAL;
ALTER TABLE deployment_members ADD COLUMN allocation REAL NOT NULL DEFAULT 0;
CREATE TABLE IF NOT EXISTS delays (
    id              TEXT PRIMARY KEY,
    tenant_id       TEXT NOT NULL,
    deployment_id   TEXT NOT NULL REFERENCES deployments(id),
    stage           TEXT NOT NULL,
    signal          TEXT NOT NULL,
    started_at      TEXT NOT NULL,
    ended_at        TEXT,
    proposed_owner  TEXT NOT NULL,
    proposed_reason TEXT NOT NULL,
    proposal_basis  TEXT NOT NULL DEFAULT '',
    evidence        TEXT NOT NULL DEFAULT '',
    dedupe_key      TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'open',
    confirmed_owner TEXT,
    confirmed_reason TEXT,
    confirmed_by    TEXT,
    confirmed_at    TEXT,
    weight          REAL NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_delays_key ON delays(tenant_id, dedupe_key);
CREATE INDEX IF NOT EXISTS ix_delays_dep ON delays(tenant_id, deployment_id);
CREATE TABLE IF NOT EXISTS time_entries (
    id            {AUTO},
    tenant_id     TEXT NOT NULL,
    user_id       TEXT NOT NULL REFERENCES users(id),
    deployment_id TEXT,
    day           TEXT NOT NULL,
    hours         REAL NOT NULL,
    source        TEXT NOT NULL DEFAULT 'import',
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_time_user ON time_entries(tenant_id, user_id, day);
CREATE TABLE IF NOT EXISTS opportunities (
    id             TEXT PRIMARY KEY,
    tenant_id      TEXT NOT NULL,
    name           TEXT NOT NULL,
    customer       TEXT NOT NULL DEFAULT '',
    use_case       TEXT NOT NULL DEFAULT '',
    value          REAL NOT NULL DEFAULT 0,
    probability    REAL NOT NULL DEFAULT 0,
    stage          TEXT NOT NULL,
    expected_start TEXT,
    weekly_hours   REAL NOT NULL DEFAULT 0,
    source         TEXT NOT NULL DEFAULT 'import',
    external_id    TEXT,
    updated_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_opps_tenant ON opportunities(tenant_id, stage);
CREATE TABLE IF NOT EXISTS flags (
    id            TEXT PRIMARY KEY,
    tenant_id     TEXT NOT NULL,
    deployment_id TEXT NOT NULL REFERENCES deployments(id),
    severity      TEXT NOT NULL,
    text          TEXT NOT NULL,
    rule_key      TEXT,
    raised_by     TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'open',
    handled_by    TEXT,
    handled_at    TEXT,
    note          TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_flags_dep ON flags(tenant_id, deployment_id, status);
CREATE TABLE IF NOT EXISTS approvals (
    id            TEXT PRIMARY KEY,
    tenant_id     TEXT NOT NULL,
    deployment_id TEXT NOT NULL REFERENCES deployments(id),
    agent         TEXT NOT NULL,
    request       TEXT NOT NULL,
    detail        TEXT NOT NULL DEFAULT '',
    requested_by  TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'pending',
    decided_by    TEXT,
    decided_at    TEXT,
    created_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS checklist_items (
    id            TEXT PRIMARY KEY,
    tenant_id     TEXT NOT NULL,
    deployment_id TEXT NOT NULL REFERENCES deployments(id),
    label         TEXT NOT NULL,
    position      INTEGER NOT NULL DEFAULT 0,
    done          INTEGER NOT NULL DEFAULT 0,
    done_by       TEXT,
    done_at       TEXT,
    created_at    TEXT NOT NULL
)
"""),
    (6, "stages that track their own state, on hold, waiting on", """
CREATE TABLE IF NOT EXISTS deployment_stages (
    deployment_id TEXT NOT NULL REFERENCES deployments(id),
    tenant_id     TEXT NOT NULL,
    stage         TEXT NOT NULL,
    state         TEXT NOT NULL DEFAULT 'not_started',
    entered_at    TEXT,
    done_at       TEXT,
    updated_at    TEXT NOT NULL,
    PRIMARY KEY (deployment_id, stage)
);
ALTER TABLE deployments ADD COLUMN hold_since TEXT;
ALTER TABLE deployments ADD COLUMN hold_reason TEXT NOT NULL DEFAULT '';
ALTER TABLE tasks ADD COLUMN waiting_on TEXT;
ALTER TABLE tasks ADD COLUMN blocked_reason TEXT NOT NULL DEFAULT ''
"""),
]

_AUTO = {"sqlite": "INTEGER PRIMARY KEY AUTOINCREMENT", "postgres": "BIGSERIAL PRIMARY KEY"}


class DB:
    """A connection plus its dialect. execute() takes '?' placeholders on both."""

    def __init__(self, raw, dialect: str):
        self.raw = raw
        self.dialect = dialect
        self.lock = threading.RLock()
        self._in_tx = 0

    def _sql(self, sql: str) -> str:
        # Postgres placeholders are %s, so any literal % (e.g. LIKE 'x%') is escaped first.
        return sql.replace("%", "%%").replace("?", "%s") if self.dialect == "postgres" else sql

    def execute(self, sql: str, params: tuple | list = ()):
        with self.lock:
            return self.raw.execute(self._sql(sql), tuple(params))

    def commit(self) -> None:
        if self.dialect == "sqlite":
            self.raw.commit()

    def rollback(self) -> None:
        if self.dialect == "sqlite":
            self.raw.rollback()

    @contextmanager
    def tx(self):
        """One write transaction. The lock also keeps the audit chain strictly ordered."""
        with self.lock:
            if self.dialect == "postgres":
                with self.raw.transaction():
                    yield self
            else:
                try:
                    yield self
                    self.raw.commit()
                except Exception:
                    self.raw.rollback()
                    raise

    def script(self, sql: str) -> None:
        sql = sql.replace("{AUTO}", _AUTO[self.dialect])
        for stmt in (s.strip() for s in sql.split(";")):
            if stmt:
                self.raw.execute(stmt)


def database_url() -> str:
    return os.environ.get("FIELDWORK_DATABASE_URL") or os.environ.get(
        "FIELDWORK_DB", str(Path.cwd() / "fieldwork.db"))


def connect(url: str | None = None) -> DB:
    url = url or database_url()
    if url.startswith(("postgres://", "postgresql://")):
        import psycopg
        from psycopg.rows import dict_row
        raw = psycopg.connect(url, row_factory=dict_row, autocommit=True)
        return DB(raw, "postgres")
    path = url[len("sqlite:///"):] if url.startswith("sqlite:///") else url
    raw = sqlite3.connect(path, check_same_thread=False)
    raw.row_factory = sqlite3.Row
    raw.execute("PRAGMA foreign_keys = ON")
    return DB(raw, "sqlite")


def applied(conn: DB) -> list[int]:
    conn.script("CREATE TABLE IF NOT EXISTS schema_migrations "
                "(version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)")
    conn.commit()
    return [r["version"] for r in conn.execute("SELECT version FROM schema_migrations ORDER BY version")]


def migrate(conn: DB) -> list[int]:
    """Apply pending migrations in order. Returns the versions applied now."""
    done = set(applied(conn))
    ran = []
    for version, name, sql in MIGRATIONS:
        if version in done:
            continue
        with conn.tx():
            conn.script(sql)
            conn.execute("INSERT INTO schema_migrations VALUES (?,?,?)",
                         (version, name, datetime.now(timezone.utc).isoformat()))
        ran.append(version)
    return ran


def init(conn: DB) -> None:
    migrate(conn)


def reset(conn: DB) -> None:
    """Drop everything. Used by `fieldwork seed` and tests; never by the API."""
    tables = ["deployment_stages", "delays", "time_entries", "opportunities", "flags", "approvals", "checklist_items", "outbox", "task_links", "reports", "personal_tokens", "sso_states", "sessions", "tenant_secrets", "engine_credentials", "audit", "findings",
              "tasks", "stage_events", "deployment_members", "deployments", "customers", "users",
              "tenants", "schema_migrations"]
    with conn.lock:
        for t in tables:
            conn.raw.execute(f"DROP TABLE IF EXISTS {t}" + (" CASCADE" if conn.dialect == "postgres" else ""))
        conn.commit()


@contextmanager
def tx(conn: DB):
    with conn.tx():
        yield conn
