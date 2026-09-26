"""Shared fixtures. Every test runs on SQLite, and on Postgres too when
FIELDWORK_TEST_POSTGRES is set to a database URL (the database is wiped).
"""

import os

import pytest
from fastapi.testclient import TestClient

from fieldwork.app import create_app
from fieldwork.seed import DEMO_TOKENS, seed

PG = os.environ.get("FIELDWORK_TEST_POSTGRES")


@pytest.fixture(autouse=True)
def _isolated_key(tmp_path, monkeypatch):
    monkeypatch.setenv("FIELDWORK_KEY_FILE", str(tmp_path / "key"))
    monkeypatch.delenv("FIELDWORK_SECRET_KEYS", raising=False)


@pytest.fixture(params=["sqlite"] + (["postgres"] if PG else []))
def db_url(request, tmp_path):
    return str(tmp_path / "fw.db") if request.param == "sqlite" else PG


@pytest.fixture()
def client(db_url):
    seed(db_url)
    app = create_app(db_url)
    c = TestClient(app)
    c.db_url = db_url
    c.conn = app.state.conn
    yield c
    if app.state.conn.dialect == "postgres":
        app.state.conn.raw.close()


def H(who):
    return {"Authorization": f"Bearer {DEMO_TOKENS[who]}"}
