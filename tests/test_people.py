"""Provisioning: add a whole team from a CSV, and offboard someone cleanly."""

from urllib.parse import parse_qs, urlparse

from fieldwork.seed import DEMO_TOKENS

from .conftest import H
from .test_beta import _env, gh, login, session_of, web  # noqa: F401  (fixtures)

SAM = {"Authorization": "Bearer demo-usr_sam-meridian"}


def test_offboarding_ends_access_and_hands_over_the_work(client):
    open_before = client.conn.execute("SELECT COUNT(*) n FROM tasks WHERE assignee_id='usr_sam' AND status!='done'"
                                      ).fetchone()["n"]
    assert open_before >= 2
    pt = client.post("/api/me/tokens", headers=SAM, json={"name": "cursor"}).json()["token"]
    with client.conn.tx():
        client.conn.execute("INSERT INTO time_off (id, tenant_id, user_id, start_on, end_on, source, external_id,"
                            " connection_id, title, created_at) VALUES ('off_x','ten_meridian','usr_sam','2026-10-05',"
                            "'2026-10-06','google_calendar','e1','con_sam','OOO','2026-09-01')")
        client.conn.execute("INSERT INTO connections (id, tenant_id, provider, user_id, status, created_by, created_at,"
                            " updated_at) VALUES ('con_sam','ten_meridian','google_calendar','usr_sam','active','usr_sam',"
                            "'2026-09-01','2026-09-01')")
    assert client.post("/api/people/usr_sam/deactivate", headers=H("em"), json={}).status_code == 403
    r = client.post("/api/people/usr_sam/deactivate", headers=H("head"), json={"reassign_to": "usr_maya"})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["tasks_reassigned"] + out["tasks_unassigned"] == open_before and out["connections_removed"] == 1
    # every way in is closed
    assert client.get("/api/me", headers=SAM).status_code == 401
    assert client.get("/api/me", headers={"Authorization": f"Bearer {pt}"}).status_code == 401
    # nothing is left with them, and nothing was handed to someone who couldn't see it
    assert client.conn.execute("SELECT COUNT(*) n FROM tasks WHERE assignee_id='usr_sam' AND status!='done'"
                               ).fetchone()["n"] == 0
    assert client.conn.execute("SELECT COUNT(*) n FROM deployment_members WHERE user_id='usr_sam'").fetchone()["n"] == 0
    assert client.conn.execute("SELECT COUNT(*) n FROM time_off WHERE user_id='usr_sam'").fetchone()["n"] == 0
    assert "usr_sam" not in {p["user_id"] for p in client.get("/api/team", headers=H("head")).json()["people"]}
    # they can't be given new work
    r = client.post("/api/tasks", headers=H("head"), json={"deployment_id": "dep_northfield", "title": "x",
                                                         "assignee_id": "usr_sam"})
    assert r.status_code == 422 and "deactivated" in r.json()["detail"]
    trail = client.get("/api/audit", headers=H("head")).json()
    assert any(a["action"] == "people.deactivate" and a["subject"] == "usr_sam" for a in trail)
    assert client.get("/api/audit/verify", headers=H("head")).json()["ok"]
    # the roster still shows them, marked
    sam = next(p for p in client.get("/api/people", headers=H("head")).json() if p["id"] == "usr_sam")
    assert sam["active"] == 0 and sam["deactivated_at"]


def test_offboarding_guards(client):
    assert client.post("/api/people/usr_dana/deactivate", headers=H("head"), json={}).status_code == 422  # yourself
    assert client.post("/api/people/usr_nobody/deactivate", headers=H("head"), json={}).status_code == 404
    assert client.post("/api/people/usr_sam/deactivate", headers=H("head"),
                       json={"reassign_to": "usr_sam"}).status_code == 422
    other = {"Authorization": f"Bearer {DEMO_TOKENS['other_tenant']}"}
    assert client.post("/api/people/usr_sam/deactivate", headers=other, json={}).status_code == 404
    client.post("/api/people/usr_sam/deactivate", headers=H("head"), json={})
    assert client.post("/api/people/usr_sam/deactivate", headers=H("head"), json={}).status_code == 409


def test_reactivated_people_sign_in_again(client, web):  # noqa: F811
    client.post("/api/people/usr_rosa/deactivate", headers=H("head"), json={})
    r = client.post("/api/people", headers=H("head"), json={"name": "Rosa D", "email": "rosa@meridian.example",
                                                          "role": "fde"})
    assert r.status_code == 409 and "reactivate" in r.json()["detail"]
    gh(web, email="rosa@meridian.example")
    _, loc = login(client)
    assert session_of(loc) is None  # deactivated: sign-in refused
    assert client.post("/api/people/usr_rosa/reactivate", headers=H("head")).json()["ok"]
    _, loc = login(client)
    me = client.get("/api/me", headers={"Authorization": f"Bearer {session_of(loc)}"}).json()
    assert me["user"]["id"] == "usr_rosa"


def test_add_a_team_from_a_csv(client):
    csv = ("name,email,role,weekly_hours\n"
           "Ana Ruiz,ana@meridian.example,Forward Deployed Engineer,40\n"
           "Bo Lin,BO@meridian.example,engagement_manager,\n"
           "Cy Park,maya@meridian.example,fde,40\n"
           "Di Oh,di@meridian.example,Astronaut,40\n"
           "No Email,,fde,40\n"
           "Ed Wu,ed@meridian.example,,90\n"
           "Ana Again,ana@meridian.example,fde,40\n")
    r = client.post("/api/import", headers=H("head"), json={"kind": "people", "csv": csv}).json()
    assert r["created"] == 2 and len(r["errors"]) == 5
    people = {p["email"]: p for p in client.get("/api/people", headers=H("head")).json()}
    assert people["ana@meridian.example"]["role"] == "fde" and people["bo@meridian.example"]["role"] == "engagement_manager"
    assert people["bo@meridian.example"]["weekly_hours"] == 40
    assert client.post("/api/import", headers=H("fde"), json={"kind": "people", "csv": csv}).status_code == 403
    trail = client.get("/api/audit", headers=H("head")).json()
    assert any(a["action"] == "people.import" for a in trail)
