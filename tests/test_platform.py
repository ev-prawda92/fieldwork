"""The guarantees a buyer's security review will ask about, as tests."""

from fieldwork.seed import northfield_adoption_csv

from .conftest import H


def cfg(client):
    return client.get("/api/config", headers=H("head")).json()["config"]


# ------------------------------------------------------------ auth & tenancy

def test_requires_token(client):
    assert client.get("/api/me").status_code == 401
    assert client.get("/api/me", headers={"Authorization": "Bearer nope"}).status_code == 401


def test_tenant_isolation(client):
    other = H("other_tenant")
    assert {d["id"] for d in client.get("/api/deployments", headers=other).json()} == {"dep_orb1"}
    assert client.get("/api/deployments/dep_northfield", headers=other).status_code == 404
    assert client.post("/api/deployments/dep_northfield/advance", headers=other,
                       json={"to_stage": "value"}).status_code == 404
    assert client.get("/api/deployments/dep_orb1", headers=H("head")).status_code == 404
    r = client.post("/api/tasks", headers=H("head"),
                    json={"deployment_id": "dep_northfield", "title": "x", "assignee_id": "usr_orb"})
    assert r.status_code == 422


# ------------------------------------------------------- roles and scopes

def test_own_scope_sees_only_staffed_deployments(client):
    assert {d["id"] for d in client.get("/api/deployments", headers=H("fde")).json()} == \
        {"dep_northfield", "dep_castellan"}
    assert client.get("/api/deployments/dep_harborview", headers=H("fde")).status_code == 404
    assert len(client.get("/api/deployments", headers=H("head")).json()) == 5


def test_engagement_manager_powers_are_scoped_to_own_engagements(client):
    # Marcus runs Northfield and Harborview, not Castellan.
    ok = client.post("/api/tasks", headers=H("em"),
                     json={"deployment_id": "dep_northfield", "title": "x", "assignee_id": "usr_rosa"})
    assert ok.status_code == 201
    assert client.get("/api/deployments/dep_castellan", headers=H("em")).status_code == 404
    # Can jump stages on his own engagement.
    r = client.post("/api/deployments/dep_harborview/advance", headers=H("em"),
                    json={"to_stage": "golive"})
    assert r.status_code == 200


def test_doers_advance_one_stage_only(client):
    r = client.post("/api/deployments/dep_northfield/advance", headers=H("fde"),
                    json={"to_stage": "discover", "note": "redo"})
    assert r.status_code == 403
    assert client.post("/api/deployments/dep_northfield/advance", headers=H("fde"),
                       json={"to_stage": "value"}).status_code == 200


def test_doers_cannot_assign_or_update_others_tasks(client):
    r = client.post("/api/tasks", headers=H("fde"),
                    json={"deployment_id": "dep_northfield", "title": "x", "assignee_id": "usr_sam"})
    assert r.status_code == 403
    assert client.patch("/api/tasks/tsk_003", headers=H("fde"), json={"status": "done"}).status_code == 403
    assert client.patch("/api/tasks/tsk_001", headers=H("fde"), json={"status": "done"}).status_code == 200
    assert client.get("/api/people", headers=H("fde")).status_code == 403


def test_customer_stakeholder_is_read_only(client):
    assert {d["id"] for d in client.get("/api/deployments", headers=H("customer")).json()} == {"dep_northfield"}
    assert client.post("/api/deployments/dep_northfield/advance", headers=H("customer"),
                       json={"to_stage": "value"}).status_code == 403
    assert client.post("/api/tasks", headers=H("customer"),
                       json={"deployment_id": "dep_northfield", "title": "x"}).status_code == 403
    assert client.get("/api/audit", headers=H("customer")).status_code == 403


def test_backward_move_needs_note(client):
    r = client.post("/api/deployments/dep_castellan/advance", headers=H("head"), json={"to_stage": "integrate"})
    assert r.status_code == 422
    r = client.post("/api/deployments/dep_castellan/advance", headers=H("head"),
                    json={"to_stage": "integrate", "note": "security review reopened scopes"})
    assert r.status_code == 200


# ---------------------------------------------------- customization

def test_custom_roles_and_permissions(client):
    c = cfg(client)
    c["roles"].append({"key": "solutions_architect", "name": "Solutions Architect"})
    c["permissions"]["deployment.view"]["solutions_architect"] = "all"
    c["permissions"]["people.read"]["fde"] = "all"
    assert client.put("/api/config", headers=H("head"), json=c).status_code == 200
    assert client.get("/api/people", headers=H("fde")).status_code == 200
    r = client.post("/api/people", headers=H("head"),
                    json={"name": "Ari Stone", "email": "ari@meridian.example", "role": "solutions_architect"})
    assert r.status_code == 201
    sa = {"Authorization": f"Bearer {r.json()['token']}"}
    assert len(client.get("/api/deployments", headers=sa).json()) == 5
    me = client.get("/api/me", headers=sa).json()
    assert me["user"]["role_name"] == "Solutions Architect"


def test_cannot_delete_role_in_use_or_lock_out_settings(client):
    c = cfg(client)
    c["roles"] = [r for r in c["roles"] if r["key"] != "fde"]
    for perms in c["permissions"].values():
        perms.pop("fde", None)
    c["views"].pop("fde")
    assert client.put("/api/config", headers=H("head"), json=c).status_code == 409
    c = cfg(client)
    c["permissions"]["config.edit"] = {"engagement_manager": "all"}
    assert client.put("/api/config", headers=H("head"), json=c).status_code == 422
    c["permissions"]["config.edit"] = {}
    assert client.put("/api/config", headers=H("head"), json=c).status_code == 422
    assert client.put("/api/config", headers=H("em"), json=cfg(client)).status_code == 403


