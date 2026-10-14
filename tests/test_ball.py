"""The ball: pass, send back, stack, redirect, call back, hand off, and what it leaves in the record."""

import pytest

from fieldwork import audit

from .conftest import H

SAM = {"Authorization": "Bearer demo-usr_sam-meridian"}

# tsk_001: Maya's internal task on Northfield. tsk_002: Rosa's task on Northfield, shared with the customer.
# Northfield is staffed by Marcus (em), Maya (fde), Sam, Rosa (ic) and Ruth (customer).


@pytest.fixture(autouse=True)
def _start_with_every_ball_home(client):
    """The demo seeds a few balls in play; these tests start from a clean court."""
    c = client.conn
    c.execute("DELETE FROM ball_passes")
    c.execute("UPDATE tasks SET holder_id=NULL")
    c.execute("DELETE FROM delays WHERE dedupe_key LIKE 'ball:%'")
    c.commit()


def test_the_demo_has_balls_in_play(db_url):
    from fastapi.testclient import TestClient
    from fieldwork.app import create_app
    from fieldwork.seed import seed
    seed(db_url)
    cl = TestClient(create_app(db_url))
    b = cl.get("/api/ball", headers=H("ic")).json()
    assert "tsk_001" in [t["id"] for t in b["holding"]]  # Maya passed it to Rosa
    assert {"tsk_002", "tsk_010"} <= {t["id"] for t in b["waiting"]}
    assert "tsk_002" in [t["id"] for t in cl.get("/api/ball", headers=H("customer")).json()["holding"]]
    d = next(x for x in cl.get("/api/delays", headers=H("head")).json() if x["dedupe_key"] == "ball:tsk_002")
    assert d["open"] and d["proposed_owner"] == "customer"
    assert audit.verify(cl.app.state.conn, "ten_meridian")["ok"]


def ball(client, who):
    return client.get("/api/ball", headers=H(who) if isinstance(who, str) else who).json()


def task(client, tid, who="head"):
    return next(t for t in client.get("/api/tasks?deployment_id=dep_northfield", headers=H(who)).json()
                if t["id"] == tid)


def pass_(client, tid, to, who, note=""):
    return client.post(f"/api/tasks/{tid}/pass", headers=H(who) if isinstance(who, str) else who,
                       json={"to": to, "note": note})


def back(client, tid, who, note=""):
    return client.post(f"/api/tasks/{tid}/return", headers=H(who) if isinstance(who, str) else who,
                       json={"note": note})


def delays(client):
    return client.get("/api/delays", headers=H("head")).json()


def test_pass_and_send_back(client):
    assert task(client, "tsk_001")["ball"]["holder_id"] == "usr_maya"
    r = pass_(client, "tsk_001", "usr_rosa", "fde", "Need the exception export mapping")
    assert r.status_code == 200 and r.json()["holder"] == "Rosa Delgado"

    b = task(client, "tsk_001")["ball"]
    assert b["holder_id"] == "usr_rosa" and b["out"] and b["from"] == "Maya Chen"
    assert b["note"] == "Need the exception export mapping"
    held = ball(client, "ic")["holding"]
    assert held[0]["id"] == "tsk_001"  # passed-to-you comes first
    assert [t["id"] for t in ball(client, "fde")["waiting"]] == ["tsk_001"]
    assert "tsk_001" not in [t["id"] for t in ball(client, "fde")["holding"]]

    assert back(client, "tsk_001", "ic", "Mapping's in the drive").status_code == 200
    b = task(client, "tsk_001")["ball"]
    assert b["holder_id"] == "usr_maya" and not b["out"]
    assert ball(client, "fde")["waiting"] == []
    assert "tsk_001" in [t["id"] for t in ball(client, "fde")["holding"]]
    assert back(client, "tsk_001", "fde").status_code == 422  # already home


def test_passes_stack_and_unwind(client):
    assert pass_(client, "tsk_001", "usr_rosa", "fde").status_code == 200
    assert pass_(client, "tsk_001", "usr_marcus", "ic").status_code == 200  # Rosa needs Marcus for her part
    assert task(client, "tsk_001")["ball"]["from"] == "Rosa Delgado"
    assert back(client, "tsk_001", "em").json()["holder_id"] == "usr_rosa"
    assert back(client, "tsk_001", "ic").json()["holder_id"] == "usr_maya"
    moves = client.get("/api/tasks/tsk_001/ball", headers=H("fde")).json()["moves"]
    assert [(m["from"], m["to"], m["back_how"]) for m in moves] == [
        ("Maya Chen", "Rosa Delgado", "returned"), ("Rosa Delgado", "Marcus Hale", "returned")]
    assert all(m["held_hours"] is not None for m in moves)


