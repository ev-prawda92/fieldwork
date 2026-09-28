"""The open beta: start a workspace, sign in with GitHub or Google, invites, feedback, and a demo
reset that never touches anyone's real workspace."""

from urllib.parse import parse_qs, urlparse

import pytest

from fieldwork import beta
from fieldwork.connect import http
from fieldwork.seed import reseed_demo

from .conftest import H
from .test_connect import FakeWeb


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("FIELDWORK_PUBLIC_URL", "https://fw.example")
    monkeypatch.setenv("FIELDWORK_OPEN_SIGNUP", "1")
    for p in ("GITHUB", "GOOGLE"):
        monkeypatch.setenv(f"FIELDWORK_{p}_CLIENT_ID", f"{p.lower()}-id")
        monkeypatch.setenv(f"FIELDWORK_{p}_CLIENT_SECRET", f"{p.lower()}-secret")
    beta._signups.clear()


@pytest.fixture()
def web(monkeypatch):
    w = FakeWeb()
    monkeypatch.setattr(http, "transport", w)
    return w


def gh(web, email="ana@acme.example", verified=True, name="Ana Ruiz"):
    web.json("POST", r"github\.com/login/oauth/access_token", {"access_token": "gho_login"})
    web.json("GET", r"api\.github\.com/user$", {"id": 9, "login": "ana", "name": name})
    web.json("GET", r"api\.github\.com/user/emails", [{"email": email, "primary": True, "verified": verified}])


def login(client, provider="github", mode="signin", workspace=""):
    r = client.post("/api/beta/start", json={"provider": provider, "mode": mode, "workspace": workspace})
    assert r.status_code == 200, r.text
    q = parse_qs(urlparse(r.json()["url"]).query)
    back = client.get(f"/oauth/callback?state={q['state'][0]}&code=c1",
                      headers={"Cookie": r.headers["set-cookie"].split(";")[0]}, follow_redirects=False)
    return q, back.headers["location"]


def session_of(location: str) -> str | None:
    frag = parse_qs(urlparse(location).fragment)
    return frag.get("session", [None])[0]


def test_start_a_workspace_with_github(client, web):
    assert client.get("/api/beta").json() == {"signup": True, "providers": ["github", "google"], "demo": False}
    gh(web)
    q, loc = login(client, mode="signup", workspace="Acme Deployments")
    assert q["scope"] == ["read:user user:email"]  # sign-in scopes only, never repo access
    tok = session_of(loc)
    assert tok and "note=welcome" in loc
    me = client.get("/api/me", headers={"Authorization": f"Bearer {tok}"}).json()
    assert me["user"]["role"] == "head" and me["user"]["email"] == "ana@acme.example"
    assert me["tenant"]["name"] == "Acme Deployments" and me["tenant"]["slug"] == "acme-deployments"
    assert client.get("/api/deployments", headers={"Authorization": f"Bearer {tok}"}).json() == []
    # the new workspace sees nothing of the demo's
    assert client.get("/api/deployments/dep_northfield", headers={"Authorization": f"Bearer {tok}"}).status_code == 404
    # starting again with the same email signs you in to what you have
    _, loc2 = login(client, mode="signup", workspace="Another")
    assert "already" in loc2 and client.conn.execute(
        "SELECT COUNT(*) n FROM tenants WHERE created_via='signup'").fetchone()["n"] == 1


def test_invited_people_sign_in_with_the_email_they_were_added_with(client, web):
    gh(web)
    _, loc = login(client, mode="signup", workspace="Acme")
    head = {"Authorization": f"Bearer {session_of(loc)}"}
    r = client.post("/api/people", headers=head, json={"name": "Bo Lin", "email": "Bo@acme.example", "role": "fde"}).json()
    assert r["sign_in_with"] == ["GitHub", "Google"] and r["sign_in_url"] == "https://fw.example"
    web.json("POST", r"oauth2\.googleapis\.com/token", {"access_token": "g"})
    web.json("GET", r"openidconnect\.googleapis\.com/v1/userinfo", {"email": "bo@acme.example", "email_verified": True})
    q, loc = login(client, provider="google")
    assert q["prompt"] == ["select_account"] and "access_type" not in q
    me = client.get("/api/me", headers={"Authorization": f"Bearer {session_of(loc)}"}).json()
    assert me["user"]["name"] == "Bo Lin" and me["user"]["role"] == "fde"


