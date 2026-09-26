"""Delivery operations: delay ledger, flags and the sweep, capacity, pipeline,
approvals, checklist, value study and the portfolio view."""

from fieldwork import audit, ops, trackers
from fieldwork.engines import stages
from fieldwork.seed import DEMO_TOKENS, value_study_csv

from .conftest import H

SAM = {"Authorization": "Bearer demo-usr_sam-meridian"}


def delays(client, who="head", **q):
    qs = "&".join(f"{k}={v}" for k, v in q.items())
    return client.get(f"/api/delays?{qs}", headers=H(who)).json()


def by_key(client, key):
    return next(x for x in delays(client) if x["dedupe_key"] == key)


# ----------------------------------------------------------------- ledger

def test_blocking_a_task_opens_a_delay_and_unblocking_closes_it(client):
    assert client.patch("/api/tasks/tsk_001", headers=H("fde"), json={"status": "blocked"}).status_code == 200
    d = by_key(client, "task:tsk_001")
    assert d["open"] and d["status"] == "open" and d["signal"] == "task_blocked"
    # The workspace's history says blocked tasks here usually sit with the customer.
    assert d["proposed_owner"] == "customer" and "Proposed as customer" in d["proposal_basis"]
    client.patch("/api/tasks/tsk_001", headers=H("fde"), json={"status": "in_progress"})
    assert not by_key(client, "task:tsk_001")["open"]
    # Blocked again: a second span, not the first one stretched over the gap.
    client.patch("/api/tasks/tsk_001", headers=H("fde"), json={"status": "blocked"})
    spans = [x for x in delays(client) if x["dedupe_key"].startswith("task:tsk_001")]
    assert len(spans) == 2 and sum(1 for x in spans if x["open"]) == 1


def test_task_with_a_customer_assignee_proposes_the_customer(client):
    client.patch("/api/tasks/tsk_005", headers=H("em"), json={"status": "blocked"})
    d = by_key(client, "task:tsk_005")
    assert d["proposed_owner"] == "customer" and "customer side" in d["proposal_basis"]


def test_default_proposal_without_history(client):
    # Wipe the history that taught the workspace; the default for a blocked task is the team.
    with client.conn.tx():
        client.conn.execute("DELETE FROM delays WHERE signal='task_blocked'")
    client.patch("/api/tasks/tsk_001", headers=H("fde"), json={"status": "blocked"})
    d = by_key(client, "task:tsk_001")
    assert d["proposed_owner"] == "team" and d["owner_label"] == "Meridian AI"


def test_confirm_reassign_amend_and_weights(client):
    sid = by_key(client, "task:tsk_003")["id"]
    assert client.post(f"/api/delays/{sid}/decide", headers=H("fde"), json={}).status_code == 403
    r = client.post(f"/api/delays/{sid}/decide", headers=H("em"), json={"owner": "team", "reason": "our config"})
    assert r.status_code == 200 and r.json() == {"owner": "team", "status": "reassigned", "weight": 1.0,
                                                  "amended": False}
    n = len(delays(client))
    r = client.post(f"/api/delays/{sid}/decide", headers=H("em"), json={})  # changed my mind
    assert r.json()["status"] == "confirmed" and r.json()["amended"] is True
    assert len(delays(client)) == n
    assert client.post(f"/api/delays/{sid}/decide", headers=H("em"), json={"owner": "nobody"}).status_code == 422
    deduced = by_key(client, "manual_ca_rl")
    assert deduced["status"] == "deduced" and deduced["weight"] == 0
    assert client.post(f"/api/delays/{deduced['id']}/decide", headers=H("head"), json={}).status_code == 409
    actions = [a["action"] for a in client.get("/api/audit", headers=H("head")).json()]
    assert "delay.decide" in actions and "delay.amend" in actions


