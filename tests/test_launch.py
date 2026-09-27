"""Launch features: Today, reports, notifications, tracker sync, import, tokens, MCP, gate, metrics."""

import hashlib
import hmac
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from fastapi.testclient import TestClient

from fieldwork import events, trackers
from fieldwork.app import create_app
from fieldwork.seed import SAMPLE_INPUTS, seed

from .conftest import H

SAM = {"Authorization": "Bearer demo-usr_sam-meridian"}


class Fake:
    """A stand-in HTTP service. handler(method, path, headers, body) -> (status, json)."""

    def __init__(self, handler):
        self.calls = []
        fake = self

        class Hd(BaseHTTPRequestHandler):
            def _do(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n) if n else b""
                fake.calls.append((self.command, self.path, dict(self.headers), body))
                code, out = handler(self.command, self.path, dict(self.headers), body)
                raw = json.dumps(out).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(raw)
            do_GET = do_POST = do_PATCH = do_PUT = _do

            def log_message(self, *a):
                pass

        self.srv = HTTPServer(("127.0.0.1", 0), Hd)
        self.url = f"http://127.0.0.1:{self.srv.server_port}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()


@pytest.fixture(autouse=True)
def _local_ok(monkeypatch):
    monkeypatch.setenv("FIELDWORK_ALLOW_PRIVATE_ENGINES", "1")
    monkeypatch.setenv("FIELDWORK_PUBLIC_URL", "https://fw.example")


def cfg(client):
    return client.get("/api/config", headers=H("head")).json()["config"]


def put_cfg(client, c):
    r = client.put("/api/config", headers=H("head"), json=c)
    assert r.status_code == 200, r.text


# ------------------------------------------------------------------- today

def test_today_shows_what_needs_you(client):
    t = client.get("/api/today", headers=H("head")).json()
    assert "fnd_probe1" in {f["id"] for f in t["confirm"]}
    assert {a["id"] for a in t["attention"]} == {"dep_northfield", "dep_castellan"}
    sam = client.get("/api/today", headers=SAM).json()
    assert [x["id"] for x in sam["blocked"]] == ["tsk_003"]
    assert client.get("/api/today", headers=H("customer")).json()["confirm"] == []


# ----------------------------------------------------------------- reports

def test_internal_and_customer_reports(client):
    r = client.post("/api/deployments/dep_northfield/reports", headers=H("em"), json={"audience": "internal"})
    internal = r.json()["body_md"]
    assert "Confirm 3-way-match tolerance config" in internal and "## Blocked" in internal
    r = client.post("/api/deployments/dep_northfield/reports", headers=H("em"), json={"audience": "customer"})
    rid, customer = r.json()["id"], r.json()["body_md"]
    assert "Confirm 3-way-match" not in customer                     # internal task stays internal
    assert "Send September AP exception export" in customer and "Needed from you" in customer
    assert "Value readout" in customer                               # the shared, confirmed finding
    assert client.get("/api/deployments/dep_northfield/reports", headers=H("customer")).json() == []
    assert client.post(f"/api/reports/{rid}/share", headers=H("em"), json={"shared": True}).status_code == 200
    assert [x["id"] for x in client.get("/api/deployments/dep_northfield/reports", headers=H("customer")).json()] == [rid]
    irid = client.get("/api/deployments/dep_northfield/reports", headers=H("em")).json()[-1]["id"]
    assert client.post(f"/api/reports/{irid}/share", headers=H("em"), json={"shared": True}).status_code == 409
    assert client.post("/api/deployments/dep_northfield/reports", headers=H("customer"),
                       json={"audience": "customer"}).status_code == 403


# ------------------------------------------------------------ notifications

def test_slack_and_signed_webhooks(client):
    slack = Fake(lambda *a: (200, {}))
    hook = Fake(lambda *a: (200, {}))
    try:
        c = cfg(client)
        c["integrations"]["slack"]["enabled"] = True
        c["integrations"]["webhooks"] = [{"id": "ops", "url": hook.url + "/in", "events": ["task.blocked"]}]
        put_cfg(client, c)
        assert client.put("/api/integrations/secret", headers=H("head"),
                          json={"name": "slack_webhook_url", "value": slack.url + "/hook"}).status_code == 200
        secret = client.post("/api/integrations/ops/generate-secret", headers=H("head")).json()["secret"]
        assert client.patch("/api/tasks/tsk_001", headers=H("fde"), json={"status": "blocked"}).status_code == 200
        assert events.process(client.conn) == {"sent": 2, "failed": 0}
        text = json.loads(slack.calls[0][3])["text"]
        assert "Blocked" in text and "Run build-vs-training" in text and "https://fw.example/#dep=dep_northfield" in text
        _, _, h, body = hook.calls[0]
        h = {k.lower(): v for k, v in h.items()}
        expect = "sha256=" + hmac.new(secret.encode(), h["x-fieldwork-timestamp"].encode() + b"." + body,
                                      hashlib.sha256).hexdigest()
        assert h["x-fieldwork-signature"] == expect and json.loads(body)["event"] == "task.blocked"
    finally:
        slack.close(); hook.close()