def test_sign_in_needs_an_account_and_a_verified_email(client, web):
    gh(web, email="stranger@nowhere.example")
    _, loc = login(client)
    assert "login_error" in loc and "No workspace has stranger@nowhere.example" in parse_qs(urlparse(loc).query)["login_error"][0]
    gh(web, verified=False)
    _, loc = login(client, mode="signup", workspace="X")
    assert "no verified email" in parse_qs(urlparse(loc).query)["login_error"][0]


def test_demo_accounts_are_never_matched_at_sign_in(client, web, monkeypatch):
    monkeypatch.setenv("FIELDWORK_DEMO", "1")
    gh(web, email="maya@meridian.example")
    _, loc = login(client)
    assert session_of(loc) is None and "login_error" in loc


def test_signup_switches_and_limits(client, web, monkeypatch):
    monkeypatch.setenv("FIELDWORK_OPEN_SIGNUP", "0")
    r = client.post("/api/beta/start", json={"provider": "github", "mode": "signup", "workspace": "X"})
    assert r.status_code == 403
    monkeypatch.setenv("FIELDWORK_OPEN_SIGNUP", "1")
    assert client.post("/api/beta/start", json={"provider": "github", "mode": "signup"}).status_code == 422
    monkeypatch.delenv("FIELDWORK_GOOGLE_CLIENT_ID")
    assert client.post("/api/beta/start", json={"provider": "google", "mode": "signin"}).status_code == 409
    monkeypatch.setenv("FIELDWORK_BETA_MAX_WORKSPACES", "0")
    gh(web)
    _, loc = login(client, mode="signup", workspace="Full")
    assert "full" in parse_qs(urlparse(loc).query)["login_error"][0]
    for _ in range(5):
        client.post("/api/beta/start", json={"provider": "github", "mode": "signup", "workspace": "Spam"})
    assert client.post("/api/beta/start", json={"provider": "github", "mode": "signup",
                                                "workspace": "Spam"}).status_code == 429


def test_demo_reset_leaves_real_workspaces_alone(client, web):
    gh(web)
    _, loc = login(client, mode="signup", workspace="Acme")
    head = {"Authorization": f"Bearer {session_of(loc)}"}
    cid = client.post("/api/customers", headers=head, json={"name": "Globex"}).json()["id"]
    did = client.post("/api/deployments", headers=head, json={"customer_id": cid, "name": "Claims agent"}).json()["id"]
    client.post("/api/deployments/dep_northfield/flags", headers=H("em"), json={"text": "demo noise", "severity": "low"})
    reseed_demo(client.conn)
    assert client.get(f"/api/deployments/{did}", headers=head).status_code == 200
    assert client.get("/api/audit/verify", headers=head).json()["ok"]
    flags = client.get("/api/flags?deployment_id=dep_northfield", headers=H("head")).json()
    assert not any(f["text"] == "demo noise" for f in flags)  # the demo is back to its seed
    assert client.get("/api/audit/verify", headers=H("head")).json()["ok"]


def test_no_connections_in_the_shared_demo(client, monkeypatch):
    monkeypatch.setenv("FIELDWORK_DEMO", "1")
    monkeypatch.setenv("FIELDWORK_SLACK_CLIENT_ID", "s")
    monkeypatch.setenv("FIELDWORK_SLACK_CLIENT_SECRET", "s")
    monkeypatch.setenv("FIELDWORK_SLACK_SIGNING_SECRET", "s")
    r = client.post("/api/connections/slack/start", headers=H("head"))
    assert r.status_code == 403 and "Start your own workspace" in r.json()["detail"]
    assert client.post("/api/connections/toggl/token", headers=H("head"), json={"token": "abcd"}).status_code == 403


def test_feedback_reaches_the_operator(client, monkeypatch):
    assert client.post("/api/feedback", headers=H("fde"), json={"text": "Love the delay ledger", "page": "home"}).status_code == 201
    assert client.get("/api/operator/feedback").status_code == 404
    monkeypatch.setenv("FIELDWORK_OPERATOR_TOKEN", "op")
    rows = client.get("/api/operator/feedback", headers={"X-Operator-Token": "op"}).json()
    assert rows[0]["text"] == "Love the delay ledger" and rows[0]["name"] == "Maya Chen"
    assert client.post("/api/feedback", json={"text": "x"}).status_code == 401