def test_batch_decisions_weigh_a_quarter(client):
    q = client.get("/api/delays/queue", headers=H("head")).json()
    assert q["groups"] and sum(g["count"] for g in q["groups"]) == len(q["items"])
    ids = [i["id"] for i in q["items"]] + [by_key(client, "manual_ca_rl")["id"]]
    r = client.post("/api/delays/batch", headers=H("head"), json={"span_ids": ids}).json()
    assert r == {"decided": len(ids) - 1, "skipped": 1, "weight_each": 0.25}
    assert client.get("/api/delays/queue", headers=H("head")).json()["items"] == []


def test_manual_delay_and_rollup(client):
    r = client.post("/api/deployments/dep_harborview/delays", headers=H("ai"),
                    json={"signal": "security_review", "evidence": "InfoSec questionnaire sent", "started_on": "2026-01-05"})
    assert r.status_code == 201 and r.json()["proposed_owner"] == "customer"
    assert client.post("/api/deployments/dep_harborview/delays", headers=H("ai"),
                       json={"signal": "stage_overrun"}).status_code == 422
    assert client.post("/api/deployments/dep_northfield/delays", headers=H("customer"), json={}).status_code == 403
    roll = client.get("/api/delays/rollup", headers=H("head")).json()
    owners = {o["owner"]: o for o in roll["owners"]}
    assert owners["customer"]["days"] > owners["team"]["days"] > 0
    assert roll["unconfirmed_rows"] >= 3
    assert any(p["signal"] == "task_blocked" and p["overrides_default"] for p in roll["priors"])


def test_advancing_closes_the_stage_overrun_delay(client):
    assert by_key(client, "dep_northfield:stage_overrun:adopt")["open"]
    client.post("/api/deployments/dep_northfield/advance", headers=H("em"), json={"to_stage": "value"})
    assert not by_key(client, "dep_northfield:stage_overrun:adopt")["open"]


def test_tracker_block_opens_a_tracker_delay(client):
    with client.conn.tx():
        client.conn.execute("INSERT INTO task_links (task_id, tenant_id, provider, external_id, url, synced_at)"
                            " VALUES ('tsk_001','ten_meridian','linear','LIN-1','',?)", (audit.now(),))
    cfg = client.get("/api/config", headers=H("head")).json()["config"]
    assert trackers.apply_inbound(client.conn, "ten_meridian", "linear", [("LIN-1", {"status": "blocked"})],
                                  cfg) == ["tsk_001"]
    d = by_key(client, "task:tsk_001")
    assert d["signal"] == "tracker_blocked" and "Linear" in d["evidence"]


def test_delays_are_internal_and_tenant_scoped(client):
    assert delays(client, "customer") == []
    assert client.get("/api/delays?deployment_id=dep_northfield", headers=H("customer")).json() == []
    other = {"Authorization": f"Bearer {DEMO_TOKENS['other_tenant']}"}
    assert client.get("/api/delays", headers=other).json() == []
    sid = by_key(client, "task:tsk_003")["id"]
    assert client.post(f"/api/delays/{sid}/decide", headers=other, json={}).status_code == 404
    assert client.get("/api/delays?deployment_id=dep_harborview", headers=H("fde")).status_code == 404


# ------------------------------------------------------------ flags, sweep

def test_sweep_is_idempotent_and_resolves_cleared_conditions(client):
    r = client.post("/api/sweep", headers=H("head")).json()
    assert r["flags_raised"] == 0 and r["delays_opened"] == 0
    flags = client.get("/api/flags?deployment_id=dep_northfield", headers=H("head")).json()
    assert any(f["rule_key"] == "blocked_task:tsk_003" for f in flags)
    client.patch("/api/tasks/tsk_003", headers=H("em"), json={"status": "in_progress"})
    r = client.post("/api/sweep", headers=H("head")).json()
    assert r["flags_resolved"] == 1
    flags = client.get("/api/flags?deployment_id=dep_northfield", headers=H("head")).json()
    assert not any(f["rule_key"] == "blocked_task:tsk_003" for f in flags)
    assert client.post("/api/sweep", headers=H("em")).status_code == 403


