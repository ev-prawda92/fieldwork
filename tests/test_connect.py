"""Live connections: installs, the inbound event store, Slack as an app, tracker installs,
CRM, timesheets, calendars, the email signal, health, replay and the live stream.

No test touches the network: connect.http.transport is replaced by FakeWeb,
which answers like each vendor's documented API.
"""

import base64
import hashlib
import hmac
import json
import re
import threading
import time
from datetime import date, datetime, timedelta, timezone
from urllib.parse import parse_qs, urlencode, urlparse

import pytest

from fieldwork import events, ops
from fieldwork.connect import core, http

from .conftest import H

SAM = {"Authorization": "Bearer demo-usr_sam-meridian"}
SIGNING = "slack-signing-secret"


class FakeWeb:
    """Stand-in for every vendor. route(method, url regex, handler); handler(req) -> (status, body[, headers])."""

    def __init__(self):
        self.routes, self.calls = [], []

    def route(self, method, pattern, handler):
        self.routes.insert(0, (method, re.compile(pattern), handler))
        return self

    def json(self, method, pattern, body, status=200, headers=None):
        return self.route(method, pattern, lambda req: (status, body, headers or {}))

    def __call__(self, method, url, headers, body, timeout):
        req = {"method": method, "url": url, "headers": headers, "body": body,
               "query": {k: v if len(v) > 1 else v[0] for k, v in parse_qs(urlparse(url).query).items()},
               "path": urlparse(url).path}
        try:
            req["json"] = json.loads(body) if body else None
        except ValueError:
            req["form"] = {k: v[0] for k, v in parse_qs(body.decode()).items()}
        self.calls.append(req)
        for m, rx, h in self.routes:
            if m == method and rx.search(url.split("?")[0]):
                out = h(req)
                status, payload = out[0], out[1]
                hdrs = out[2] if len(out) > 2 else {}
                raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
                return http.Response(status, raw, {k.lower(): v for k, v in hdrs.items()})
        return http.Response(404, json.dumps({"error": f"no fake for {method} {url}"}).encode(), {})

    def called(self, method, pattern):
        return [c for c in self.calls if c["method"] == method and re.search(pattern, c["url"])]


@pytest.fixture()
def web(monkeypatch):
    from fieldwork.connect import slack as slack_app
    w = FakeWeb()
    monkeypatch.setattr(http, "transport", w)
    monkeypatch.setattr(slack_app, "run_later", lambda fn, *a: fn(*a))  # Slack's deferred work, inline
    return w


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("FIELDWORK_PUBLIC_URL", "https://fw.example")
    for p in ("SLACK", "GITHUB", "LINEAR", "JIRA", "SALESFORCE", "HUBSPOT", "HARVEST", "GOOGLE", "MICROSOFT"):
        monkeypatch.setenv(f"FIELDWORK_{p}_CLIENT_ID", f"{p.lower()}-id")
        monkeypatch.setenv(f"FIELDWORK_{p}_CLIENT_SECRET", f"{p.lower()}-secret")
    monkeypatch.setenv("FIELDWORK_SLACK_SIGNING_SECRET", SIGNING)
    monkeypatch.setenv("FIELDWORK_LINEAR_WEBHOOK_SECRET", "linear-hook-secret")


def install(client, key, who="head", code="code-1", headers=None):
    """Run the one-click install: start, then come back to the callback like the browser would."""
    r = client.post(f"/api/connections/{key}/start", headers=headers or H(who))
    assert r.status_code == 200, r.text
    q = parse_qs(urlparse(r.json()["url"]).query)
    state = q["state"][0]
    cookie = r.headers["set-cookie"].split(";")[0]  # the browser keeps it (Secure: https in real life)
    back = client.get(f"/oauth/callback?state={state}&code={code}", headers={"Cookie": cookie},
                      follow_redirects=False)
    assert back.status_code == 303, back.text
    assert "connected=" in back.headers["location"], back.headers["location"]
    return q


def the_cx(client, key, user_id=None):
    return core.active(client.conn, "ten_meridian", key, user_id)


# ================================================================ core flows

def test_catalog_shows_what_each_person_can_connect(client, monkeypatch):
    monkeypatch.delenv("FIELDWORK_SALESFORCE_CLIENT_ID")
    head = client.get("/api/connections", headers=H("head")).json()
    keys = {p["key"]: p for p in head["providers"]}
    assert {"slack", "github", "linear", "jira", "salesforce", "hubspot", "harvest", "toggl",
            "google_calendar", "outlook_calendar", "gmail", "outlook_mail"} <= set(keys)
    assert keys["salesforce"]["configured"] is False
    assert "FIELDWORK_SALESFORCE_CLIENT_ID" in keys["salesforce"]["env_needed"]
    assert keys["toggl"]["configured"] is True  # token-based, nothing to register
    assert head["redirect_uri"] == "https://fw.example/oauth/callback"
    fde = client.get("/api/connections", headers=H("fde")).json()
    assert {p["key"] for p in fde["providers"]} == {"google_calendar", "outlook_calendar", "gmail", "outlook_mail"}
    assert all(p["env_needed"] == [] for p in fde["providers"])
    assert client.get("/api/connections", headers=H("customer")).json()["providers"] == []


def test_start_needs_app_setup_and_permission(client, monkeypatch):
    monkeypatch.delenv("FIELDWORK_HUBSPOT_CLIENT_SECRET")
    r = client.post("/api/connections/hubspot/start", headers=H("head"))
    assert r.status_code == 409 and "FIELDWORK_HUBSPOT_CLIENT_SECRET" in r.json()["detail"]
    assert client.post("/api/connections/slack/start", headers=H("fde")).status_code == 403
    assert client.post("/api/connections/gmail/start", headers=H("customer")).status_code == 403
    assert client.post("/api/connections/nope/start", headers=H("head")).status_code == 404
    r = client.post("/api/connections/salesforce/start", headers=H("head"))
    q = parse_qs(urlparse(r.json()["url"]).query)
    assert q["redirect_uri"] == ["https://fw.example/oauth/callback"] and q["code_challenge_method"] == ["S256"]
    assert "fw_oauth" in r.headers["set-cookie"] and "HttpOnly" in r.headers["set-cookie"]


def slack_fakes(web, team="T1"):
    web.json("POST", r"slack\.com/api/oauth\.v2\.access", {
        "ok": True, "access_token": "xoxb-1", "bot_user_id": "UBOT", "app_id": "A1",
        "team": {"id": team, "name": "Meridian"},
        "incoming_webhook": {"channel": "#delivery", "channel_id": "C1", "url": "https://hooks.slack.com/services/x"}})
    web.json("POST", r"slack\.com/api/chat\.postMessage", {"ok": True, "ts": "1.1"})
    emails = {"UMARCUS": "marcus@meridian.example", "UDANA": "dana@meridian.example",
              "USAM": "sam@meridian.example", "URUTH": "ruth@northfieldsupply.example",
              "USTRANGER": "who@else.example", "UMAYA": "maya@meridian.example"}
    web.route("GET", r"slack\.com/api/users\.info", lambda r: (200, {
        "ok": True, "user": {"id": r["query"]["user"], "profile": {"email": emails.get(r["query"]["user"])}}}))
    by_email = {v: k for k, v in emails.items()}
    web.route("GET", r"slack\.com/api/users\.lookupByEmail", lambda r: (
        (200, {"ok": True, "user": {"id": by_email[r["query"]["email"]]}}) if r["query"]["email"] in by_email
        else (200, {"ok": False, "error": "users_not_found"})))
    web.json("POST", r"hooks\.slack\.com/(actions|commands)", {"ok": True})


def test_oauth_callback_creates_encrypted_connection(client, web):
    slack_fakes(web)
    install(client, "slack")
    cx = the_cx(client, "slack")
    assert cx and cx["account_name"] == "Meridian" and cx["external_account_id"] == "T1"
    raw = client.conn.execute("SELECT tokens FROM connections WHERE id=?", (cx.id,)).fetchone()["tokens"]
    assert "xoxb-1" not in raw and cx.tokens["access_token"] == "xoxb-1"
    assert cx.settings["channel_id"] == "C1"
    cfg = client.get("/api/config", headers=H("head")).json()["config"]
    assert cfg["integrations"]["slack"]["enabled"] is True
    trail = client.get("/api/audit", headers=H("head")).json()
    assert any(a["action"] == "connection.create" and a["subject"] == cx.id for a in trail)
    # the exchange sent the code and our redirect back
    ex = web.called("POST", "oauth.v2.access")[0]
    assert ex["form"]["code"] == "code-1" and ex["form"]["redirect_uri"] == "https://fw.example/oauth/callback"


