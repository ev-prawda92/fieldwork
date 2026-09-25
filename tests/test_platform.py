"""The guarantees a buyer's security review will ask about, as tests."""

import sqlite3

import pytest
from fastapi.testclient import TestClient

from fieldwork.app import create_app
from fieldwork.seed import DEMO_TOKENS, northfield_adoption_csv, seed


@pytest.fixture()
def client(tmp_path):
    path = str(tmp_path / "fw.db")
    seed(path)
    app = create_app(path)
    c = TestClient(app)
    c.db_path = path
    return c


def H(who):
    return {"Authorization": f"Bearer {DEMO_TOKENS[who]}"}


# ------------------------------------------------------------ auth & tenancy

def test_requires_token(client):
    assert client.get("/api/me").status_code == 401
    assert client.get("/api/me", headers={"Authorization": "Bearer nope"}).status_code == 401


def test_tenant_isolation(client):
    other = H("other_tenant")
    ids = {d["id"] for d in client.get("/api/deployments", headers=other).json()}
    assert ids == {"dep_orb1"}
    # Can't read, advance, or run engines on another tenant's deployment.
    assert client.get("/api/deployments/dep_northfield", headers=other).status_code == 404
    assert client.post("/api/deployments/dep_northfield/advance", headers=other,
                       json={"to_stage": "value"}).status_code == 404
    assert client.get("/api/deployments/dep_orb1", headers=H("director")).status_code == 404
    # Can't assign work to someone in another tenant.
    r = client.post("/api/tasks", headers=H("manager"),
                    json={"deployment_id": "dep_northfield", "title": "x", "assignee_id": "usr_orb"})
    assert r.status_code == 422


# ----------------------------------------------------------------- roles

def test_fde_sees_only_assigned_deployments(client):
    mine = {d["id"] for d in client.get("/api/deployments", headers=H("fde")).json()}
    assert mine == {"dep_northfield"}
    assert client.get("/api/deployments/dep_harborview", headers=H("fde")).status_code == 404
    everything = client.get("/api/deployments", headers=H("manager")).json()
    assert len(everything) == 4


def test_fde_cannot_assign_or_read_people(client):
    assert client.get("/api/people", headers=H("fde")).status_code == 403
    r = client.post("/api/tasks", headers=H("fde"),
                    json={"deployment_id": "dep_northfield", "title": "x", "assignee_id": "usr_sam"})
    assert r.status_code == 403
    r = client.post("/api/tasks", headers=H("fde"),
                    json={"deployment_id": "dep_northfield", "title": "my own task"})
    assert r.status_code == 201


def test_fde_advances_one_stage_only(client):
    r = client.post("/api/deployments/dep_northfield/advance", headers=H("fde"),
                    json={"to_stage": "discover", "note": "redo"})
    assert r.status_code == 403
    r = client.post("/api/deployments/dep_northfield/advance", headers=H("fde"),
                    json={"to_stage": "value"})
    assert r.status_code == 200


def test_backward_move_needs_note(client):
    r = client.post("/api/deployments/dep_castellan/advance", headers=H("manager"),
                    json={"to_stage": "integrate"})
    assert r.status_code == 422
    r = client.post("/api/deployments/dep_castellan/advance", headers=H("manager"),
                    json={"to_stage": "integrate", "note": "security review reopened scopes"})
    assert r.status_code == 200


def test_permissions_are_customer_configurable(client):
    cfg = client.get("/api/config", headers=H("director")).json()["config"]
    cfg["permissions"]["people.read"] = ["fde", "manager", "director"]
    assert client.put("/api/config", headers=H("director"), json=cfg).status_code == 200
    assert client.get("/api/people", headers=H("fde")).status_code == 200


def test_config_cannot_lock_out_directors(client):
    cfg = client.get("/api/config", headers=H("director")).json()["config"]
    cfg["permissions"]["config.edit"] = ["manager"]
    assert client.put("/api/config", headers=H("director"), json=cfg).status_code == 422
    assert client.put("/api/config", headers=H("manager"), json=cfg).status_code == 403