def test_seeded_rule_flags(client):
    keys = {f["rule_key"] for f in client.get("/api/flags", headers=H("head")).json()}
    assert {"stage_overrun:adopt", "stage_overrun:golive", "conformance_gate", "budget_burn"} <= keys
    assert not any(k and k.startswith("stage_overrun:value") for k in keys)  # last stage never overruns


def test_flag_lifecycle(client):
    r = client.post("/api/deployments/dep_northfield/flags", headers=H("fde"),
                    json={"text": "Customer IT out next week", "severity": "med"})
    assert r.status_code == 201
    fid = r.json()["id"]
    assert client.post(f"/api/flags/{fid}/take", headers=H("fde"), json={}).status_code == 403
    assert client.post(f"/api/flags/{fid}/take", headers=H("head"), json={}).json() == {"status": "owned"}
    assert client.post(f"/api/flags/{fid}/return", headers=H("head"), json={}).status_code == 422
    assert client.post(f"/api/flags/{fid}/return", headers=H("head"),
                       json={"note": "Line up cover with Rosa"}).json() == {"status": "returned"}
    assert client.post(f"/api/flags/{fid}/resolve", headers=H("em"), json={"note": "covered"}).json() == \
        {"status": "resolved"}
    assert client.post(f"/api/flags/{fid}/resolve", headers=H("em"), json={}).status_code == 409
    assert client.get("/api/flags", headers=H("customer")).json() == []
    assert client.post("/api/deployments/dep_northfield/flags", headers=H("fde"),
                       json={"text": "x", "severity": "urgent"}).status_code == 422


# ----------------------------------------------------------- portfolio

def test_portfolio_for_the_team(client):
    p = client.get("/api/portfolio", headers=H("head")).json()
    rows = {r["id"]: r for r in p["deployments"]}
    cas = rows["dep_castellan"]
    assert [c["state"] for c in cas["cells"]] == ["done", "done", "done", "stalled", "pending", "pending"]
    assert cas["overrun"] and cas["target_days"] == 7 and cas["status"] == "off_track"
    assert cas["conformance"]["status"] == "fail" and cas["flags_high"] >= 2
    assert cas["delay"]["days"] >= 12 and cas["delay"]["label"] == "Customer"
    assert rows["dep_harborview"]["burn"] > rows["dep_harborview"]["progress"]
    assert rows["dep_redline"]["cells"][0]["state"] == "progress"
    k = p["kpis"]
    assert k["active_deployments"] == 4 and k["contract_value"] == 1800000
    assert k["median_days_to_live"] is not None and k["days_to_live_target"] == 56
    assert k["utilization"] and 0.5 < k["utilization"] < 1.3
    med = {m["key"]: m for m in p["stage_medians"]}
    assert med["discover"]["n"] == 4 and med["discover"]["median_days"] is not None
    assert {o["owner"] for o in p["delays"]["owners"]} == set(ops.OWNERS)


def test_portfolio_for_the_customer_is_stripped(client):
    p = client.get("/api/portfolio", headers=H("customer")).json()
    assert [r["id"] for r in p["deployments"]] == ["dep_northfield"]
    row = p["deployments"][0]
    for k in ("delay", "burn", "arr", "flags", "conformance", "hours_spent", "status"):
        assert k not in row
    assert "kpis" not in p and "delays" not in p


# ------------------------------------------------------------- capacity