def test_callback_rejects_replay_and_other_browsers(client, web):
    slack_fakes(web)
    r = client.post("/api/connections/slack/start", headers=H("head"))
    state = parse_qs(urlparse(r.json()["url"]).query)["state"][0]
    # someone else's browser (no cookie) can't finish a flow this person started
    client.cookies.clear()
    back = client.get(f"/oauth/callback?state={state}&code=x", follow_redirects=False)
    assert "oauth_error" in back.headers["location"] and not the_cx(client, "slack")
    # and a state is single-use
    back = client.get(f"/oauth/callback?state={state}&code=x", follow_redirects=False)
    assert "expired" in back.headers["location"]
    back = client.get("/oauth/callback?state=forged&code=x", follow_redirects=False)
    assert "oauth_error" in back.headers["location"]


# ===================================================================== Slack

def slack_sign(body: bytes, ts=None, secret=SIGNING):
    ts = str(int(ts or time.time()))
    sig = "v0=" + hmac.new(secret.encode(), f"v0:{ts}:".encode() + body, hashlib.sha256).hexdigest()
    return {"X-Slack-Request-Timestamp": ts, "X-Slack-Signature": sig,
            "Content-Type": "application/x-www-form-urlencoded"}


def press(client, action_id, value, user, team="T1", selected=False):
    act = {"action_id": "fw:" + action_id, "action_ts": str(time.time())}
    if selected:
        act["selected_option"] = {"value": value}
    else:
        act["value"] = value
    payload = {"type": "block_actions", "team": {"id": team}, "user": {"id": user}, "actions": [act],
               "response_url": "https://hooks.slack.com/actions/T1/1/x",
               "message": {"blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": "hi"}},
                                      {"type": "actions", "elements": []}]}}
    body = urlencode({"payload": json.dumps(payload)}).encode()
    return client.post("/hooks/slack/interact", content=body, headers=slack_sign(body))


def test_slack_notifications_carry_the_decision_buttons(client, web):
    slack_fakes(web)
    install(client, "slack")
    cfg = client.get("/api/config", headers=H("head")).json()["config"]
    cfg["integrations"]["slack"]["events"] = ["flag.raised", "approval.requested", "task.blocked", "delay.opened"]
    assert client.put("/api/config", headers=H("head"), json=cfg).status_code == 200
    fid = client.post("/api/deployments/dep_northfield/flags", headers=H("em"),
                      json={"text": "Customer IT unresponsive", "severity": "med"}).json()["id"]
    aid = client.post("/api/deployments/dep_harborview/approvals", headers=H("ai"),
                      json={"agent": "Intake agent", "request": "submit 12 prior-auths"}).json()["id"]
    events.process(client.conn)
    posts = web.called("POST", "chat.postMessage")
    by_text = {p["json"]["text"]: p["json"] for p in posts}
    flag = next(v for k, v in by_text.items() if "Customer IT unresponsive" in k)
    assert flag["channel"] == "C1"
    acts = [b for b in flag["blocks"] if b["type"] == "actions"][0]["elements"]
    assert {"fw:flag.take", "fw:flag.resolve", "fw:open"} == {a["action_id"] for a in acts}
    assert next(a for a in acts if a["action_id"] == "fw:flag.take")["value"] == fid
    appr = next(v for k, v in by_text.items() if "prior-auths" in k)
    ids = {a["action_id"]: a.get("value") for a in [b for b in appr["blocks"] if b["type"] == "actions"][0]["elements"]}
    assert ids["fw:approval.approve"] == aid and ids["fw:approval.reject"] == aid


def test_slack_button_acts_as_the_person_who_pressed_it(client, web):
    slack_fakes(web)
    install(client, "slack")
    r = press(client, "flag.take", "flg_nf_site", "UMARCUS")
    assert r.status_code == 200
    f = client.conn.execute("SELECT * FROM flags WHERE id='flg_nf_site'").fetchone()
    assert f["status"] == "owned" and f["handled_by"] == "usr_marcus"
    reply = web.called("POST", "hooks.slack.com/actions")[-1]["json"]
    assert reply["replace_original"] is True and "Marcus Hale took this" in reply["text"]
    assert not any(b["type"] == "actions" for b in reply["blocks"])
    trail = client.get("/api/audit", headers=H("head")).json()
    assert any(a["action"] == "flag.take" and a["subject"] == "flg_nf_site" and a["actor"] == "Marcus Hale"
               for a in trail)


def test_slack_button_respects_permissions_and_unknown_people(client, web):
    slack_fakes(web)
    install(client, "slack")
    press(client, "flag.resolve", "flg_ca_sponsor", "URUTH")  # the customer can't see internal flags
    assert client.conn.execute("SELECT status FROM flags WHERE id='flg_ca_sponsor'").fetchone()["status"] != "resolved"
    assert web.called("POST", "hooks.slack.com/actions")[-1]["json"]["response_type"] == "ephemeral"
    press(client, "flag.resolve", "flg_ca_sponsor", "USTRANGER")
    reply = web.called("POST", "hooks.slack.com/actions")[-1]["json"]
    assert "doesn't match anyone" in reply["text"]
    # an approval can't be decided by the person who asked for it
    aid = client.post("/api/deployments/dep_harborview/approvals", headers=H("head"),
                      json={"agent": "Intake agent", "request": "send letters"}).json()["id"]
    press(client, "approval.approve", aid, "UDANA")
    assert client.conn.execute("SELECT status FROM approvals WHERE id=?", (aid,)).fetchone()["status"] == "pending"
    assert "Couldn't do that" in web.called("POST", "hooks.slack.com/actions")[-1]["json"]["text"]
    press(client, "approval.approve", "apr_hv1", "UDANA")
    a = client.conn.execute("SELECT status, decided_by FROM approvals WHERE id='apr_hv1'").fetchone()
    assert (a["status"], a["decided_by"]) == ("approved", "usr_dana")


def test_slack_select_sets_waiting_on_and_confirms_delays(client, web):
    slack_fakes(web)
    install(client, "slack")
    press(client, "task.waiting_on", "tsk_003|customer", "USAM", selected=True)
    t = client.conn.execute("SELECT waiting_on FROM tasks WHERE id='tsk_003'").fetchone()
    assert t["waiting_on"] == "customer"
    d = client.post("/api/deployments/dep_castellan/delays", headers=H("head"),
                    json={"signal": "security_review", "reason": "Model gateway review"}).json()
    press(client, "delay.reassign", f"{d['id']}|software_vendor", "UDANA", selected=True)
    row = client.conn.execute("SELECT status, confirmed_owner, weight FROM delays WHERE id=?", (d["id"],)).fetchone()
    assert (row["status"], row["confirmed_owner"], row["weight"]) == ("reassigned", "software_vendor", 1.0)


def test_slack_rejects_bad_or_stale_signatures(client, web):
    slack_fakes(web)
    install(client, "slack")
    body = urlencode({"payload": json.dumps({"type": "block_actions"})}).encode()
    assert client.post("/hooks/slack/interact", content=body, headers=slack_sign(body, secret="nope")).status_code == 401
    assert client.post("/hooks/slack/interact", content=body,
                       headers=slack_sign(body, ts=time.time() - 600)).status_code == 401
    assert client.post("/hooks/slack/command", content=b"text=help", headers={}).status_code == 401


def test_slack_events_verification_and_uninstall(client, web):
    slack_fakes(web)
    install(client, "slack")
    body = json.dumps({"type": "url_verification", "challenge": "abc"}).encode()
    assert client.post("/hooks/slack/events", content=body, headers=slack_sign(body)).json() == {"challenge": "abc"}
    # Slack checks the URL while the app is being created, before its signing secret is on the server
    assert client.post("/hooks/slack/events", content=body).json() == {"challenge": "abc"}
    other = json.dumps({"type": "event_callback", "team_id": "T1", "event": {"type": "app_uninstalled"}}).encode()
    assert client.post("/hooks/slack/events", content=other).status_code == 401
    body = json.dumps({"type": "event_callback", "team_id": "T1", "event_id": "Ev1",
                       "event": {"type": "app_uninstalled"}}).encode()
    client.post("/hooks/slack/events", content=body, headers=slack_sign(body))
    client.post("/hooks/slack/events", content=body, headers=slack_sign(body))  # Slack retries: stored once
    assert the_cx(client, "slack") is None
    n = client.conn.execute("SELECT COUNT(*) n FROM inbound_events WHERE external_id='Ev1'").fetchone()["n"]
    assert n == 1


def command(client, web, user, text):
    body = urlencode({"team_id": "T1", "user_id": user, "text": text, "command": "/fieldwork",
                      "response_url": "https://hooks.slack.com/commands/T1/1/x"}).encode()
    r = client.post("/hooks/slack/command", content=body, headers=slack_sign(body))
    assert r.status_code == 200 and not r.content  # acknowledged at once; the answer follows
    return web.called("POST", "hooks.slack.com/commands")[-1]["json"]