def test_passing_straight_back_is_sending_it_back(client):
    pass_(client, "tsk_001", "usr_rosa", "fde")
    assert pass_(client, "tsk_001", "usr_maya", "ic", "done my bit").status_code == 200
    assert not task(client, "tsk_001")["ball"]["out"]
    moves = client.get("/api/tasks/tsk_001/ball", headers=H("fde")).json()["moves"]
    assert len(moves) == 1 and moves[0]["back_how"] == "returned" and moves[0]["back_note"] == "done my bit"


def test_who_can_move_the_ball(client):
    pass_(client, "tsk_001", "usr_rosa", "fde")
    # Sam isn't holding it and doesn't own it.
    assert pass_(client, "tsk_001", "usr_marcus", SAM).status_code == 403
    assert back(client, "tsk_001", SAM).status_code == 403
    # Jordan isn't on Northfield, so he can't see the task at all, and can't be passed it.
    assert pass_(client, "tsk_001", "usr_rosa", "ai").status_code == 404
    r = pass_(client, "tsk_001", "usr_jordan", "ic")
    assert r.status_code == 422 and "add them to this deployment" in r.json()["detail"]
    # Another workspace sees nothing.
    assert pass_(client, "tsk_001", "usr_rosa", "other_tenant").status_code == 404
    assert pass_(client, "tsk_001", "usr_rosa", "ic").status_code == 422  # she already has it


def test_lead_redirects_and_owner_calls_it_back(client):
    pass_(client, "tsk_001", "usr_rosa", "fde")
    # Marcus (a lead on Northfield) redirects Rosa's ball to Sam: it still comes home to Maya.
    assert pass_(client, "tsk_001", "usr_sam", "em").status_code == 200
    b = task(client, "tsk_001")["ball"]
    assert b["holder_id"] == "usr_sam" and b["from"] == "Maya Chen"
    assert ball(client, "ic")["holding"][0]["id"] != "tsk_001"
    # Maya, the owner, calls it back.
    assert back(client, "tsk_001", "fde").json()["holder_id"] == "usr_maya"
    hows = [m["back_how"] for m in client.get("/api/tasks/tsk_001/ball", headers=H("fde")).json()["moves"]]
    assert hows == ["redirected", "called_back"]


def test_ball_with_the_customer_opens_a_delay_until_it_comes_back(client):
    r = pass_(client, "tsk_001", "usr_ruth", "fde")
    assert r.status_code == 422 and "share the task with the customer" in r.json()["detail"]
    assert pass_(client, "tsk_002", "usr_ruth", "ic", "Need the AP lead's availability").status_code == 200
    d = next(x for x in delays(client) if x["dedupe_key"] == "ball:tsk_002")
    assert d["open"] and d["proposed_owner"] == "customer"
    assert d["proposed_reason"] == "Need the AP lead's availability"
    assert [t["id"] for t in ball(client, "customer")["holding"]][:1] == ["tsk_002"]
    assert back(client, "tsk_002", "customer", "Thursday works").status_code == 200
    d = next(x for x in delays(client) if x["dedupe_key"] == "ball:tsk_002")
    assert not d["open"]
    # A second trip is a second span, not one stretched over the gap.
    pass_(client, "tsk_002", "usr_ruth", "ic")
    assert sum(1 for x in delays(client) if x["dedupe_key"].startswith("ball:tsk_002")) == 2


def test_hand_off_moves_ownership(client):
    pass_(client, "tsk_001", "usr_rosa", "fde")
    assert client.post("/api/tasks/tsk_001/handoff", headers=H("ic"), json={"to": "usr_sam"}).status_code == 403
    # Maya owns it, so she can hand it off without being able to assign other people's work.
    r = client.post("/api/tasks/tsk_001/handoff", headers=H("fde"), json={"to": "usr_sam", "note": "Yours now"})
    assert r.status_code == 200
    t = task(client, "tsk_001")
    assert t["assignee_id"] == "usr_sam" and t["ball"]["holder_id"] == "usr_sam" and not t["ball"]["out"]
    assert ball(client, "ic")["holding"] == [] or "tsk_001" not in [x["id"] for x in ball(client, "ic")["holding"]]
    assert ball(client, "fde")["waiting"] == []
    moves = client.get("/api/tasks/tsk_001/ball", headers=H("fde")).json()["moves"]
    assert [m["kind"] for m in moves] == ["pass", "handoff"] and moves[0]["back_how"] == "handed_off"