def test_team_capacity_and_unassigned_work(client):
    assert client.get("/api/team", headers=H("fde")).status_code == 403
    t = client.get("/api/team", headers=H("em")).json()
    people = {p["name"]: p for p in t["people"]}
    assert "Dana Whitfield" not in people and "Ruth Albrecht" not in people
    assert people["Sam Okoro"]["planned"] == 48 and people["Sam Okoro"]["over"] == 8
    assert people["Maya Chen"]["free"] == 0
    assert {u["title"] for u in t["unassigned"]} == {"Draft payer test matrix"}  # marcus isn't on Castellan
    assert all(u["you_can_assign"] for u in t["unassigned"])
    head = client.get("/api/team", headers=H("head")).json()
    assert {u["title"] for u in head["unassigned"]} == {"Hypercare rota for week two", "Draft payer test matrix"}
    assert any(r["user"] == "Maya Chen" and r["deployment_id"] == "dep_castellan" for r in head["rolloff"])
    r = client.patch("/api/tasks/tsk_011", headers=H("em"), json={"assignee_id": "usr_jordan"})
    assert r.status_code == 200
    assert client.get("/api/team", headers=H("em")).json()["unassigned"] == []


def test_unassigned_tasks_need_assign_rights(client):
    r = client.post("/api/tasks", headers=H("fde"),
                    json={"deployment_id": "dep_northfield", "title": "x", "unassigned": True})
    assert r.status_code == 403
    r = client.post("/api/tasks", headers=H("em"),
                    json={"deployment_id": "dep_northfield", "title": "Pick me up", "unassigned": True})
    assert r.status_code == 201
    t = next(x for x in client.get("/api/tasks?deployment_id=dep_northfield", headers=H("em")).json()
             if x["title"] == "Pick me up")
    assert t["assignee_id"] is None and t["assignee"] is None


def test_allocations_hours_and_time(client):
    assert client.put("/api/deployments/dep_redline/members/usr_sam", headers=H("fde"),
                      json={"allocation": .1}).status_code == 404  # maya can't see redline
    assert client.put("/api/deployments/dep_northfield/members/usr_sam", headers=H("em"),
                      json={"allocation": .2}).status_code == 200
    t = client.get("/api/team", headers=H("em")).json()
    assert next(p for p in t["people"] if p["name"] == "Sam Okoro")["planned"] == 36
    assert client.patch("/api/people/usr_sam", headers=H("head"), json={"weekly_hours": 32}).status_code == 200
    t = client.get("/api/team", headers=H("em")).json()
    assert next(p for p in t["people"] if p["name"] == "Sam Okoro")["available"] == 32
    wk = client.get("/api/me/week", headers=H("fde")).json()
    before = wk["logged"]
    assert client.post("/api/time", headers=H("fde"), json={"day": wk["week"], "hours": 2.5,
                                                            "deployment_id": "dep_northfield"}).status_code == 201
    assert client.get("/api/me/week", headers=H("fde")).json()["logged"] == round(before + 2.5, 1)
    assert client.post("/api/time", headers=H("ai"), json={"day": wk["week"], "hours": 1,
                                                           "deployment_id": "dep_northfield"}).status_code == 404
    assert client.post("/api/time", headers=H("fde"), json={"day": "soon", "hours": 1}).status_code == 422


def test_deployment_dates_and_budget(client):
    r = client.patch("/api/deployments/dep_redline", headers=H("em"),
                     json={"start_on": "2026-09-01", "end_on": "2026-12-01", "budget_hours": 400})
    assert r.status_code == 404  # marcus isn't on redline
    r = client.patch("/api/deployments/dep_redline", headers=H("head"),
                     json={"start_on": "2026-09-01", "end_on": "2026-12-01", "budget_hours": 400})
    assert set(r.json()["changed"]) == {"start_on", "end_on", "budget_hours"}
    assert client.patch("/api/deployments/dep_redline", headers=H("head"),
                        json={"end_on": "2026-08-01"}).status_code == 422
    assert client.patch("/api/deployments/dep_northfield", headers=H("fde"),
                        json={"budget_hours": 1}).status_code == 403


def test_time_import(client):
    csv = ("email,day,hours,deployment\n"
           "maya@meridian.example,2026-09-01,6,AP exception agent\n"
           "nobody@x.example,2026-09-01,6,\n"
           "sam@meridian.example,2026-09-01,30,\n")
    r = client.post("/api/import", headers=H("em"), json={"kind": "time", "csv": csv}).json()
    assert r["created"] == 1 and len(r["errors"]) == 2
    assert client.post("/api/import", headers=H("fde"), json={"kind": "time", "csv": csv}).status_code == 403