def test_workspace_actions_must_be_workspace_scoped(client):
    c = cfg(client)
    c["permissions"]["audit.read"]["fde"] = "own"
    r = client.put("/api/config", headers=H("head"), json=c)
    assert r.status_code == 422 and "workspace-wide" in r.json()["detail"]


def test_white_label(client):
    me = client.get("/api/me", headers=H("fde")).json()
    assert me["branding"]["product_name"] == "Meridian Deploy"
    c = cfg(client)
    c["branding"] = {"product_name": "Castle", "accent": "red", "logo_url": ""}
    assert client.put("/api/config", headers=H("head"), json=c).status_code == 422
    c["branding"] = {"product_name": "Castle", "accent": "#123abc", "logo_url": "javascript:alert(1)"}
    assert client.put("/api/config", headers=H("head"), json=c).status_code == 422
    c["branding"]["logo_url"] = "https://cdn.example.com/logo.svg"
    assert client.put("/api/config", headers=H("head"), json=c).status_code == 200


def test_views_per_role(client):
    d = client.get("/api/dashboard", headers=H("customer")).json()
    assert d["widgets"] == ["clients", "my_tasks"] and "kpis" not in d and "team" not in d
    d = client.get("/api/dashboard", headers=H("head")).json()
    assert {"kpis", "chain", "findings"} <= set(d) and "capacity" in d["widgets"]


def test_custom_stages_and_fields(client):
    c = cfg(client)
    c["stages"].insert(3, {"key": "security", "name": "Security review", "engine": "none"})
    c["fields"]["deployment"].append({"key": "region", "label": "Region", "type": "select",
                                      "options": ["NA", "EMEA"]})
    assert client.put("/api/config", headers=H("head"), json=c).status_code == 200
    assert client.patch("/api/deployments/dep_northfield", headers=H("em"),
                        json={"fields": {"region": "APAC"}}).status_code == 422
    assert client.patch("/api/deployments/dep_northfield", headers=H("em"),
                        json={"fields": {"region": "EMEA"}}).status_code == 200


def test_cannot_delete_stage_with_live_deployments(client):
    c = cfg(client)
    c["stages"] = [s for s in c["stages"] if s["key"] != "adopt"]
    assert client.put("/api/config", headers=H("head"), json=c).status_code == 409


# ---------------------------------------------------------- built-in engines

def test_sendero_classifies_site_config_as_build(client):
    r = client.post("/api/deployments/dep_northfield/engines/sendero", headers=H("fde"),
                    json={"title": "Invoice exceptions", "csv": northfield_adoption_csv(),
                          "metric": "minutes_per_exception", "baseline": 6})
    assert r.status_code == 200
    res = r.json()["result"]
    assert res["classification"] == "BUILD"
    assert res["cohorts"] == {"capability": ["tenure_months"], "config": ["site"]}


def test_threshold_ranks_bench_and_cites(client):
    r = client.post("/api/deployments/dep_northfield/engines/threshold", headers=H("em"))
    assert r.status_code == 200
    ranked = r.json()["result"]["ranked"]
    assert ranked[-1]["verdict"] == "blocked"
    maya = next(p for p in ranked if p["name"] == "Maya Chen")
    netsuite = next(f for f in maya["findings"] if "NetSuite" in f["requirement"])
    assert netsuite["status"] == "met" and netsuite["citations"]
    travel = next(f for f in maya["findings"] if "travel" in f["requirement"])
    assert travel["status"] == "unknown"  # never inferred
    assert client.post("/api/deployments/dep_northfield/engines/threshold", headers=H("fde")).status_code == 403


def test_finding_confirmed_by_second_person(client):
    fid = client.post("/api/deployments/dep_northfield/engines/threshold", headers=H("em")).json()["finding_id"]
    assert client.post(f"/api/findings/{fid}/confirm", headers=H("em")).status_code == 403
    assert client.post(f"/api/findings/{fid}/confirm", headers=H("head")).status_code == 200
    assert client.post(f"/api/findings/{fid}/confirm", headers=H("head")).status_code == 409


# ------------------------------------------------------------ audit

def test_every_write_is_audited_and_chain_verifies(client):
    before = client.get("/api/audit/verify", headers=H("head")).json()["entries"]
    client.post("/api/deployments/dep_northfield/advance", headers=H("fde"), json={"to_stage": "value"})
    client.patch("/api/tasks/tsk_001", headers=H("fde"), json={"status": "done"})
    v = client.get("/api/audit/verify", headers=H("head")).json()
    assert v["ok"] and v["entries"] == before + 2


def test_tampering_is_detected(client):
    client.post("/api/deployments/dep_northfield/advance", headers=H("fde"), json={"to_stage": "value"})
    with client.conn.tx():
        client.conn.execute("UPDATE audit SET detail_json=? WHERE action='deployment.advance'",
                            ('{"to":"discover"}',))
    v = client.get("/api/audit/verify", headers=H("head")).json()
    assert v["ok"] is False and v["broken_at"] is not None


def test_audit_chains_are_per_tenant(client):
    v = client.get("/api/audit/verify", headers=H("other_tenant")).json()
    assert v["ok"] and v["entries"] == 0