def test_failed_delivery_is_retried_later(client):
    c = cfg(client)
    c["integrations"]["webhooks"] = [{"id": "down", "url": "http://127.0.0.1:9/nothing", "events": ["task.done"]}]
    put_cfg(client, c)
    client.patch("/api/tasks/tsk_001", headers=H("fde"), json={"status": "done"})
    assert events.process(client.conn)["failed"] == 1
    row = client.conn.execute("SELECT * FROM outbox WHERE kind='webhook'").fetchone()
    assert row["status"] == "pending" and row["attempts"] == 1 and row["last_error"]
    assert events.process(client.conn) == {"sent": 0, "failed": 0}  # backoff: not due yet


def test_digest_lists_what_matters(client):
    text = events.digest_text(client.conn, "ten_meridian", cfg(client))
    assert "Claims triage copilot" in text and "Close security review of model gateway" in text
    assert "Carrier dispute pilot" not in text  # on track, nothing waiting


def test_slack_url_must_be_slack(client, monkeypatch):
    monkeypatch.delenv("FIELDWORK_ALLOW_PRIVATE_ENGINES")
    r = client.put("/api/integrations/secret", headers=H("head"),
                   json={"name": "slack_webhook_url", "value": "https://evil.example/x"})
    assert r.status_code == 422
    assert client.get("/api/integrations", headers=H("em")).status_code == 403


# -------------------------------------------------------------- tracker sync

def github_fake():
    state = {"n": 0}

    def h(method, path, headers, body):
        if method == "POST" and path.endswith("/issues"):
            state["n"] += 1
            return 201, {"number": state["n"], "html_url": f"https://github.com/o/r/issues/{state['n']}"}
        return 200, {}
    return Fake(h)


def test_github_two_way_sync(client):
    gh = github_fake()
    try:
        c = cfg(client)
        c["integrations"]["github"] = {"enabled": True, "api_base": gh.url}
        put_cfg(client, c)
        client.put("/api/integrations/secret", headers=H("head"), json={"name": "github_token", "value": "ghp_x"})
        wh = client.post("/api/integrations/github/generate-secret", headers=H("head")).json()["secret"]
        assert client.put("/api/deployments/dep_northfield/sync", headers=H("em"),
                          json={"provider": "github", "target": "o/r"}).status_code == 200
        events.process(client.conn)
        links = client.get("/api/deployments/dep_northfield/links", headers=H("fde")).json()["links"]
        assert len(links) == 5 and all(l["url"].startswith("https://github.com/o/r/issues/") for l in links.values())
        assert gh.calls[0][2]["Authorization"] == "Bearer ghp_x"
        # Fieldwork -> GitHub
        client.patch("/api/tasks/tsk_001", headers=H("fde"), json={"status": "done"})
        events.process(client.conn)
        patch = [c for c in gh.calls if c[0] == "PATCH"][-1]
        assert json.loads(patch[3])["state"] == "closed"
        # GitHub -> Fieldwork, signed, and not echoed back
        num = links["tsk_002"]["external_id"]
        payload = json.dumps({"action": "labeled", "issue": {"number": int(num), "state": "open", "title": "Walk Harrisburg AP lead",
                                                             "labels": [{"name": "fieldwork:blocked"}]}}).encode()
        sig = "sha256=" + hmac.new(wh.encode(), payload, hashlib.sha256).hexdigest()
        bad = client.post("/integrations/meridian/github/webhook", content=payload,
                          headers={"X-GitHub-Event": "issues", "X-Hub-Signature-256": "sha256=00"})
        assert bad.status_code == 401
        before = client.conn.execute("SELECT COUNT(*) n FROM outbox WHERE kind='tracker'").fetchone()["n"]
        ok = client.post("/integrations/meridian/github/webhook", content=payload,
                         headers={"X-GitHub-Event": "issues", "X-Hub-Signature-256": sig})
        assert ok.json()["updated"] == ["tsk_002"]
        t = next(x for x in client.get("/api/tasks?deployment_id=dep_northfield", headers=H("em")).json() if x["id"] == "tsk_002")
        assert t["status"] == "blocked" and t["title"] == "Walk Harrisburg AP lead"
        after = client.conn.execute("SELECT COUNT(*) n FROM outbox WHERE kind='tracker'").fetchone()["n"]
        assert after == before
        assert client.get("/api/audit", headers=H("head")).json()[0]["actor"] == "tracker:github"
    finally:
        gh.close()