# ------------------------------------------------------------- pipeline

def test_pipeline_and_staffing_collisions(client):
    assert client.get("/api/pipeline", headers=H("fde")).status_code == 403
    p = client.get("/api/pipeline", headers=H("head")).json()
    assert [c["stage"] for c in p["columns"]] == ["lead", "qualified", "proposal", "commit"]
    commit = {d["name"]: d for d in p["columns"][3]["deals"]}
    assert commit["Northfield · Order-to-cash agent"]["staffing"]["ok"] is True
    assert commit["Cobalt Energy · Field ticket agent"]["staffing"]["ok"] is False
    assert [c["opportunity"] for c in p["collisions"]] == ["Cobalt Energy · Field ticket agent"]
    assert p["win_rate"] == 0.5 and p["weighted"] < p["total"]


def test_pipeline_import_upserts_and_edits(client):
    csv = ("name,customer,value,probability,stage,expected_start,weekly_hours,external_id\n"
           "Pinecrest Bank · KYC review copilot,Pinecrest Bank,\"$600,000\",40%,proposal,2026-12-01,80,crm-1004\n"
           "Nova Health · Scheduling agent,Nova Health,90000,0.1,lead,,20,crm-2001\n"
           "Bad,,1,1,someday,,,\n")
    r = client.post("/api/import", headers=H("em"), json={"kind": "pipeline", "csv": csv}).json()
    assert r["created"] == 2 and len(r["errors"]) == 1
    p = client.get("/api/pipeline", headers=H("em")).json()
    prop = {d["name"]: d for d in p["columns"][2]["deals"]}
    assert prop["Pinecrest Bank · KYC review copilot"]["value"] == 600000
    assert prop["Pinecrest Bank · KYC review copilot"]["probability"] == 0.4
    r = client.post("/api/opportunities", headers=H("em"), json={"name": "Juniper · Agent", "stage": "lead"})
    oid = r.json()["id"]
    assert client.patch(f"/api/opportunities/{oid}", headers=H("em"), json={"stage": "nope"}).status_code == 422
    assert client.patch(f"/api/opportunities/{oid}", headers=H("em"), json={"stage": "won"}).status_code == 200
    assert client.post("/api/opportunities", headers=H("fde"), json={"name": "x"}).status_code == 403


# ------------------------------------------------------------ approvals

def test_agent_approvals(client):
    r = client.post("/api/deployments/dep_harborview/approvals", headers=H("ai"),
                    json={"agent": "Intake agent", "request": "Send 3 test submissions to the payer sandbox"})
    aid = r.json()["id"]
    assert client.post(f"/api/approvals/{aid}/decide", headers=H("ai"), json={"approve": True}).status_code == 403
    pending = client.get("/api/approvals", headers=H("em")).json()
    assert aid in {a["id"] for a in pending} and all(a["you_can_decide"] for a in pending)
    assert client.post(f"/api/approvals/{aid}/decide", headers=H("em"), json={"approve": True}).json() == \
        {"status": "approved"}
    assert client.get(f"/api/approvals/{aid}", headers=H("ai")).json()["status"] == "approved"
    assert client.post(f"/api/approvals/{aid}/decide", headers=H("em"), json={"approve": False}).status_code == 409
    # The head asks; the head can't approve their own request.
    aid2 = client.post("/api/deployments/dep_castellan/approvals", headers=H("head"),
                       json={"agent": "x", "request": "y"}).json()["id"]
    assert client.post(f"/api/approvals/{aid2}/decide", headers=H("head"), json={"approve": True}).status_code == 403
    assert client.get("/api/approvals", headers=H("customer")).json() == []
    assert client.post("/api/deployments/dep_northfield/approvals", headers=H("customer"),
                       json={"agent": "x", "request": "y"}).status_code == 403


# ------------------------------------------------------------ checklist

