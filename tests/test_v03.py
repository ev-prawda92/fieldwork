"""v0.3: customer-safe views, encryption at rest, migrations, stage engines."""

import json
import sqlite3

import pytest
from cryptography.fernet import Fernet

from fieldwork import config, crypto, db
from fieldwork.engines import stages
from fieldwork.seed import SAMPLE_INPUTS

from .conftest import H


# ------------------------------------------------------ customer-safe views

def test_customer_sees_only_shared_tasks(client):
    ids = {t["id"] for t in client.get("/api/tasks?deployment_id=dep_northfield", headers=H("customer")).json()}
    assert ids == {"tsk_002", "tsk_004", "tsk_005", "tsk_nf_plan", "tsk_nf_train"}
    team = {t["id"] for t in client.get("/api/tasks?deployment_id=dep_northfield", headers=H("fde")).json()}
    assert {"tsk_001", "tsk_003"} <= team
    d = client.get("/api/deployments/dep_northfield", headers=H("customer")).json()
    assert sum(d["tasks"].values()) == 5 and d["staffing_req"] == ""


def test_customer_can_work_their_own_shared_task_but_not_internal_ones(client):
    assert client.patch("/api/tasks/tsk_005", headers=H("customer"), json={"status": "done"}).status_code == 200
    assert client.patch("/api/tasks/tsk_001", headers=H("customer"), json={"status": "done"}).status_code == 404


def test_task_for_a_customer_is_shared_automatically(client):
    r = client.post("/api/tasks", headers=H("em"), json={
        "deployment_id": "dep_northfield", "title": "Confirm exception categories", "assignee_id": "usr_ruth"})
    assert r.status_code == 201 and r.json()["visibility"] == "shared"
    # Someone without customer.share can't make shared tasks.
    r = client.post("/api/tasks", headers=H("fde"), json={
        "deployment_id": "dep_northfield", "title": "x", "visibility": "shared"})
    assert r.status_code == 403
    # Can't pull a customer's task back to internal.
    tid = client.post("/api/tasks", headers=H("em"), json={
        "deployment_id": "dep_northfield", "title": "y", "assignee_id": "usr_ruth"}).json()["id"]
    assert client.patch(f"/api/tasks/{tid}", headers=H("em"), json={"visibility": "internal"}).status_code == 422


def test_findings_are_internal_until_confirmed_and_shared(client):
    shared = [f["id"] for f in client.get("/api/deployments/dep_northfield/findings", headers=H("customer")).json()]
    assert shared == ["fnd_value1"]
    fid = client.post("/api/deployments/dep_northfield/engines/stage/attribution", headers=H("fde"),
                      json={"input": SAMPLE_INPUTS["attribution"]}).json()["finding_id"]
    assert client.post(f"/api/findings/{fid}/share", headers=H("em"), json={"shared": True}).status_code == 409
    assert client.post(f"/api/findings/{fid}/confirm", headers=H("em")).status_code == 200
    assert client.post(f"/api/findings/{fid}/share", headers=H("fde"), json={"shared": True}).status_code == 403
    assert client.post(f"/api/findings/{fid}/share", headers=H("em"), json={"shared": True}).status_code == 200
    seen = {f["id"] for f in client.get("/api/deployments/dep_northfield/findings", headers=H("customer")).json()}
    assert fid in seen
    assert client.post(f"/api/findings/{fid}/confirm", headers=H("customer")).status_code == 403


# ------------------------------------------------------ encryption at rest

def test_webhook_secrets_are_encrypted_at_rest(client):
    row = client.conn.execute("SELECT secret FROM engine_credentials WHERE engine_key='readiness'").fetchone()
    assert row["secret"].startswith(crypto.PREFIX)
    assert "fws_demo" not in row["secret"]
    assert crypto.decrypt(row["secret"]) == "fws_demo_readiness_signing_secret"


def test_key_rotation(client, monkeypatch):
    old = open(crypto.os.environ["FIELDWORK_KEY_FILE"], "rb").read().decode().strip()
    new = Fernet.generate_key().decode()
    monkeypatch.setenv("FIELDWORK_SECRET_KEYS", f"{new},{old}")
    assert crypto.rotate_all(client.conn) >= 1
    monkeypatch.setenv("FIELDWORK_SECRET_KEYS", new)  # old key retired
    row = client.conn.execute("SELECT secret FROM engine_credentials WHERE engine_key='readiness'").fetchone()
    assert crypto.decrypt(row["secret"]) == "fws_demo_readiness_signing_secret"
    monkeypatch.setenv("FIELDWORK_SECRET_KEYS", Fernet.generate_key().decode())
    with pytest.raises(crypto.SecretError):
        crypto.decrypt(row["secret"])