def test_linear_create_and_inbound(client):
    def h(method, path, headers, body):
        q = json.loads(body)["query"]
        if "states" in q:
            return 200, {"data": {"team": {"states": {"nodes": [
                {"id": "s-todo", "type": "unstarted", "position": 1}, {"id": "s-doing", "type": "started", "position": 2},
                {"id": "s-done", "type": "completed", "position": 3}]}}}}
        if "issueCreate" in q:
            return 200, {"data": {"issueCreate": {"success": True, "issue": {"id": "lin-1", "url": "https://linear.app/x/LIN-1"}}}}
        return 200, {"data": {"issueUpdate": {"success": True}}}
    lin = Fake(h)
    try:
        c = cfg(client)
        c["integrations"]["linear"] = {"enabled": True, "api_base": lin.url}
        put_cfg(client, c)
        client.put("/api/integrations/secret", headers=H("head"), json={"name": "linear_api_key", "value": "lin_api_x"})
        client.put("/api/deployments/dep_harborview/sync", headers=H("em"), json={"provider": "linear", "target": "team-1"})
        events.process(client.conn)
        create = next(json.loads(c[3]) for c in lin.calls if "issueCreate" in json.loads(c[3])["query"])
        assert create["variables"]["i"]["teamId"] == "team-1" and create["variables"]["i"]["stateId"] in ("s-todo", "s-doing")
        wh = client.post("/api/integrations/linear/generate-secret", headers=H("head")).json()["secret"]
        body = json.dumps({"type": "Issue", "action": "update",
                           "data": {"id": "lin-1", "title": "Scope FHIR read permissions for intake agent",
                                    "state": {"type": "completed"}}}).encode()
        r = client.post("/integrations/meridian/linear/webhook", content=body, headers={
            "Linear-Signature": hmac.new(wh.encode(), body, hashlib.sha256).hexdigest()})
        assert len(r.json()["updated"]) == 1
    finally:
        lin.close()


def test_jira_create_transitions_and_inbound(client):
    seq = {"n": 0}

    def h(method, path, headers, body):
        if method == "POST" and path == "/rest/api/3/issue":
            seq["n"] += 1
            return 201, {"key": f"FW-{seq['n']}"}
        if path.endswith("/transitions") and method == "GET":
            return 200, {"transitions": [{"id": "11", "to": {"statusCategory": {"key": "new"}}},
                                         {"id": "21", "to": {"statusCategory": {"key": "indeterminate"}}},
                                         {"id": "31", "to": {"statusCategory": {"key": "done"}}}]}
        return 204, {}
    jira = Fake(h)
    try:
        c = cfg(client)
        c["integrations"]["jira"] = {"enabled": True, "base_url": jira.url, "email": "ops@meridian.example"}
        put_cfg(client, c)
        client.put("/api/integrations/secret", headers=H("head"), json={"name": "jira_api_token", "value": "atl"})
        r = client.post("/api/tasks", headers=H("head"), json={"deployment_id": "dep_redline", "title": "Pull rate tables"})
        client.put("/api/deployments/dep_redline/sync", headers=H("head"), json={"provider": "jira", "target": "FW"})
        events.process(client.conn)
        assert any(c[1] == "/rest/api/3/issue" for c in jira.calls)
        assert jira.calls[0][2]["Authorization"].startswith("Basic ")
        client.patch(f"/api/tasks/{r.json()['id']}", headers=H("head"), json={"status": "in_progress"})
        events.process(client.conn)
        moves = [json.loads(c[3]) for c in jira.calls if c[0] == "POST" and c[1].endswith("/transitions")]
        assert {"transition": {"id": "21"}} in moves
        wh = client.post("/api/integrations/jira/generate-secret", headers=H("head")).json()["secret"]
        links = client.get("/api/deployments/dep_redline/links", headers=H("head")).json()["links"]
        key = links[r.json()["id"]]["external_id"]
        body = json.dumps({"webhookEvent": "jira:issue_updated", "issue": {"key": key, "fields": {
            "summary": "Pull rate tables", "status": {"statusCategory": {"key": "done"}}}}}).encode()
        out = client.post("/integrations/meridian/jira/webhook", content=body, headers={
            "X-Hub-Signature": "sha256=" + hmac.new(wh.encode(), body, hashlib.sha256).hexdigest()})
        assert out.json()["updated"] == [r.json()["id"]]
    finally:
        jira.close()