def test_go_live_checklist(client):
    items = client.get("/api/deployments/dep_castellan/checklist", headers=H("fde")).json()
    assert len(items) == len(ops.CHECKLIST_DEFAULT) and sum(i["done"] for i in items) == 3
    todo = next(i for i in items if not i["done"])
    assert client.patch(f"/api/checklist/{todo['id']}", headers=H("fde"), json={"done": True}).status_code == 200
    items = client.get("/api/deployments/dep_castellan/checklist", headers=H("fde")).json()
    assert next(i for i in items if i["id"] == todo["id"])["done_by_name"] == "Maya Chen"
    r = client.post("/api/deployments/dep_northfield/checklist", headers=H("em"), json={"defaults": True})
    assert len(r.json()["ids"]) == len(ops.CHECKLIST_DEFAULT)
    assert client.post("/api/deployments/dep_northfield/checklist", headers=H("em"), json={}).status_code == 422
    assert client.get("/api/deployments/dep_northfield/checklist", headers=H("customer")).json() == []
    assert client.delete(f"/api/checklist/{todo['id']}", headers=H("customer")).status_code == 404


# ---------------------------------------------------------- value study

def test_value_study_engine():
    r = stages.value_study(value_study_csv())
    assert r["status"] == "pass" and r["result"]["sufficient"] and r["result"]["p_value"] < 0.05
    assert r["result"]["hours_saved_per_month"] > 0
    few = "\n".join(value_study_csv().splitlines()[:40])
    r = stages.value_study(few)
    assert r["result"]["sufficient"] is False and r["result"]["p_value"] is None
    assert "Cannot tell yet" in r["result"]["verdict"]
    same = stages.value_study(value_study_csv(drop=0.5))
    assert same["status"] != "pass" and same["result"]["sufficient"]


def test_value_study_through_the_api(client):
    r = client.post("/api/deployments/dep_northfield/engines/stage/value_study", headers=H("fde"),
                    json={"input": value_study_csv()})
    assert r.status_code == 200 and r.json()["result"]["status"] == "pass"
    r = client.post("/api/deployments/dep_northfield/engines/stage/value_study", headers=H("fde"),
                    json={"input": "arm,subject,day,value\ntreatment,a,2026-01-01,1"})
    assert r.status_code == 422 and "effective" in r.json()["detail"]


# --------------------------------------------------------------- config

def test_stage_targets_validate_and_upgrade(client):
    c = client.get("/api/config", headers=H("head")).json()["config"]
    c["stages"][0]["target_days"] = 0
    assert client.put("/api/config", headers=H("head"), json=c).status_code == 422
    c["stages"][0]["target_days"] = 10
    assert client.put("/api/config", headers=H("head"), json=c).status_code == 200
    from fieldwork import config
    old = config.default()
    for s in old["stages"]:
        s.pop("target_days")
    old["permissions"].pop("delay.confirm")
    up = config.upgrade(old)
    assert all(s["target_days"] for s in up["stages"])
    assert up["permissions"]["delay.confirm"] == up["permissions"]["finding.confirm"]


def test_mcp_ops_tools(client):
    tok = client.post("/api/me/tokens", headers=H("ai"), json={"name": "agent"}).json()["token"]
    hdr = {"Authorization": f"Bearer {tok}"}

    def call(name, args):
        return client.post("/mcp", headers=hdr, json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                                       "params": {"name": name, "arguments": args}}).json()["result"]
    names = {t["name"] for t in client.post("/mcp", headers=hdr, json={"jsonrpc": "2.0", "id": 1,
                                                                        "method": "tools/list"}).json()["result"]["tools"]}
    assert {"request_approval", "check_approval", "log_delay", "portfolio"} <= names
    r = call("request_approval", {"deployment_id": "dep_harborview", "agent": "Intake agent",
                                  "request": "Write to the Epic sandbox"})
    assert not r["isError"]
    import json
    aid = json.loads(r["content"][0]["text"])["id"]
    assert json.loads(call("check_approval", {"approval_id": aid})["content"][0]["text"])["status"] == "pending"
    r = call("log_delay", {"deployment_id": "dep_harborview", "signal": "model_access"})
    assert json.loads(r["content"][0]["text"])["status"] == "deduced"


