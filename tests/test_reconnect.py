"""Serverless Postgres (Neon) drops idle connections; Fieldwork reconnects instead of failing."""

import os

import pytest

from fieldwork import db

PG = os.environ.get("FIELDWORK_TEST_POSTGRES")


@pytest.mark.skipif(not PG, reason="needs FIELDWORK_TEST_POSTGRES")
def test_dropped_connection_is_replaced():
    import psycopg
    c = db.connect(PG)
    killer = psycopg.connect(PG, autocommit=True)
    killer.execute("SELECT pg_terminate_backend(%s)", (c.execute("SELECT pg_backend_pid() p").fetchone()["p"],))
    assert c.execute("SELECT 1 x").fetchone()["x"] == 1
    killer.execute("SELECT pg_terminate_backend(%s)", (c.execute("SELECT pg_backend_pid() p").fetchone()["p"],))
    with c.tx():
        assert c.execute("SELECT 2 x").fetchone()["x"] == 2
    killer.close()
    c.raw.close()