def test_sync_needs_connected_tracker(client):
    r = client.put("/api/deployments/dep_northfield/sync", headers=H("em"), json={"provider": "github", "target": "o/r"})
    assert r.status_code == 409
    assert client.put("/api/deployments/dep_northfield/sync", headers=H("fde"),
                      json={"provider": None}).status_code == 403


# ------------------------------------------------------------------ import

def test_import_deployments_and_tasks(client):
    dcsv = ("customer,deployment,stage,health,lead_email,tier\n"
            "Acme Foods,Order-to-cash agent,Integrate,at risk,marcus@meridian.example,Strategic\n"
            "Northfield Supply Co.,Returns agent,,,,\n"
            "Bad Co,,discover,,,\n"
            "Zed,Thing,nope,,,\n")
    r = client.post("/api/import", headers=H("head"), json={"kind": "deployments", "csv": dcsv}).json()
    assert r["created"] == 2 and len(r["errors"]) == 2
    deps = {d["name"]: d for d in client.get("/api/deployments", headers=H("head")).json()}
    assert deps["Order-to-cash agent"]["stage"] == "integrate" and deps["Order-to-cash agent"]["fields"]["tier"] == "Strategic"
    assert deps["Returns agent"]["customer_id"] == "cus_northfield"
    tcsv = ("deployment,title,assignee_email,status,due,share\n"
            "Order-to-cash agent,Map invoice states,maya@meridian.example,in progress,2026-10-09,\n"
            "AP exception agent,Customer sends vendor master,ruth@meridian.example,open,2026-10-03,\n"
            "Nope,x,,,,\n")
    r = client.post("/api/import", headers=H("head"), json={"kind": "tasks", "csv": tcsv}).json()
    assert r["created"] == 2 and len(r["errors"]) == 1
    ruth = client.get("/api/tasks?deployment_id=dep_northfield", headers=H("customer")).json()
    assert "Customer sends vendor master" in {t["title"] for t in ruth}  # auto-shared for the customer
    assert client.post("/api/import", headers=H("fde"), json={"kind": "deployments", "csv": dcsv}).status_code == 403


# --------------------------------------------------------- personal tokens

def test_personal_tokens(client):
    r = client.post("/api/me/tokens", headers=H("fde"), json={"name": "Cursor"}).json()
    pat = {"Authorization": f"Bearer {r['token']}"}
    assert client.get("/api/me", headers=pat).json()["user"]["name"] == "Maya Chen"
    assert client.post("/api/me/tokens", headers=pat, json={"name": "x"}).status_code == 403
    assert [t["name"] for t in client.get("/api/me/tokens", headers=H("fde")).json()] == ["Cursor"]
    assert client.delete(f"/api/me/tokens/{r['id']}", headers=H("em")).status_code == 404  # not yours
    assert client.delete(f"/api/me/tokens/{r['id']}", headers=H("fde")).status_code == 200
    assert client.get("/api/me", headers=pat).status_code == 401


# --------------------------------------------------------------------- MCP

def rpc(client, auth, method, params=None, id_=1):
    return client.post("/mcp", headers=auth, json={"jsonrpc": "2.0", "id": id_, "method": method, "params": params or {}})