# --------------------------------------------------------------- migrations

def test_upgrades_a_v02_database_in_place(tmp_path):
    path = str(tmp_path / "old.db")
    raw = sqlite3.connect(path)
    raw.executescript(db.MIGRATIONS[0][2].replace("{AUTO}", "INTEGER PRIMARY KEY AUTOINCREMENT"))
    old_cfg = config.default()
    del old_cfg["permissions"]["task.view_internal"]
    for s in old_cfg["stages"]:
        s["engine"] = s.pop("engines")[0]
    raw.execute("INSERT INTO tenants VALUES ('t1','Old Co',?, '2026-01-01')", (json.dumps(old_cfg),))
    raw.execute("INSERT INTO users VALUES ('u1','t1','A','a@x','engagement_manager',NULL,'h','{}','x')")
    raw.commit()
    raw.close()
    conn = db.connect(path)
    assert db.migrate(conn) == [m[0] for m in db.MIGRATIONS]
    assert db.migrate(conn) == []
    assert conn.execute("SELECT name FROM users WHERE id='u1'").fetchone()["name"] == "A"
    cfg = config.upgrade(json.loads(conn.execute("SELECT config_json FROM tenants").fetchone()["config_json"]))
    # New permission inherited from the closest existing one, so nobody loses access.
    assert cfg["permissions"]["task.view_internal"]["engagement_manager"] == "own"
    assert cfg["stages"][0]["engines"] == ["census"]
    config.validate(cfg)


# ------------------------------------------------------------ stage engines

def test_census_flags_blockers_and_writes_memo():
    r = stages.census(SAMPLE_INPUTS["census"])
    assert r["status"] == "fail"
    bad = {s["system"] for s in r["result"]["systems"] if s["severity"] == "blocker"}
    assert "Rate database" in bad and "Claims archive (SharePoint)" in bad
    assert "## Discovery findings" in r["result"]["memo"]


def test_cortex_authority_runs_real_cortex_and_flags_risky_grants():
    r = stages.authority(SAMPLE_INPUTS["cortex"])
    assert all(s["ok"] for s in r["result"]["scenarios"])
    assert r["status"] == "warn"
    assert any("no human review gate" in x for x in r["result"]["risks"])
    broken = json.loads(SAMPLE_INPUTS["cortex"])
    broken["scenarios"][0]["expect"] = "ALLOW"
    assert stages.authority(json.dumps(broken))["status"] == "fail"


def test_conformance_fails_on_critical_miss():
    r = stages.conformance(SAMPLE_INPUTS["conformance"])
    assert r["status"] == "fail" and r["result"]["critical_failures"] == ["F-03"]
    assert r["result"]["by_category"]["extraction"]["rate"] == 1.0  # 12999.99 vs 13000 within 1%


def test_command_center_stand_down_logic():
    assert stages.command_center(SAMPLE_INPUTS["golive"])["status"] == "pass"
    hot = SAMPLE_INPUTS["golive"] + "\nINC-110,sev1,open,2026-10-21T19:00,,routing"
    r = stages.command_center(hot)
    assert r["status"] == "fail" and r["result"]["open_by_severity"]["sev1"] == 1


def test_attribution_values_and_attributes_delay():
    r = stages.attribution(SAMPLE_INPUTS["attribution"])
    v = r["result"]["value"]
    assert v["hours_saved_per_month"] == pytest.approx(329.3, abs=0.1)
    assert r["result"]["delay_days_by_cause"] == {"customer": 9, "vendor": 4, "third_party": 3}
    assert r["result"]["unattributed_days"] == 2 and r["status"] == "warn"


def test_stage_engines_run_through_the_api(client):
    for key in stages.STAGE_ENGINES:
        r = client.post("/api/deployments/dep_northfield/engines/stage/" + key, headers=H("fde"),
                        json={"input": SAMPLE_INPUTS[key]})
        assert r.status_code == 200, (key, r.text)
    r = client.post("/api/deployments/dep_northfield/engines/stage/census", headers=H("fde"),
                    json={"input": "system,owner\nx,y"})
    assert r.status_code == 422 and "missing column" in r.json()["detail"]
    assert client.post("/api/deployments/dep_northfield/engines/stage/census", headers=H("customer"),
                       json={"input": SAMPLE_INPUTS["census"]}).status_code == 403


def test_stage_can_carry_several_engines(client):
    cfg = client.get("/api/config", headers=H("head")).json()["config"]
    golive = next(s for s in cfg["stages"] if s["key"] == "golive")
    assert golive["engines"] == ["golive", "readiness"]
    golive["engines"] = ["golive", "readiness", "conformance", "census", "cortex"]
    assert client.put("/api/config", headers=H("head"), json=cfg).status_code == 422