def test_slash_command_today_and_status(client, web):
    slack_fakes(web)
    install(client, "slack")
    r = command(client, web, "USAM", "today")
    assert r["response_type"] == "ephemeral" and "Confirm 3-way-match" in r["text"]
    assert "Claims triage copilot" in command(client, web, "UDANA", "status castellan")["text"]
    assert "No deployment you can see" in command(client, web, "URUTH", "status castellan")["text"]


def test_assignment_sends_a_direct_message(client, web):
    slack_fakes(web)
    install(client, "slack")
    tid = client.post("/api/tasks", headers=H("em"), json={"deployment_id": "dep_northfield", "title": "Pull invoices",
                                                           "assignee_id": "usr_maya"}).json()["id"]
    events.process(client.conn)
    dm = [c["json"] for c in web.called("POST", "chat.postMessage") if c["json"]["channel"] == "UMAYA"]
    assert dm and "Pull invoices" in dm[0]["text"]
    btns = {b["action_id"]: b.get("value") for b in dm[0]["blocks"][1]["elements"]}
    assert btns["fw:task.start"] == tid and btns["fw:task.done"] == tid
    press(client, "task.done", tid, "UMAYA")
    assert client.conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()["status"] == "done"


def test_not_in_channel_falls_back_to_the_install_webhook(client, web):
    slack_fakes(web)
    install(client, "slack")
    web.json("POST", r"slack\.com/api/chat\.postMessage", {"ok": False, "error": "not_in_channel"})
    web.json("POST", r"hooks\.slack\.com/services", {})
    client.post("/api/deployments/dep_northfield/flags", headers=H("em"), json={"text": "x", "severity": "low"})
    cfg = client.get("/api/config", headers=H("head")).json()["config"]
    cfg["integrations"]["slack"]["events"] = ["flag.raised"]
    client.put("/api/config", headers=H("head"), json=cfg)
    client.post("/api/deployments/dep_northfield/flags", headers=H("em"), json={"text": "y", "severity": "low"})
    assert events.process(client.conn)["sent"] >= 1
    assert web.called("POST", "hooks.slack.com/services")


# ================================================================== trackers

def github_fakes(web):
    web.json("POST", r"github\.com/login/oauth/access_token", {"access_token": "gho_1", "scope": "repo"})
    web.json("GET", r"api\.github\.com/user$", {"id": 77, "login": "meridian-bot"})
    web.json("POST", r"api\.github\.com/repos/acme/ap/hooks$", {"id": 901})
    web.json("DELETE", r"api\.github\.com/repos/acme/ap/hooks/901$", {}, 204)
    web.route("POST", r"api\.github\.com/repos/acme/ap/issues$",
              lambda r: (201, {"number": 5, "html_url": "https://github.com/acme/ap/issues/5"}))
    web.json("PATCH", r"api\.github\.com/repos/acme/ap/issues/\d+$", {})


def gh_hook(client, cx, payload, delivery="d-1", secret=None):
    body = json.dumps(payload).encode()
    sig = "sha256=" + hmac.new((secret or cx.webhook["secret"]).encode(), body, hashlib.sha256).hexdigest()
    return client.post(f"/hooks/github/{cx.id}", content=body, headers={
        "X-GitHub-Event": "issues", "X-GitHub-Delivery": delivery, "X-Hub-Signature-256": sig,
        "Content-Type": "application/json"})


def test_github_install_links_repo_and_syncs_both_ways(client, web):
    github_fakes(web)
    install(client, "github")
    r = client.put("/api/deployments/dep_northfield/sync", headers=H("head"),
                   json={"provider": "github", "target": "acme/ap"})
    assert r.status_code == 200 and r.json()["webhook"]["status"] == "created"
    hook = web.called("POST", "repos/acme/ap/hooks")[0]["json"]
    cx = the_cx(client, "github")
    assert hook["config"]["url"] == f"https://fw.example/hooks/github/{cx.id}" and hook["events"] == ["issues"]
    assert hook["config"]["secret"] == cx.webhook["secret"]
    events.process(client.conn)
    created = web.called("POST", "repos/acme/ap/issues$")
    assert created and created[0]["headers"]["Authorization"] == "Bearer gho_1"
    link = client.conn.execute("SELECT * FROM task_links WHERE provider='github' LIMIT 1").fetchone()
    # closed in GitHub by someone mapped in the connection's people setting
    client.patch(f"/api/connections/{cx.id}/settings", headers=H("head"),
                 json={"settings": {"people": {"maya-gh": "maya@meridian.example"}}})
    assert link["external_id"].startswith("acme/ap#")
    payload = {"action": "closed", "repository": {"full_name": "acme/ap"},
               "issue": {"number": int(link["external_id"].split("#")[1]), "state": "closed", "title": "t",
                         "labels": [], "assignee": {"login": "maya-gh", "id": 3}}}
    r = gh_hook(client, cx, payload)
    assert r.status_code == 200 and r.json()["results"] == ["done"]
    t = client.conn.execute("SELECT status, assignee_id FROM tasks WHERE id=?", (link["task_id"],)).fetchone()
    assert t["status"] == "done" and t["assignee_id"] == "usr_maya"
    # GitHub redelivers: stored once, applied once
    assert gh_hook(client, cx, payload).json()["stored"] == 0
    assert gh_hook(client, cx, payload, secret="wrong").status_code == 401
    # disconnecting removes the repo webhook and forgets the token
    assert client.delete(f"/api/connections/{cx.id}", headers=H("head")).json()["ok"]
    assert web.called("DELETE", "hooks/901")
    assert client.conn.execute("SELECT tokens, status FROM connections WHERE id=?", (cx.id,)).fetchone()["tokens"] is None


def test_github_nightly_reconcile_catches_missed_webhooks(client, web):
    github_fakes(web)
    install(client, "github")
    client.put("/api/deployments/dep_northfield/sync", headers=H("head"), json={"provider": "github", "target": "acme/ap"})
    events.process(client.conn)
    link = client.conn.execute("SELECT * FROM task_links WHERE provider='github' LIMIT 1").fetchone()
    web.route("GET", r"repos/acme/ap/issues/\d+$", lambda r: (200, {
        "number": int(r["path"].rsplit("/", 1)[1]), "state": "open", "title": "renamed there",
        "labels": [{"name": "fieldwork:blocked"}]}))
    cx = the_cx(client, "github")
    res = client.post(f"/api/connections/{cx.id}/sync", headers=H("head"), json={"full": True}).json()
    assert "result" in res, res
    assert res["result"]["changed"] >= 1
    t = client.conn.execute("SELECT status, title FROM tasks WHERE id=?", (link["task_id"],)).fetchone()
    assert (t["status"], t["title"]) == ("blocked", "renamed there")
    assert res["health"]["last_reconcile_at"]


def test_linear_app_webhook_routes_by_organization(client, web):
    web.json("POST", r"api\.linear\.app/oauth/token", {"access_token": "lin_1", "expires_in": 86400,
                                                        "refresh_token": "r1"})
    web.route("POST", r"api\.linear\.app/graphql", lambda r: (200, {"data": (
        {"viewer": {"id": "u", "name": "D", "email": "dana@meridian.example"},
         "organization": {"id": "org-1", "name": "Meridian", "urlKey": "meridian"}}
        if "viewer" in r["json"]["query"] else
        {"team": {"states": {"nodes": [{"id": "s-open", "type": "unstarted", "position": 0},
                                       {"id": "s-done", "type": "completed", "position": 0}]}}}
        if "team(" in r["json"]["query"] else
        {"issueCreate": {"success": True, "issue": {"id": "LIN-1", "url": "https://linear.app/i/1"}}}
        if "issueCreate" in r["json"]["query"] else {"users": {"nodes": []}})}))
    install(client, "linear")
    cx = the_cx(client, "linear")
    assert cx["external_account_id"] == "org-1"
    client.put("/api/deployments/dep_redline/sync", headers=H("head"), json={"provider": "linear", "target": "team-1"})
    events.process(client.conn)
    link = client.conn.execute("SELECT * FROM task_links WHERE provider='linear'").fetchone()
    assert link["external_id"] == "LIN-1"
    payload = {"type": "Issue", "action": "update", "organizationId": "org-1", "webhookId": "wh1",
               "webhookTimestamp": int(time.time() * 1000),
               "data": {"id": "LIN-1", "title": "Systems inventory", "updatedAt": "2026-09-27T10:00:00Z",
                        "state": {"type": "completed"}, "assignee": {"id": "lu1", "email": "rosa@meridian.example"}}}
    body = json.dumps(payload).encode()
    sig = hmac.new(b"linear-hook-secret", body, hashlib.sha256).hexdigest()
    r = client.post("/hooks/linear/app", content=body, headers={"Linear-Signature": sig})
    assert r.json()["results"] == ["done"]
    assert client.conn.execute("SELECT status FROM tasks WHERE id=?", (link["task_id"],)).fetchone()["status"] == "done"
    assert client.post("/hooks/linear/app", content=body, headers={"Linear-Signature": "x"}).status_code == 401