def test_mcp_end_to_end(client):
    pat = {"Authorization": "Bearer " + client.post("/api/me/tokens", headers=H("fde"), json={"name": "Claude"}).json()["token"]}
    init = rpc(client, pat, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t"}}).json()
    assert init["result"]["protocolVersion"] == "2025-06-18" and "Maya Chen" in init["result"]["instructions"]
    assert client.post("/mcp", headers=pat, json={"jsonrpc": "2.0", "method": "notifications/initialized"}).status_code == 202
    names = {t["name"] for t in rpc(client, pat, "tools/list").json()["result"]["tools"]}
    assert {"today", "create_task", "run_engine", "draft_status_report"} <= names
    out = rpc(client, pat, "tools/call", {"name": "create_task", "arguments": {
        "deployment_id": "dep_northfield", "title": "Draft cutover plan"}}).json()["result"]
    assert not out["isError"] and "tsk_" in out["content"][0]["text"]
    assert client.get("/api/audit", headers=H("head")).json()[0]["actor"] == "Maya Chen"
    run = rpc(client, pat, "tools/call", {"name": "run_engine", "arguments": {
        "deployment_id": "dep_castellan", "engine": "conformance", "input": SAMPLE_INPUTS["conformance"]}}).json()["result"]
    assert not run["isError"] and "critical" in run["content"][0]["text"]
    # Her permissions, not more: FDEs can't assign work to others or see Harborview.
    jump = rpc(client, pat, "tools/call", {"name": "create_task", "arguments": {
        "deployment_id": "dep_northfield", "title": "x", "assignee_id": "usr_sam"}}).json()["result"]
    assert jump["isError"] and "can't" in jump["content"][0]["text"]
    hidden = rpc(client, pat, "tools/call", {"name": "get_deployment", "arguments": {"deployment_id": "dep_harborview"}}).json()["result"]
    assert hidden["isError"]
    report = rpc(client, pat, "tools/call", {"name": "draft_status_report", "arguments": {"deployment_id": "dep_northfield"}}).json()["result"]
    assert report["content"][0]["text"].startswith("# AP exception agent")
    assert rpc(client, {"Authorization": "Bearer nope"}, "initialize").status_code == 401
    assert client.post("/mcp", json={}).status_code == 401
    bad = rpc(client, pat, "tools/call", {"name": "create_task", "arguments": {"deployment_id": "dep_northfield"}}).json()
    assert bad["error"]["code"] == -32602


# --------------------------------------------------------- gate and metrics

def test_access_gate(db_url, monkeypatch):
    monkeypatch.setenv("FIELDWORK_ACCESS_PASSWORD", "open sesame")
    monkeypatch.setenv("FIELDWORK_PUBLIC_URL", "http://testserver")  # https would mark the cookie Secure
    monkeypatch.setenv("FIELDWORK_DEMO", "1")
    seed(db_url)
    c = TestClient(create_app(db_url))
    assert c.get("/", follow_redirects=False).headers["location"] == "/gate"
    assert c.get("/api/demo").status_code == 401                       # demo tokens hidden behind the gate
    assert c.get("/api/me", headers=H("fde")).status_code == 200       # API clients unaffected
    assert c.post("/gate", content="password=wrong", headers={"Content-Type": "application/x-www-form-urlencoded"},
                  follow_redirects=False).headers["location"] == "/gate?error=1"
    ok = c.post("/gate", content="password=open+sesame", headers={"Content-Type": "application/x-www-form-urlencoded"},
                follow_redirects=False)
    assert ok.status_code == 303 and "fw_gate" in ok.cookies
    assert c.get("/api/demo").status_code == 200


def test_operator_metrics(client, monkeypatch):
    assert client.get("/api/operator/metrics").status_code == 404
    monkeypatch.setenv("FIELDWORK_OPERATOR_TOKEN", "op-secret")
    client.post("/api/deployments/dep_castellan/engines/stage/golive", headers=H("fde"), json={"input": SAMPLE_INPUTS["golive"]})
    m = client.get("/api/operator/metrics", headers={"X-Operator-Token": "op-secret"}).json()
    mer = next(w for w in m["by_workspace"] if w["workspace"] == "Meridian AI")
    assert mer["engine_runs_7d"] >= 1 and mer["active_people_7d"] >= 1 and mer["custom_engines"] == 2


def test_hosted_demo_serves_its_own_readiness_engine(client, monkeypatch):
    import time
    from fieldwork import plugins
    from fieldwork.seed import DEMO_ENGINE_SECRET, READINESS_CHECKLIST
    body = json.dumps({"input": READINESS_CHECKLIST}).encode()
    ts = str(int(time.time()))
    sig = plugins.sign(DEMO_ENGINE_SECRET, ts, body)
    assert client.post("/demo-engines/readiness", content=body).status_code == 404   # demo off
    monkeypatch.setenv("FIELDWORK_DEMO", "1")
    assert client.post("/demo-engines/readiness", content=body,
                       headers={"X-Fieldwork-Timestamp": ts, "X-Fieldwork-Signature": "sha256=bad"}).status_code == 401
    r = client.post("/demo-engines/readiness", content=body,
                    headers={"X-Fieldwork-Timestamp": ts, "X-Fieldwork-Signature": sig})
    assert r.json()["status"] == "fail"