def test_coverage_summary(client):
    assert client.get("/api/coverage", headers=H("em")).status_code == 403
    c = client.get("/api/coverage", headers=H("head")).json()
    assert c["deployments"] == 5 and c["opportunities"] == 7 and c["time_entries_30d"] > 0
    assert c["slack"] == {"enabled": False, "connected": False} and c["delays_confirmed"] >= 10


# ------------------------------------------------ review regressions

def test_customers_get_no_delay_attribution_or_time(client):
    r = client.get("/api/delays/rollup", headers=H("customer")).json()
    assert r["priors"] == [] and r["confirmed_rows"] == 0
    assert client.post("/api/time", headers=H("customer"),
                       json={"day": "2026-09-20", "hours": 24, "deployment_id": "dep_northfield"}).status_code == 403
    assert client.post("/api/time", headers=H("customer"), json={"day": "2026-09-20", "hours": 2}).status_code == 403


def test_time_import_only_lands_on_deployments_you_run(client):
    csv = "email,day,hours,deployment\nrosa@meridian.example,2026-09-21,24,Carrier dispute pilot\n" \
          "rosa@meridian.example,2026-09-21,2,\n"
    r = client.post("/api/import", headers=H("em"), json={"kind": "time", "csv": csv}).json()
    assert r["created"] == 0 and len(r["errors"]) == 2
    r = client.post("/api/import", headers=H("head"), json={"kind": "time", "csv": csv}).json()
    assert r["created"] == 2


def test_own_scope_sees_no_names_of_other_deployments(client):
    p = client.get("/api/pipeline", headers=H("em")).json()
    assert all(x["deployment_id"] in ("dep_northfield", "dep_harborview", "dep_keystone") for x in p["rolloff"])
    t = client.get("/api/team", headers=H("em")).json()
    names = {a["deployment"] for person in t["people"] for a in person["allocations"]}
    assert "Claims triage copilot" not in names and "Another deployment" in names
    med = {m["key"]: m for m in client.get("/api/portfolio", headers=H("fde")).json()["stage_medians"]}
    assert med["discover"]["n"] == 2


def test_automatic_delays_are_audited(client):
    client.patch("/api/tasks/tsk_001", headers=H("fde"), json={"status": "blocked"})
    sid = by_key(client, "task:tsk_001")["id"]
    rows = [a for a in client.get("/api/audit?limit=50", headers=H("head")).json() if a["subject"] == sid]
    assert rows and rows[0]["action"] == "delay.open" and rows[0]["actor"] == "rule:task_blocked"


def test_bad_input_is_a_422_not_a_500(client):
    r = client.post("/api/deployments/dep_northfield/delays", headers=H("em"), json={"started_on": "yesterday"})
    assert r.status_code == 422
    csv = ("name,value,probability,weekly_hours\nA,nan,0.1,10\nB,10,inf,10\nC,1e400,0.1,10\n"
           "D,100,0.2,10\nE,100,0.2,10\n")
    r = client.post("/api/import", headers=H("em"), json={"kind": "pipeline", "csv": csv}).json()
    assert r["created"] == 2 and len(r["errors"]) == 3
    assert client.get("/api/pipeline", headers=H("em")).status_code == 200
    r = client.patch("/api/people/usr_rosa", headers=H("head"), json={"weekly_hours": 5, "role": "bogus"})
    assert r.status_code == 422
    rosa = next(p for p in client.get("/api/people", headers=H("head")).json() if p["id"] == "usr_rosa")
    assert rosa["weekly_hours"] == 40


def test_customer_cant_probe_checklist_items(client):
    assert client.patch("/api/checklist/chk_ca1", headers=H("customer"), json={"done": True}).status_code == 404