def jwt(secret, claims):
    enc = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=").decode()  # noqa: E731
    h, p = enc(json.dumps({"alg": "HS256", "typ": "JWT"}).encode()), enc(json.dumps(claims).encode())
    return f"{h}.{p}." + enc(hmac.new(secret.encode(), f"{h}.{p}".encode(), hashlib.sha256).digest())


def test_jira_3lo_dynamic_webhook_renewal_and_signed_delivery(client, web):
    web.json("POST", r"auth\.atlassian\.com/oauth/token", {"access_token": "atl_1", "refresh_token": "ar",
                                                           "expires_in": 3600})
    web.json("GET", r"api\.atlassian\.com/oauth/token/accessible-resources", [
        {"id": "cloud-1", "url": "https://meridian.atlassian.net", "name": "meridian",
         "scopes": ["read:jira-work", "write:jira-work"]}])
    web.json("POST", r"/ex/jira/cloud-1/rest/api/3/webhook$", {"webhookRegistrationResult": [{"createdWebhookId": 55}]})
    web.json("PUT", r"/ex/jira/cloud-1/rest/api/3/webhook/refresh$", {"expirationDate": "2026-12-01T00:00:00Z"})
    web.json("POST", r"/ex/jira/cloud-1/rest/api/3/issue$", {"key": "AP-9"})
    install(client, "jira")
    cx = the_cx(client, "jira")
    assert cx.extra["cloud_id"] == "cloud-1"
    client.put("/api/deployments/dep_castellan/sync", headers=H("head"), json={"provider": "jira", "target": "AP"})
    reg = web.called("POST", "rest/api/3/webhook$")[0]["json"]
    assert reg["webhooks"][0]["jqlFilter"] == 'project in ("AP")'
    events.process(client.conn)
    link = client.conn.execute("SELECT * FROM task_links WHERE provider='jira' LIMIT 1").fetchone()
    assert link["url"].startswith("https://meridian.atlassian.net/browse/")
    # renewal comes due after 25 days
    cx = the_cx(client, "jira")
    later = core.now() + timedelta(days=26)
    assert ("renew" in {w for _, w in core.due_work(client.conn, later)})
    core.run_due(client.conn, later)
    assert web.called("PUT", "webhook/refresh")[0]["json"] == {"webhookIds": [55]}
    payload = {"webhookEvent": "jira:issue_updated", "timestamp": 1, "matchedWebhookIds": [55],
               "issue": {"key": link["external_id"], "fields": {"summary": "s",
                                                                "status": {"statusCategory": {"key": "done"}}}}}
    tok = jwt("jira-secret", {"iss": "x", "exp": int(time.time()) + 60})
    r = client.post(f"/hooks/jira/{cx.id}", json=payload, headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200
    assert client.conn.execute("SELECT status FROM tasks WHERE id=?", (link["task_id"],)).fetchone()["status"] == "done"
    bad = jwt("not-the-secret", {"exp": int(time.time()) + 60})
    assert client.post(f"/hooks/jira/{cx.id}", json=payload, headers={"Authorization": f"Bearer {bad}"}).status_code == 401
    assert client.post(f"/hooks/jira/{cx.id}?k=wrong", json=payload).status_code == 401


# ======================================================================= CRM

def sf_record(i, stage, prob, closed=False, won=False, mod="2026-09-20T10:00:00.000+0000", amount=100000):
    return {"Id": f"006{i}", "Name": f"Deal {i}", "Account": {"Name": f"Acct {i}", "Website": f"https://www.acct{i}.example"},
            "Amount": amount, "Probability": prob, "StageName": stage, "CloseDate": "2026-11-01",
            "IsClosed": closed, "IsWon": won, "SystemModstamp": mod, "Weekly_Hours__c": 30}


def test_salesforce_sync_maps_stages_and_moves_the_cursor(client, web):
    web.json("POST", r"login\.salesforce\.com/services/oauth2/token", {
        "access_token": "sf1", "refresh_token": "sfr", "instance_url": "https://acme.my.salesforce.com",
        "id": "https://login.salesforce.com/id/00D1/005X"})
    web.json("GET", r"login\.salesforce\.com/id/", {"organization_id": "00D1", "username": "dana@acme"})
    records = [sf_record(1, "Prospecting", 10), sf_record(2, "Negotiation", 90),
               sf_record(3, "Closed Won", 100, True, True, mod="2026-09-21T09:00:00.000+0000"),
               sf_record(4, "Closed Lost", 0, True, False)]
    seen = []

    def query(r):
        seen.append(r["query"]["q"])
        return (200, {"records": records, "done": True})
    web.route("GET", r"acme\.my\.salesforce\.com/services/data/v\d+\.\d+/query", query)
    install(client, "salesforce")
    cx = the_cx(client, "salesforce")
    client.patch(f"/api/connections/{cx.id}/settings", headers=H("head"),
                 json={"settings": {"hours_field": "Weekly_Hours__c", "stage_map": {"Prospecting": "qualified"}}})
    assert client.patch(f"/api/connections/{cx.id}/settings", headers=H("head"),
                        json={"settings": {"hours_field": "x; DROP"}}).status_code == 422
    res = client.post(f"/api/connections/{cx.id}/sync", headers=H("head"), json={}).json()["result"]
    assert res["created"] == 4 and res["won"] == 1
    opps = {o["external_id"]: o for o in client.conn.execute(
        "SELECT * FROM opportunities WHERE connection_id=?", (cx.id,))}
    assert [opps[f"006{i}"]["stage"] for i in (1, 2, 3, 4)] == ["qualified", "commit", "won", "lost"]
    assert opps["0061"]["weekly_hours"] == 30 and opps["0062"]["expected_start"] == "2026-11-01"
    assert "Weekly_Hours__c" in seen[-1] and "LAST_N_DAYS" in seen[-1]
    assert the_cx(client, "salesforce").cursor["modstamp"] == "2026-09-21T09:00:00Z"
    # next time only what changed, and nothing changed means nothing rewritten
    res = client.post(f"/api/connections/{cx.id}/sync", headers=H("head"), json={}).json()["result"]
    assert "SystemModstamp >= 2026-09-21T09:00:00Z" in seen[-1] and res["updated"] == 0
    # the pipeline board shows them
    board = client.get("/api/pipeline", headers=H("head")).json()
    assert any(dl["name"] == "Deal 2" for col in board["columns"] for dl in col["deals"])


def test_salesforce_refreshes_on_401(client, web):
    web.json("POST", r"login\.salesforce\.com/services/oauth2/token", {
        "access_token": "old", "refresh_token": "sfr", "instance_url": "https://acme.my.salesforce.com"})
    install(client, "salesforce")
    web.json("POST", r"login\.salesforce\.com/services/oauth2/token", {"access_token": "new"})
    web.route("GET", r"services/data/.*/query", lambda r: (
        (401, [{"errorCode": "INVALID_SESSION_ID"}]) if r["headers"]["Authorization"] == "Bearer old"
        else (200, {"records": [], "done": True})))
    cx = the_cx(client, "salesforce")
    assert client.post(f"/api/connections/{cx.id}/sync", headers=H("head"), json={}).status_code == 200
    assert the_cx(client, "salesforce").tokens["access_token"] == "new"
    assert the_cx(client, "salesforce").tokens["refresh_token"] == "sfr"


def test_won_deal_opens_a_deployment_once(client, web):
    web.json("POST", r"login\.salesforce\.com/services/oauth2/token", {
        "access_token": "sf1", "refresh_token": "sfr", "instance_url": "https://acme.my.salesforce.com"})
    web.json("GET", r"services/data/.*/query", {"records": [sf_record(7, "Closed Won", 100, True, True)]})
    install(client, "salesforce")
    cx = the_cx(client, "salesforce")
    client.post(f"/api/connections/{cx.id}/sync", headers=H("head"), json={})
    oid = client.conn.execute("SELECT id FROM opportunities WHERE external_id='0067'").fetchone()["id"]
    assert client.post(f"/api/opportunities/{oid}/open-deployment", headers=H("fde")).status_code == 403
    r = client.post(f"/api/opportunities/{oid}/open-deployment", headers=H("head"))
    assert r.status_code == 201, r.text
    dep = client.get(f"/api/deployments/{r.json()['deployment_id']}", headers=H("head")).json()
    assert dep["name"] == "Deal 7" and dep["stage"] == "discover"
    cust = client.conn.execute("SELECT * FROM customers WHERE id=?", (r.json()["customer_id"],)).fetchone()
    assert cust["name"] == "Acct 7" and cust["domains"] == "acct7.example"
    assert client.post(f"/api/opportunities/{oid}/open-deployment", headers=H("head")).status_code == 409


def hubspot_sign(method, uri, body, secret="hubspot-secret", ts=None):
    ts = str(ts or int(time.time() * 1000))
    sig = base64.b64encode(hmac.new(secret.encode(), method.encode() + uri.encode() + body + ts.encode(),
                                    hashlib.sha256).digest()).decode()
    return {"X-HubSpot-Signature-v3": sig, "X-HubSpot-Request-Timestamp": ts, "Content-Type": "application/json"}


def test_hubspot_signed_nudge_then_sync(client, web):
    web.json("POST", r"api\.hubapi\.com/oauth/v3/token$", {"access_token": "hs1", "refresh_token": "hsr",
                                                           "expires_in": 1800, "hub_id": 4242})
    web.json("POST", r"api\.hubapi\.com/oauth/v3/token/introspect", {"hub_id": 4242, "hub_domain": "meridian.com"})
    web.json("GET", r"crm/v3/pipelines/deals", {"results": [{"stages": [
        {"id": "appt", "label": "Appointment scheduled", "metadata": {"probability": "0.2", "isClosed": "false"}},
        {"id": "won", "label": "Closed won", "metadata": {"probability": "1.0", "isClosed": "true"}}]}]})
    web.json("POST", r"crm/v3/objects/deals/search", {"results": [
        {"id": "11", "properties": {"dealname": "Harbor expansion", "amount": "250000", "dealstage": "appt",
                                    "closedate": "2026-12-01T00:00:00Z", "hs_lastmodifieddate": "1790000000000"}},
        {"id": "12", "properties": {"dealname": "Keystone renewal", "amount": "90000", "dealstage": "won",
                                    "hs_is_closed_won": "true", "hs_is_closed": "true",
                                    "hs_lastmodifieddate": "1790000500000"}}]})
    web.json("POST", r"crm/v4/associations/deals/companies/batch/read", {"results": [
        {"from": {"id": "11"}, "to": [{"toObjectId": 501}]}]})
    web.json("POST", r"crm/v3/objects/companies/batch/read", {"results": [
        {"id": "501", "properties": {"name": "Harborview Health", "domain": "harborviewhealth.example"}}]})
    install(client, "hubspot")
    cx = the_cx(client, "hubspot")
    assert cx["external_account_id"] == "4242"
    body = json.dumps([{"eventId": 1, "subscriptionType": "deal.propertyChange", "portalId": 4242, "objectId": 11}]).encode()
    uri = "https://fw.example/hooks/hubspot/app"
    assert client.post("/hooks/hubspot/app", content=body,
                       headers=hubspot_sign("POST", uri, body, secret="wrong")).status_code == 401
    assert client.post("/hooks/hubspot/app", content=body,
                       headers=hubspot_sign("POST", uri, body, ts=int(time.time() * 1000) - 600_000)).status_code == 401
    assert client.post("/hooks/hubspot/app", content=body, headers=hubspot_sign("POST", uri, body)).json()["stored"] == 1
    core.run_due(client.conn)
    opps = {o["external_id"]: o for o in client.conn.execute("SELECT * FROM opportunities WHERE connection_id=?",
                                                              (cx.id,))}
    assert opps["11"]["stage"] == "qualified" and opps["11"]["customer"] == "Harborview Health"
    assert opps["12"]["stage"] == "won" and opps["11"]["value"] == 250000
    assert the_cx(client, "hubspot").cursor["modified_ms"] == 1790000500000
    ev = client.get(f"/api/connections/{cx.id}/events", headers=H("head")).json()
    assert ev[0]["status"] == "done"


# ================================================================ timesheets

def test_harvest_entries_map_people_and_projects(client, web):
    web.json("POST", r"id\.getharvest\.com/api/v2/oauth2/token", {"access_token": "hv1", "refresh_token": "hvr",
                                                                  "expires_in": 1209600})
    web.json("GET", r"id\.getharvest\.com/api/v2/accounts", {"accounts": [{"id": 99, "name": "Meridian",
                                                                           "product": "harvest"}]})
    web.json("GET", r"api\.harvestapp\.com/v2/users", {"users": [
        {"id": 1, "email": "maya@meridian.example"}, {"id": 2, "email": "contractor@else.example"}], "next_page": None})
    web.json("GET", r"api\.harvestapp\.com/v2/projects", {"projects": [
        {"id": 10, "name": "AP exception agent", "client": {"name": "Northfield Supply Co."}},
        {"id": 11, "name": "Internal", "client": {"name": "Meridian"}}], "next_page": None})
    today = date.today().isoformat()
    entries = [{"id": 5001, "user": {"id": 1}, "project": {"id": 10}, "spent_date": today, "hours": 3.5},
               {"id": 5002, "user": {"id": 1}, "project": {"id": 11}, "spent_date": today, "hours": 1},
               {"id": 5003, "user": {"id": 2}, "project": {"id": 10}, "spent_date": today, "hours": 8}]
    web.route("GET", r"api\.harvestapp\.com/v2/time_entries", lambda r: (200, {"time_entries": entries,
                                                                                "next_page": None}))
    install(client, "harvest")
    cx = the_cx(client, "harvest")
    res = client.post(f"/api/connections/{cx.id}/sync", headers=H("head"), json={}).json()["result"]
    assert res["created"] == 2 and res["unmatched_people"] == 1
    assert web.called("GET", "v2/time_entries")[0]["headers"]["Harvest-Account-Id"] == "99"
    rows = {r["external_id"]: r for r in client.conn.execute("SELECT * FROM time_entries WHERE connection_id=?",
                                                              (cx.id,))}
    assert rows["5001"]["deployment_id"] == "dep_northfield" and rows["5002"]["deployment_id"] is None
    week = client.get("/api/me/week", headers=H("fde")).json()
    assert week["logged"] >= 4.5
    # an edit there updates here; a deletion there is removed at the reconcile
    entries[0]["hours"] = 4
    del entries[1]
    res = client.post(f"/api/connections/{cx.id}/sync", headers=H("head"), json={"full": True}).json()["result"]
    assert res["updated"] == 1 and res["removed"] == 1
    opts = client.get(f"/api/connections/{cx.id}", headers=H("head")).json()["options"]
    assert {p["name"]: p["deployment_id"] for p in opts["projects"]}["AP exception agent"] == "dep_northfield"


def test_toggl_token_connect_and_report_pages(client, web):
    web.route("GET", r"api\.track\.toggl\.com/api/v9/me$", lambda r: (
        (200, {"id": 1, "default_workspace_id": 777}) if "Basic" in r["headers"].get("Authorization", "")
        and base64.b64decode(r["headers"]["Authorization"][6:]).startswith(b"good:") else (403, {})))
    web.json("GET", r"api/v9/workspaces/777$", {"id": 777, "name": "Meridian Toggl"})
    web.json("GET", r"api/v9/workspaces/777/users", [{"id": 31, "email": "sam@meridian.example"}])
    web.json("GET", r"api/v9/workspaces/777/projects", [{"id": 40, "name": "Claims triage copilot"}])
    today = date.today().isoformat()
    pages = iter([
        (200, [{"user_id": 31, "project_id": 40, "time_entries": [{"id": 1, "seconds": 7200, "start": today}]}],
         {"X-Next-ID": "2", "X-Next-Row-Number": "1"}),
        (200, [{"user_id": 31, "project_id": 40, "time_entries": [{"id": 2, "seconds": 3600, "start": today},
                                                                  {"id": 3, "seconds": -1, "start": today}]}], {})])
    web.route("POST", r"reports/api/v3/workspace/777/search/time_entries", lambda r: next(pages))
    assert client.post("/api/connections/toggl/token", headers=H("head"), json={"token": "bad-token"}).status_code == 422
    r = client.post("/api/connections/toggl/token", headers=H("head"), json={"token": "good"})
    assert r.status_code == 201 and r.json()["account_name"] == "Meridian Toggl"
    cx = the_cx(client, "toggl")
    res = client.post(f"/api/connections/{cx.id}/sync", headers=H("head"), json={}).json()["result"]
    assert res["created"] == 2  # the running entry waits until it stops
    second = web.called("POST", "search/time_entries")[1]["json"]
    assert second["first_id"] == 2 and second["first_row_number"] == 1
    assert client.conn.execute("SELECT SUM(hours) h FROM time_entries WHERE connection_id=? AND deployment_id="
                               "'dep_castellan'", (cx.id,)).fetchone()["h"] == 3


# ================================================================= calendars

def next_monday():
    t = date.today()
    return t + timedelta(days=7 - t.weekday())


def google_fakes(web, items):
    web.json("POST", r"oauth2\.googleapis\.com/token", {"access_token": "g1", "refresh_token": "gr", "expires_in": 3600})
    web.json("GET", r"openidconnect\.googleapis\.com/v1/userinfo", {"sub": "g-maya", "email": "maya@meridian.example"})
    state = {"items": items}

    def events_list(r):
        if r["query"].get("syncToken") == "expired":
            return (410, {"error": "gone"})
        return (200, {"items": state["items"], "nextSyncToken": "tok-2"})
    web.route("GET", r"calendar/v3/calendars/primary/events$", events_list)
    web.json("POST", r"calendar/v3/calendars/primary/events/watch", {
        "resourceId": "res-1", "expiration": str(int((time.time() + 7 * 86400) * 1000))})
    web.json("POST", r"calendar/v3/channels/stop", {}, 204)
    return state


def test_google_calendar_time_off_reduces_capacity(client, web):
    mon = next_monday()
    state = google_fakes(web, [
        {"id": "e1", "status": "confirmed", "eventType": "outOfOffice", "summary": "Out of office",
         "start": {"dateTime": f"{mon}T00:00:00Z"}, "end": {"dateTime": f"{mon + timedelta(days=1)}T23:59:00Z"}},
        {"id": "e2", "status": "confirmed", "summary": "PTO", "start": {"date": (mon + timedelta(days=4)).isoformat()},
         "end": {"date": (mon + timedelta(days=5)).isoformat()}},
        {"id": "e3", "status": "confirmed", "summary": "Customer sync", "start": {"dateTime": f"{mon}T15:00:00Z"},
         "end": {"dateTime": f"{mon}T16:00:00Z"}}])
    before = client.get(f"/api/me/week?week={mon}", headers=H("fde")).json()
    install(client, "google_calendar", who="fde")
    cx = the_cx(client, "google_calendar", "usr_maya")
    assert cx and cx.user_id == "usr_maya"
    off = {r["external_id"]: r for r in client.conn.execute("SELECT * FROM time_off WHERE connection_id=?", (cx.id,))}
    assert set(off) == {"e1", "e2"}  # only time off is stored, never the meeting
    assert off["e2"]["start_on"] == off["e2"]["end_on"] == (mon + timedelta(days=4)).isoformat()
    after = client.get(f"/api/me/week?week={mon}", headers=H("fde")).json()
    assert len(after["off_days"]) == 3 and after["available"] == round(before["available"] * 2 / 5, 1)
    # a push channel was opened with our key
    watch = web.called("POST", "events/watch")[0]["json"]
    assert watch["address"] == f"https://fw.example/hooks/google_calendar/{cx.id}"
    cx = the_cx(client, "google_calendar", "usr_maya")
    # a cancelled event arrives by push
    state["items"] = [{"id": "e2", "status": "cancelled"}]
    r = client.post(f"/hooks/google_calendar/{cx.id}", headers={
        "X-Goog-Channel-Id": cx.webhook["channel"]["id"], "X-Goog-Channel-Token": cx.webhook["key"],
        "X-Goog-Resource-State": "exists", "X-Goog-Message-Number": "2"})
    assert r.status_code == 202
    with client.conn.tx():
        client.conn.execute("UPDATE connections SET last_sync_at=NULL WHERE id=?", (cx.id,))
    core.run_due(client.conn)
    assert {r["external_id"] for r in client.conn.execute("SELECT * FROM time_off WHERE connection_id=?", (cx.id,))} \
        == {"e1"}
    assert client.post(f"/hooks/google_calendar/{cx.id}", headers={
        "X-Goog-Channel-Id": cx.webhook["channel"]["id"], "X-Goog-Channel-Token": "forged"}).status_code == 401
    # other people can't see or manage Maya's calendar connection; admins can
    assert client.get(f"/api/connections/{cx.id}", headers=SAM).status_code == 404
    assert client.get(f"/api/connections/{cx.id}", headers=H("head")).status_code == 200


def test_google_expired_sync_token_starts_over(client, web):
    google_fakes(web, [])
    install(client, "google_calendar", who="fde")
    cx = the_cx(client, "google_calendar", "usr_maya")
    with client.conn.tx():
        core.save(client.conn, cx, cursor={"sync_token": "expired"})
    assert core.run_sync(client.conn, the_cx(client, "google_calendar", "usr_maya")) == \
        {"added": 0, "changed": 0, "removed": 0}
    assert the_cx(client, "google_calendar", "usr_maya").cursor == {"sync_token": "tok-2"}


def test_outlook_calendar_handshake_delta_and_notifications(client, web):
    web.json("POST", r"login\.microsoftonline\.com/common/oauth2/v2\.0/token", {
        "access_token": "ms1", "refresh_token": "msr", "expires_in": 3600})
    web.json("GET", r"graph\.microsoft\.com/v1\.0/me$", {"id": "m-maya", "mail": "maya@meridian.example"})
    mon = next_monday()
    web.json("GET", r"me/calendarView/delta", {"value": [
        {"id": "o1", "subject": "Vacation", "isAllDay": True, "showAs": "oof",
         "start": {"dateTime": f"{mon}T00:00:00.0000000"}, "end": {"dateTime": f"{mon + timedelta(days=2)}T00:00:00.0000000"}},
        {"id": "o2", "subject": "Standup", "isAllDay": False, "showAs": "busy",
         "start": {"dateTime": f"{mon}T09:00:00.0000000"}, "end": {"dateTime": f"{mon}T09:15:00.0000000"}}],
        "@odata.deltaLink": "https://graph.microsoft.com/v1.0/me/calendarView/delta?$deltatoken=d1"})
    web.json("POST", r"graph\.microsoft\.com/v1\.0/subscriptions$", {"id": "sub-1"}, 201)
    install(client, "outlook_calendar", who="fde")
    cx = the_cx(client, "outlook_calendar", "usr_maya")
    off = client.conn.execute("SELECT * FROM time_off WHERE connection_id=?", (cx.id,)).fetchall()
    assert [(o["start_on"], o["end_on"]) for o in off] == [(mon.isoformat(), (mon + timedelta(days=1)).isoformat())]
    assert cx.cursor["delta"].endswith("d1") and cx.webhook["subscription"] == "sub-1"
    sub = web.called("POST", "v1.0/subscriptions$")[0]["json"]
    assert sub["clientState"] == cx.webhook["key"] and sub["resource"] == "me/events"
    r = client.post(f"/hooks/outlook_calendar/{cx.id}?validationToken=hello%20there")
    assert r.status_code == 200 and r.text == "hello there"
    good = {"value": [{"subscriptionId": "sub-1", "clientState": cx.webhook["key"], "changeType": "updated",
                       "resource": "me/events/o1"}]}
    assert client.post(f"/hooks/outlook_calendar/{cx.id}", json=good).status_code == 202
    bad = {"value": [{**good["value"][0], "clientState": "nope"}]}
    assert client.post(f"/hooks/outlook_calendar/{cx.id}", json=bad).status_code == 401


def test_time_off_by_hand(client):
    mon = next_monday()
    r = client.post("/api/time-off", headers=H("fde"), json={"start_on": str(mon), "end_on": str(mon), "title": "Dentist"})
    assert r.status_code == 201
    assert client.get(f"/api/me/week?week={mon}", headers=H("fde")).json()["off_days"] == [str(mon)]
    assert client.post("/api/time-off", headers=H("fde"), json={"start_on": str(mon), "end_on": str(mon),
                                                               "user_id": "usr_sam"}).status_code == 403
    assert client.post("/api/time-off", headers=H("customer"), json={"start_on": str(mon),
                                                                    "end_on": str(mon)}).status_code == 403
    assert client.post("/api/time-off", headers=H("fde"), json={"start_on": str(mon),
                                                               "end_on": str(mon - timedelta(days=1))}).status_code == 422
    team = client.get("/api/time-off", headers=H("head")).json()
    assert team[0]["user"] == "Maya Chen"
    assert client.delete(f"/api/time-off/{r.json()['id']}", headers=SAM).status_code == 404
    assert client.delete(f"/api/time-off/{r.json()['id']}", headers=H("fde")).json()["ok"]


# ===================================================================== email

def test_gmail_metadata_becomes_last_contact_and_a_quiet_flag(client, web):
    with client.conn.tx():  # start from no signal (the demo seeds some)
        client.conn.execute("DELETE FROM contact_signals WHERE connection_id='seed'")
    web.json("POST", r"oauth2\.googleapis\.com/token", {"access_token": "g1", "refresh_token": "gr", "expires_in": 3600})
    web.json("GET", r"openidconnect\.googleapis\.com/v1/userinfo", {"sub": "g-marcus", "email": "marcus@meridian.example"})
    web.json("GET", r"gmail/v1/users/me/profile", {"historyId": "100"})
    web.json("GET", r"gmail/v1/users/me/messages$", {"messages": [{"id": "m1"}, {"id": "m2"}, {"id": "m3"}]})
    old = int((time.time() - 14 * 86400) * 1000)
    recent = int((time.time() - 2 * 86400) * 1000)
    msgs = {
        "m1": {"id": "m1", "internalDate": str(old), "payload": {"headers": [
            {"name": "From", "value": "Ruth A <ruth@northfieldsupply.example>"},
            {"name": "To", "value": "marcus@meridian.example"}, {"name": "Subject", "value": "secret stuff"}]}},
        "m2": {"id": "m2", "internalDate": str(recent), "payload": {"headers": [
            {"name": "From", "value": "marcus@meridian.example"},
            {"name": "To", "value": "ops@northfieldsupply.example"}]}},
        "m3": {"id": "m3", "internalDate": str(recent), "payload": {"headers": [
            {"name": "From", "value": "friend@gmail.com"}, {"name": "To", "value": "marcus@meridian.example"}]}}}
    web.route("GET", r"gmail/v1/users/me/messages/m\d$", lambda r: (200, msgs[r["path"].rsplit("/", 1)[1]]))
    install(client, "gmail", who="em")
    cx = the_cx(client, "gmail", "usr_marcus")
    rows = client.conn.execute("SELECT * FROM contact_signals WHERE connection_id=?", (cx.id,)).fetchall()
    assert sorted((r["customer_id"], r["direction"]) for r in rows) == [("cus_northfield", "in"),
                                                                       ("cus_northfield", "out")]
    assert set(rows[0].keys()) == {"id", "tenant_id", "user_id", "connection_id", "customer_id", "domain",
                                   "direction", "at", "external_id"}  # no subject, no addresses
    assert all("secret" not in str(dict(r)) and "ruth@" not in str(dict(r)) for r in rows)
    fetch = web.called("GET", "messages/m1")[0]["query"]
    assert fetch["format"] == "metadata" and "q" not in web.called("GET", r"/messages(\?|$)")[0]["query"]
    row = next(r for r in client.get("/api/portfolio", headers=H("head")).json()["deployments"]
               if r["id"] == "dep_northfield")
    assert row["last_contact"]["outbound_days"] < 3 and row["last_contact"]["inbound_days"] > 13
    client.post("/api/sweep", headers=H("head"))
    flags = client.get("/api/flags?deployment_id=dep_northfield", headers=H("head")).json()
    assert any("No word from the customer in 14 days" in f["text"] for f in flags)
    # disconnecting deletes what it collected
    client.delete(f"/api/connections/{cx.id}", headers=H("em"))
    assert client.conn.execute("SELECT COUNT(*) n FROM contact_signals WHERE connection_id=?", (cx.id,)).fetchone()["n"] == 0


def test_customer_domains(client):
    r = client.put("/api/customers/cus_redline/domains", headers=H("head"),
                   json={"domains": ["@redline.example", "https://www.redline.example/x", "gmail.com", "bad"]})
    assert r.json()["domains"] == ["redline.example", "www.redline.example"]
    assert client.put("/api/customers/cus_redline/domains", headers=H("fde"), json={"domains": []}).status_code == 403


# ============================================================ health & replay

def test_failed_event_stays_pending_and_can_be_replayed(client, web, monkeypatch):
    github_fakes(web)
    install(client, "github")
    client.put("/api/deployments/dep_northfield/sync", headers=H("head"), json={"provider": "github", "target": "acme/ap"})
    cx = the_cx(client, "github")
    from fieldwork.connect import trackers as live
    boom = {"on": True}
    real = live.GitHubApp.handle

    def flaky(self, rt, cx_, kind, payload):
        if boom["on"]:
            raise RuntimeError("database hiccup")
        return real(self, rt, cx_, kind, payload)
    monkeypatch.setattr(live.GitHubApp, "handle", flaky)
    payload = {"repository": {"full_name": "acme/ap"}, "issue": {"number": 1, "state": "open", "title": "x", "labels": []}}
    assert gh_hook(client, cx, payload, delivery="d-9").json()["results"] == ["pending"]
    detail = client.get(f"/api/connections/{cx.id}", headers=H("head")).json()
    assert detail["health"]["events"]["pending"] == 1 and "database hiccup" in detail["health"]["last_error"]
    boom["on"] = False
    eid = client.get(f"/api/connections/{cx.id}/events?status=pending", headers=H("head")).json()[0]["id"]
    assert client.post(f"/api/connections/{cx.id}/events/{eid}/replay", headers=H("head")).json()["status"] == "done"
    assert client.post(f"/api/connections/{cx.id}/events/{eid}/replay", headers=H("fde")).status_code == 404


def test_scheduler_polls_reconciles_and_backs_off(client, web):
    web.json("POST", r"login\.salesforce\.com/services/oauth2/token", {
        "access_token": "sf1", "refresh_token": "sfr", "instance_url": "https://acme.my.salesforce.com"})
    web.json("GET", r"services/data/.*/query", {"records": [], "done": True})
    install(client, "salesforce")
    cx = the_cx(client, "salesforce")
    assert (cx.id, "reconcile") in {(c.id, w) for c, w in core.due_work(client.conn)}
    core.run_due(client.conn)
    assert cx.id not in {c.id for c, _ in core.due_work(client.conn)}
    soon = core.now() + timedelta(minutes=16)
    assert (cx.id, "poll") in {(c.id, w) for c, w in core.due_work(client.conn, soon)}
    web.json("GET", r"services/data/.*/query", {"message": "down"}, 503)
    core.run_due(client.conn, soon)
    after = the_cx(client, "salesforce")
    assert after["errors"] == 1 and after["retry_at"]
    assert core.parse(after["retry_at"]) > core.now()
    with client.conn.tx():  # overdue for a poll, but backing off after the failure
        client.conn.execute("UPDATE connections SET last_sync_at=? WHERE id=?",
                            (core.iso(core.now() - timedelta(hours=1)), cx.id))
    assert cx.id not in {c.id for c, _ in core.due_work(client.conn)}
    h = client.get(f"/api/connections/{cx.id}", headers=H("head")).json()["health"]
    assert h["state"] == "erroring"


def test_stream_says_when_something_changed(client):
    """A real server: the stream has to arrive while it's still open."""
    import queue
    import socket

    import httpx
    import uvicorn
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    server = uvicorn.Server(uvicorn.Config(client.app, host="127.0.0.1", port=port, log_level="error"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    base = f"http://127.0.0.1:{port}"
    got = queue.Queue()

    def listen():
        with httpx.stream("GET", f"{base}/api/stream?max_events=1&poll=0.25", headers=H("head"), timeout=10) as r:
            for line in r.iter_lines():
                if line.startswith("event:"):
                    got.put(line)
    th = threading.Thread(target=listen, daemon=True)
    th.start()
    try:
        assert got.get(timeout=5) == "event: hello"
        httpx.post(f"{base}/api/deployments/dep_northfield/flags", headers=H("em"),
                   json={"text": "ping", "severity": "low"})
        assert got.get(timeout=5) == "event: change"
        th.join(timeout=5)
        assert httpx.get(f"{base}/api/stream").status_code == 401
    finally:
        server.should_exit = True


def test_other_workspaces_never_see_connections(client, web):
    slack_fakes(web)
    install(client, "slack")
    cx = the_cx(client, "slack")
    other = {"Authorization": "Bearer demo-head-orbital"}
    assert client.get(f"/api/connections/{cx.id}", headers=other).status_code == 404
    assert all(not p["connections"] for p in client.get("/api/connections", headers=other).json()["providers"])


def test_webhooks_get_past_the_access_gate(db_url, monkeypatch, web):
    from fastapi.testclient import TestClient

    from fieldwork.app import create_app
    from fieldwork.seed import seed
    monkeypatch.setenv("FIELDWORK_ACCESS_PASSWORD", "letmein")
    seed(db_url)
    c = TestClient(create_app(db_url))
    body = json.dumps({"type": "url_verification", "challenge": "ok"}).encode()
    assert c.post("/hooks/slack/events", content=body, headers=slack_sign(body)).json() == {"challenge": "ok"}
    assert c.get("/", follow_redirects=False).status_code == 302  # people still meet the gate


# ======================================================= review regressions

def test_same_issue_number_in_another_repo_touches_nothing(client, web):
    github_fakes(web)
    install(client, "github")
    client.put("/api/deployments/dep_northfield/sync", headers=H("head"), json={"provider": "github", "target": "acme/ap"})
    events.process(client.conn)
    cx = the_cx(client, "github")
    link = client.conn.execute("SELECT * FROM task_links WHERE provider='github' LIMIT 1").fetchone()
    num = int(link["external_id"].split("#")[1])
    before = dict(client.conn.execute("SELECT status, title FROM tasks WHERE id=?", (link["task_id"],)).fetchone())
    gh_hook(client, cx, {"repository": {"full_name": "acme/other"},
                         "issue": {"number": num, "state": "closed", "title": "someone else's", "labels": []}},
            delivery="d-other")
    after = dict(client.conn.execute("SELECT status, title FROM tasks WHERE id=?", (link["task_id"],)).fetchone())
    assert after == before


def test_linking_a_new_repo_is_an_admin_call(client, web):
    github_fakes(web)
    install(client, "github")
    r = client.put("/api/deployments/dep_northfield/sync", headers=H("em"), json={"provider": "github", "target": "acme/secret"})
    assert r.status_code == 403 and not web.called("POST", "acme/secret/hooks")
    client.put("/api/deployments/dep_northfield/sync", headers=H("head"), json={"provider": "github", "target": "acme/ap"})
    assert client.put("/api/deployments/dep_harborview/sync", headers=H("em"),
                      json={"provider": "github", "target": "acme/ap"}).status_code == 200


def test_the_install_token_only_goes_to_its_own_host(client, web):
    github_fakes(web)
    install(client, "github")
    c = client.get("/api/config", headers=H("head")).json()["config"]
    c["integrations"]["github"]["api_base"] = "https://evil.example"
    client.put("/api/config", headers=H("head"), json=c)
    client.put("/api/deployments/dep_northfield/sync", headers=H("head"), json={"provider": "github", "target": "acme/ap"})
    events.process(client.conn)
    assert not [x for x in web.calls if "evil.example" in x["url"]]
    assert web.called("POST", r"api\.github\.com/repos/acme/ap/issues$")


def test_an_event_is_applied_once(client, web):
    github_fakes(web)
    install(client, "github")
    cx = the_cx(client, "github")
    eid = core.store_event(client.conn, cx, "dup-1", "ping", {})
    assert core.apply_event(client.conn, eid) == "done"
    assert core.apply_event(client.conn, eid) == "done"
    assert client.conn.execute("SELECT attempts FROM inbound_events WHERE id=?", (eid,)).fetchone()["attempts"] == 1


def test_reconnecting_harvest_does_not_double_hours(client, web):
    web.json("POST", r"id\.getharvest\.com/api/v2/oauth2/token", {"access_token": "hv1", "expires_in": 1209600})
    web.json("GET", r"id\.getharvest\.com/api/v2/accounts", {"accounts": [{"id": 99, "name": "M", "product": "harvest"}]})
    web.json("GET", r"v2/users", {"users": [{"id": 1, "email": "maya@meridian.example"}], "next_page": None})
    web.json("GET", r"v2/projects", {"projects": [], "next_page": None})
    web.json("GET", r"v2/time_entries", {"time_entries": [{"id": 7, "user": {"id": 1}, "project": {"id": 1},
                                                           "spent_date": date.today().isoformat(), "hours": 3.5}],
                                         "next_page": None})
    install(client, "harvest")
    cx = the_cx(client, "harvest")
    client.post(f"/api/connections/{cx.id}/sync", headers=H("head"), json={})
    client.delete(f"/api/connections/{cx.id}", headers=H("head"))
    install(client, "harvest", code="code-2")
    cx2 = the_cx(client, "harvest")
    client.post(f"/api/connections/{cx2.id}/sync", headers=H("head"), json={})
    rows = client.conn.execute("SELECT * FROM time_entries WHERE source='harvest'").fetchall()
    assert len(rows) == 1 and rows[0]["connection_id"] == cx2.id and rows[0]["hours"] == 3.5


def test_a_one_day_google_ooo_is_one_day(client, web):
    mon = next_monday()
    google_fakes(web, [{"id": "d1", "status": "confirmed", "eventType": "outOfOffice", "summary": "OOO",
                        "start": {"dateTime": f"{mon}T00:00:00-04:00"},
                        "end": {"dateTime": f"{mon + timedelta(days=1)}T00:00:00-04:00"}}])
    install(client, "google_calendar", who="fde")
    assert client.get(f"/api/me/week?week={mon}", headers=H("fde")).json()["off_days"] == [str(mon)]


def test_disconnecting_a_calendar_forgets_time_off_even_if_google_is_gone(client, web):
    mon = next_monday()
    google_fakes(web, [{"id": "d1", "status": "confirmed", "summary": "PTO", "start": {"date": str(mon)},
                        "end": {"date": str(mon + timedelta(days=1))}}])
    install(client, "google_calendar", who="fde")
    cx = the_cx(client, "google_calendar", "usr_maya")
    with client.conn.tx():  # the person revoked access in Google: refreshing fails
        client.conn.execute("UPDATE connections SET token_expires_at=? WHERE id=?", (core.iso(core.now()), cx.id))
    web.json("POST", r"oauth2\.googleapis\.com/token", {"error": "invalid_grant"}, 400)
    r = client.delete(f"/api/connections/{cx.id}", headers=H("fde"))
    assert r.json()["ok"] and r.json()["cleanup_error"]
    assert client.get(f"/api/me/week?week={mon}", headers=H("fde")).json()["off_days"] == []


def test_lapsed_jira_webhook_and_graph_subscription_are_recreated(client, web):
    web.json("POST", r"auth\.atlassian\.com/oauth/token", {"access_token": "a", "refresh_token": "r", "expires_in": 3600})
    web.json("GET", r"accessible-resources", [{"id": "c1", "url": "https://m.atlassian.net", "scopes": ["read:jira-work"]}])
    ids = iter([55, 56])
    web.route("POST", r"/ex/jira/c1/rest/api/3/webhook$", lambda r: (200, {"webhookRegistrationResult": [
        {"createdWebhookId": next(ids)}]}))
    web.json("PUT", r"webhook/refresh$", {"errorMessages": ["not found"]}, 404)
    web.json("DELETE", r"/rest/api/3/webhook$", {}, 202)
    install(client, "jira")
    client.put("/api/deployments/dep_castellan/sync", headers=H("head"), json={"provider": "jira", "target": "AP"})
    core.run_due(client.conn, core.now() + timedelta(days=26))
    assert the_cx(client, "jira").webhook["webhook_id"] == 56
    # Graph
    web.json("POST", r"login\.microsoftonline\.com/common/oauth2/v2\.0/token", {"access_token": "m", "refresh_token": "r",
                                                                                "expires_in": 3600})
    web.json("GET", r"graph\.microsoft\.com/v1\.0/me$", {"id": "m-maya", "mail": "maya@meridian.example"})
    web.json("GET", r"me/calendarView/delta", {"value": [], "@odata.deltaLink": "https://graph.microsoft.com/d?t=1"})
    subs = iter(["sub-1", "sub-2"])
    web.route("POST", r"v1\.0/subscriptions$", lambda r: (201, {"id": next(subs)}))
    web.json("PATCH", r"v1\.0/subscriptions/sub-1$", {"error": {"code": "ResourceNotFound"}}, 404)
    install(client, "outlook_calendar", who="fde")
    cx = the_cx(client, "outlook_calendar", "usr_maya")
    assert cx.webhook["subscription"] == "sub-1"
    core.run_due(client.conn, core.now() + timedelta(days=3))
    assert the_cx(client, "outlook_calendar", "usr_maya").webhook["subscription"] == "sub-2"


def test_one_slack_workspace_belongs_to_one_fieldwork_workspace(client, web):
    slack_fakes(web)
    install(client, "slack")
    other = {"Authorization": "Bearer demo-head-orbital"}
    r = client.post("/api/connections/slack/start", headers=other)
    state = parse_qs(urlparse(r.json()["url"]).query)["state"][0]
    back = client.get(f"/oauth/callback?state={state}&code=x", headers={"Cookie": r.headers["set-cookie"].split(";")[0]},
                      follow_redirects=False)
    assert "already connected to another" in back.headers["location"].replace("+", " ")


def test_hubspot_reads_companies_a_hundred_at_a_time(client, web):
    web.json("POST", r"oauth/v3/token$", {"access_token": "hs1", "expires_in": 1800, "hub_id": 1})
    web.json("POST", r"oauth/v3/token/introspect", {"hub_id": 1, "hub_domain": "x"})
    web.json("GET", r"crm/v3/pipelines/deals", {"results": []})
    deals = [{"id": str(i), "properties": {"dealname": f"D{i}", "hs_lastmodifieddate": str(1000 + i)}} for i in range(250)]
    web.json("POST", r"deals/search", {"results": deals})
    web.json("POST", r"associations/deals/companies/batch/read", {"results": [
        {"from": {"id": str(i)}, "to": [{"toObjectId": 10000 + i}]} for i in range(250)]})

    def companies(r):
        assert len(r["json"]["inputs"]) <= 100
        return (200, {"results": [{"id": x["id"], "properties": {"name": f"Co {x['id']}"}} for x in r["json"]["inputs"]]})
    web.route("POST", r"objects/companies/batch/read", companies)
    install(client, "hubspot")
    cx = the_cx(client, "hubspot")
    res = client.post(f"/api/connections/{cx.id}/sync", headers=H("head"), json={}).json()["result"]
    assert res["created"] == 250 and len(web.called("POST", "objects/companies/batch/read")) == 3