def test_custom_stages_and_fields(client):
    cfg = client.get("/api/config", headers=H("director")).json()["config"]
    cfg["stages"].insert(3, {"key": "security", "name": "Security review", "engine": "none"})
    cfg["fields"]["deployment"].append({"key": "region", "label": "Region", "type": "select",
                                        "options": ["NA", "EMEA"]})
    assert client.put("/api/config", headers=H("director"), json=cfg).status_code == 200
    r = client.patch("/api/deployments/dep_castellan", headers=H("manager"), json={"fields": {"region": "APAC"}})
    assert r.status_code == 422
    r = client.patch("/api/deployments/dep_castellan", headers=H("manager"), json={"fields": {"region": "EMEA"}})
    assert r.status_code == 200
    r = client.post("/api/deployments/dep_castellan/advance", headers=H("manager"),
                    json={"to_stage": "security"})
    assert r.status_code == 200


def test_cannot_delete_stage_with_live_deployments(client):
    cfg = client.get("/api/config", headers=H("director")).json()["config"]
    cfg["stages"] = [s for s in cfg["stages"] if s["key"] != "adopt"]
    assert client.put("/api/config", headers=H("director"), json=cfg).status_code == 409


# ---------------------------------------------------------------- engines

def test_sendero_classifies_site_config_as_build(client):
    r = client.post("/api/deployments/dep_northfield/engines/sendero", headers=H("fde"),
                    json={"title": "Invoice exceptions", "csv": northfield_adoption_csv(),
                          "metric": "minutes_per_exception", "baseline": 6})
    assert r.status_code == 200
    res = r.json()["result"]
    assert res["classification"] == "BUILD"
    assert res["cohorts"] == {"capability": ["tenure_months"], "config": ["site"]}


def test_sendero_rejects_bad_metric(client):
    r = client.post("/api/deployments/dep_northfield/engines/sendero", headers=H("fde"),
                    json={"title": "x", "csv": northfield_adoption_csv(), "metric": "nope"})
    assert r.status_code == 422 and "nope" in r.json()["detail"]


def test_threshold_ranks_bench_and_cites(client):
    r = client.post("/api/deployments/dep_northfield/engines/threshold", headers=H("manager"))
    assert r.status_code == 200
    ranked = r.json()["result"]["ranked"]
    assert ranked[-1]["name"] == "Sam Okoro" and ranked[-1]["verdict"] == "blocked"
    maya = next(p for p in ranked if p["name"] == "Maya Chen")
    netsuite = next(f for f in maya["findings"] if "NetSuite" in f["requirement"])
    assert netsuite["status"] == "met" and netsuite["citations"]
    travel = next(f for f in maya["findings"] if "travel" in f["requirement"])
    assert travel["status"] == "unknown"  # never inferred


def test_fde_cannot_run_bench(client):
    r = client.post("/api/deployments/dep_northfield/engines/threshold", headers=H("fde"))
    assert r.status_code == 403


def test_finding_confirmed_by_second_person(client):
    r = client.post("/api/deployments/dep_northfield/engines/threshold", headers=H("manager"))
    fid = r.json()["finding_id"]
    assert client.post(f"/api/findings/{fid}/confirm", headers=H("manager")).status_code == 403
    assert client.post(f"/api/findings/{fid}/confirm", headers=H("director")).status_code == 200
    assert client.post(f"/api/findings/{fid}/confirm", headers=H("director")).status_code == 409


# ------------------------------------------------------------------ audit

def test_every_write_is_audited_and_chain_verifies(client):
    before = client.get("/api/audit/verify", headers=H("director")).json()["entries"]
    client.post("/api/deployments/dep_northfield/advance", headers=H("fde"), json={"to_stage": "value"})
    client.patch("/api/tasks/tsk_001", headers=H("fde"), json={"status": "done"})
    v = client.get("/api/audit/verify", headers=H("director")).json()
    assert v["ok"] and v["entries"] == before + 2
    actions = [e["action"] for e in client.get("/api/audit", headers=H("director")).json()[:2]]
    assert actions == ["task.update", "deployment.advance"]


def test_tampering_is_detected(client):
    client.post("/api/deployments/dep_northfield/advance", headers=H("fde"), json={"to_stage": "value"})
    raw = sqlite3.connect(client.db_path)
    raw.execute("UPDATE audit SET detail_json='{\"to\":\"discover\"}' "
                "WHERE action='deployment.advance'")
    raw.commit()
    v = client.get("/api/audit/verify", headers=H("director")).json()
    assert v["ok"] is False and v["broken_at"] is not None


def test_audit_chains_are_per_tenant(client):
    v = client.get("/api/audit/verify", headers=H("other_tenant")).json()
    assert v == {"ok": True, "entries": 0, "broken_at": None, "head": v["head"]}
