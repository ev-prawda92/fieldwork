"""Customer-built engines: webhook (we call them) and push (they call us)."""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from fastapi.testclient import TestClient

from fieldwork import plugins
from fieldwork.app import create_app
from fieldwork.seed import DEMO_PUSH_TOKEN, DEMO_TOKENS, READINESS_CHECKLIST, seed


def H(who):
    return {"Authorization": f"Bearer {DEMO_TOKENS[who]}"}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("FIELDWORK_ALLOW_PRIVATE_ENGINES", "1")
    path = str(tmp_path / "fw.db")
    seed(path)
    return TestClient(create_app(path))


@pytest.fixture()
def engine_server():
    """A stand-in customer engine that records what it received."""
    seen = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            seen["body"], seen["headers"] = body, dict(self.headers)
            out = json.dumps({"summary": "3 of 4 integrations healthy", "status": "warn",
                              "result": {"unhealthy": ["sftp-drop"]}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(out)

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}/", seen
    srv.shutdown()


def test_register_and_run_webhook_engine(client, engine_server):
    url, seen = engine_server
    r = client.post("/api/engines", headers=H("head"), json={
        "key": "integration_health", "kind": "webhook", "name": "Integration health", "url": url})
    assert r.status_code == 201
    secret = r.json()["signing_secret"]
    run = client.post("/api/deployments/dep_northfield/engines/custom/integration_health",
                      headers=H("fde"), json={"input": "check all"})
    assert run.status_code == 200 and run.json()["result"]["status"] == "warn"
    # The call was signed with the engine's secret and carried the deployment.
    h = {k.lower(): v for k, v in seen["headers"].items()}
    assert plugins.verify_signature(secret, h["x-fieldwork-timestamp"], seen["body"],
                                    h["x-fieldwork-signature"])
    assert json.loads(seen["body"])["deployment"]["id"] == "dep_northfield"
    # Result is a finding like any other, confirmable by someone else.
    f = client.get("/api/deployments/dep_northfield/findings", headers=H("em")).json()[0]
    assert f["engine"] == "integration_health"
    assert client.post(f"/api/findings/{f['id']}/confirm", headers=H("em")).status_code == 200


def test_only_engine_managers_register(client):
    r = client.post("/api/engines", headers=H("em"), json={
        "key": "x", "kind": "push", "name": "X"})
    assert r.status_code == 403


def test_engine_run_respects_scope(client, engine_server):
    url, _ = engine_server
    client.post("/api/engines", headers=H("head"), json={
        "key": "ih", "kind": "webhook", "name": "IH", "url": url})
    # Jordan (AI engineer) isn't staffed on Northfield.
    r = client.post("/api/deployments/dep_northfield/engines/custom/ih", headers=H("ai"), json={})
    assert r.status_code == 404


def test_ssrf_guard_blocks_private_hosts_in_saas_mode(monkeypatch):
    monkeypatch.delenv("FIELDWORK_ALLOW_PRIVATE_ENGINES", raising=False)
    with pytest.raises(plugins.PluginError, match="private"):
        plugins.call_webhook("https://127.0.0.1/", "s", {}, allow_private=False)
    with pytest.raises(plugins.PluginError, match="private"):
        plugins.call_webhook("https://169.254.169.254/latest", "s", {}, allow_private=False)


def test_saas_mode_requires_https_engines(tmp_path, monkeypatch):
    monkeypatch.delenv("FIELDWORK_ALLOW_PRIVATE_ENGINES", raising=False)
    path = str(tmp_path / "fw.db")
    seed(path)
    c = TestClient(create_app(path))
    r = c.post("/api/engines", headers=H("head"), json={
        "key": "plain", "kind": "webhook", "name": "Plain", "url": "http://example.com/"})
    assert r.status_code == 422


def test_push_engine_ingest(client):
    ok = client.post("/api/ingest/findings", headers={"Authorization": f"Bearer {DEMO_PUSH_TOKEN}"},
                     json={"deployment_id": "dep_castellan", "title": "Latency", "summary": "p95 ok",
                           "status": "pass", "result": {"p95_ms": 900}})
    assert ok.status_code == 201
    audit = client.get("/api/audit", headers=H("head")).json()[0]
    assert audit["action"] == "engine.run" and audit["actor"] == "engine:latency_probe"
    # A user token can't push; an engine token can't reach another tenant.
    assert client.post("/api/ingest/findings", headers=H("head"), json={
        "deployment_id": "dep_castellan", "title": "x", "summary": "x"}).status_code == 401
    assert client.post("/api/ingest/findings", headers={"Authorization": f"Bearer {DEMO_PUSH_TOKEN}"},
                       json={"deployment_id": "dep_orb1", "title": "x", "summary": "x"}).status_code == 404
    bad = client.post("/api/ingest/findings", headers={"Authorization": f"Bearer {DEMO_PUSH_TOKEN}"},
                      json={"deployment_id": "dep_castellan", "title": "x", "summary": "x", "status": "great"})
    assert bad.status_code == 502


def test_rotating_a_push_token_revokes_the_old_one(client):
    new = client.post("/api/engines/latency_probe/rotate", headers=H("head")).json()["engine_token"]
    body = {"deployment_id": "dep_castellan", "title": "x", "summary": "x"}
    assert client.post("/api/ingest/findings", headers={"Authorization": f"Bearer {DEMO_PUSH_TOKEN}"},
                       json=body).status_code == 401
    assert client.post("/api/ingest/findings", headers={"Authorization": f"Bearer {new}"},
                       json=body).status_code == 201


def test_example_readiness_engine_scores_checklist():
    import importlib.util
    spec = importlib.util.spec_from_file_location("re", "examples/engines/readiness_engine.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    out = mod.score(READINESS_CHECKLIST)
    assert out["status"] == "fail"
    assert out["result"]["open_blockers"] == ["Security review of model gateway closed (blocker)"]