def test_done_and_reassignment_bring_the_ball_home(client):
    pass_(client, "tsk_001", "usr_rosa", "fde")
    assert client.patch("/api/tasks/tsk_001", headers=H("fde"), json={"status": "done"}).status_code == 200
    t = task(client, "tsk_001")
    assert not t["ball"]["out"] and t["holder_id"] is None
    assert ball(client, "fde")["waiting"] == []
    assert pass_(client, "tsk_001", "usr_rosa", "fde").status_code == 422  # done
    client.patch("/api/tasks/tsk_001", headers=H("fde"), json={"status": "open"})
    pass_(client, "tsk_001", "usr_rosa", "fde")
    assert client.patch("/api/tasks/tsk_001", headers=H("em"), json={"assignee_id": "usr_sam"}).status_code == 200
    t = task(client, "tsk_001")
    assert t["ball"]["holder_id"] == "usr_sam" and not t["ball"]["out"]


def test_catchers_are_the_people_who_can_see_the_task(client):
    internal = {p["id"] for p in client.get("/api/tasks/tsk_001/catchers", headers=H("fde")).json()}
    assert {"usr_maya", "usr_rosa", "usr_sam", "usr_marcus", "usr_dana"} <= internal
    assert "usr_ruth" not in internal and "usr_jordan" not in internal
    shared = client.get("/api/tasks/tsk_002/catchers", headers=H("ic")).json()
    ruth = next(p for p in shared if p["id"] == "usr_ruth")
    assert ruth["customer_side"] and ruth["on_deployment"]


def test_deployment_summary_and_audit(client):
    pass_(client, "tsk_001", "usr_rosa", "fde")
    pass_(client, "tsk_002", "usr_ruth", "ic")
    s = client.get("/api/deployments/dep_northfield/ball", headers=H("em")).json()
    rosa = next(p for p in s["holders"] if p["id"] == "usr_rosa")
    assert rosa["passed_to_them"] == 1 and s["passes_30d"] == 2
    assert {h["id"] for h in s["held_30d"]} == {"usr_rosa", "usr_ruth"}
    # The customer only sees time on shared work.
    cs = client.get("/api/deployments/dep_northfield/ball", headers=H("customer")).json()
    assert {h["id"] for h in cs["held_30d"]} == {"usr_ruth"}
    acts = [a["action"] for a in client.get("/api/audit", headers=H("head")).json()]
    assert "ball.pass" in acts
    assert audit.verify(client.conn, "ten_meridian")["ok"]


def test_a_pass_notifies_slack_and_webhooks(client):
    client.conn.execute("DELETE FROM outbox")
    client.conn.commit()
    pass_(client, "tsk_001", "usr_rosa", "fde", "your turn")
    kinds = [r["kind"] for r in client.conn.execute("SELECT kind FROM outbox").fetchall()]
    # The demo workspace has a webhook subscribed to every event; Slack may or may not be installed.
    assert kinds == [] or all(k in ("slack", "slack_dm", "webhook") for k in kinds)


def test_an_agent_moves_the_ball_as_its_person(client):
    rpc = lambda method, params=None: client.post("/mcp", headers=H("fde"), json={
        "jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}).json()
    names = {t["name"] for t in rpc("tools/list")["result"]["tools"]}
    assert {"my_ball", "pass_ball", "send_ball_back", "hand_off_task", "who_can_catch"} <= names
    out = rpc("tools/call", {"name": "pass_ball", "arguments": {"task_id": "tsk_001", "to": "usr_rosa",
                                                                 "note": "Agent: need the mapping"}})["result"]
    assert not out["isError"]
    mine = rpc("tools/call", {"name": "my_ball", "arguments": {}})["result"]
    assert "tsk_001" in mine["content"][0]["text"]
    assert task(client, "tsk_001")["ball"]["holder_id"] == "usr_rosa"
