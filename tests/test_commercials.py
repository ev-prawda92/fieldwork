"""Statements of work, billable milestones, the customer's sign-off, and the ERP billing bridge.

The demo has Northfield's SOW set up: discovery and integration paid, UAT invoiced, go-live signed off,
the adoption plan (a hand-marked milestone) waiting on Ruth, and the value readout tied to the Value stage.
"""

import json
from urllib.parse import parse_qs, urlparse

import pytest

from fieldwork import events
from fieldwork.connect import core, http

from .conftest import H
from .test_connect import FakeWeb, _env, install, web  # noqa: F401  (fixtures)


def ms(client, mid):
    return client.conn.execute("SELECT * FROM milestones WHERE id=?", (mid,)).fetchone()


def commercials(client, who, dep="dep_northfield"):
    r = client.get(f"/api/deployments/{dep}/commercials", headers=H(who))
    assert r.status_code == 200, r.text
    return r.json()


def all_ms(cm):
    return {m["id"]: m for s in cm["sows"] for m in s["milestones"]}


def trail(client, action):
    return [a for a in client.get("/api/audit?limit=500", headers=H("head")).json() if a["action"] == action]


# ------------------------------------------------------------ stage → ready

def test_finishing_a_stage_makes_its_milestone_ready_and_reopening_reverts_it(client):
    assert ms(client, "ms_nf1_6")["status"] == "pending"
    r = client.put("/api/deployments/dep_northfield/stages/value", headers=H("em"), json={"state": "done"})
    assert r.status_code == 200, r.text
    assert ms(client, "ms_nf1_6")["status"] == "ready"
    assert trail(client, "milestone.ready")[0]["subject"] == "ms_nf1_6"
    client.put("/api/deployments/dep_northfield/stages/value", headers=H("em"), json={"state": "in_progress"})
    assert ms(client, "ms_nf1_6")["status"] == "pending"
    # a milestone already submitted doesn't move back when its stage reopens
    client.put("/api/deployments/dep_northfield/stages/value", headers=H("em"), json={"state": "done"})
    client.post("/api/milestones/ms_nf1_6/submit", headers=H("em"), json={})
    client.put("/api/deployments/dep_northfield/stages/value", headers=H("em"), json={"state": "in_progress"})
    assert ms(client, "ms_nf1_6")["status"] == "submitted"


def test_a_new_milestone_on_a_finished_stage_is_ready_straight_away(client):
    r = client.post("/api/sows/sow_nf1/milestones", headers=H("em"),
                    json={"name": "Integration hardening", "amount": 5000, "stage": "integrate"})
    assert r.status_code == 201, r.text
    assert ms(client, r.json()["id"])["status"] == "ready"
    assert client.post("/api/sows/sow_nf1/milestones", headers=H("em"),
                       json={"name": "x", "stage": "nope"}).status_code == 422


def test_sow_setup(client):
    body = {"reference": "RL-1", "currency": "eur", "milestones": [
        {"name": "Pilot readout", "amount": 20000, "stage": "discover"}, {"name": "Rollout", "amount": 30000}]}
    assert client.post("/api/deployments/dep_northfield/sows", headers=H("fde"), json=body).status_code == 403
    r = client.post("/api/deployments/dep_redline/sows", headers=H("head"), json=body)
    assert r.status_code == 201, r.text
    assert r.json()["total_value"] == 50000 and r.json()["unallocated"] == 0
    cm = commercials(client, "head", "dep_redline")
    assert cm["sows"][0]["currency"] == "EUR" and cm["totals"]["contract"] == 50000
    bad = client.post("/api/deployments/dep_redline/sows", headers=H("head"), json={"currency": "XYZ"})
    assert bad.status_code == 422
    other = {"Authorization": "Bearer demo-head-orbital"}
    assert client.post("/api/deployments/dep_redline/sows", headers=other, json=body).status_code == 404
    assert client.get("/api/deployments/dep_northfield/commercials", headers=other).status_code == 404


# ------------------------------------------------------------ sign-off

def test_submit_freezes_evidence_and_the_customer_signs_off_that_exact_packet(client):
    cust = commercials(client, "customer")
    m = all_ms(cust)["ms_nf1_5"]
    assert m["status"] == "submitted" and m["amount"] == 30000 and "invoice_ref" not in m
    assert cust["audience"] == "customer" and "delay_days_by_owner" not in cust
    assert cust["waiting_on_customer"] == [{"id": "ms_nf1_5", "name": "Adoption plan approved"}]
    pk = client.get("/api/milestones/ms_nf1_5/packet", headers=H("customer")).json()
    assert pk["audience"] == "customer" and pk["intact"] and "hours_logged" not in pk["packet"]
    internal = client.get("/api/milestones/ms_nf1_5/packet", headers=H("em")).json()
    assert internal["audience"] == "internal" and "hours_logged" in internal["packet"]
    assert internal["packet"]["customer_packet_hash"] == pk["customer_hash"]
    # the EM can't sign off for the customer through the customer's button, and the head can't either
    assert client.post("/api/milestones/ms_nf1_5/accept", headers=H("em"), json={}).status_code == 403
    assert client.post("/api/milestones/ms_nf1_5/accept", headers=H("head"), json={}).status_code == 403
    r = client.post("/api/milestones/ms_nf1_5/accept", headers=H("customer"), json={"packet_hash": "sha256:other"})
    assert r.status_code == 409
    r = client.post("/api/milestones/ms_nf1_5/accept", headers=H("customer"),
                    json={"packet_hash": pk["customer_hash"], "note": "Approved in the steering meeting"})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["status"] == "accepted" and out["customer_packet_hash"] == pk["customer_hash"]
    row = ms(client, "ms_nf1_5")
    assert row["decided_by"] == "usr_ruth" and not row["proxy"]
    entry = trail(client, "milestone.accept")[0]
    assert entry["actor"] == "Ruth Albrecht" and entry["detail"]["customer_packet"] == pk["customer_hash"]
    # the receipt checks out against the record, for the customer too
    chk = client.post("/api/audit/verify-anchor", headers=H("customer"),
                      json={"seq": out["anchor"]["seq"], "hash": out["anchor"]["hash"]}).json()
    assert chk["ok"]
    assert client.post("/api/milestones/ms_nf1_5/accept", headers=H("customer"), json={}).status_code == 409
    assert client.get("/api/audit/verify", headers=H("head")).json()["ok"]


def test_rewriting_history_breaks_the_customers_receipt(client):
    pk = client.get("/api/milestones/ms_nf1_4/packet", headers=H("customer")).json()
    rec = pk["receipt"]
    ok = client.post("/api/audit/verify-anchor", headers=H("customer"),
                     json={"seq": rec["record_position"], "hash": rec["record_hash"]}).json()
    assert ok["ok"]
    with client.conn.tx():  # someone edits an earlier entry in the database
        first = client.conn.execute("SELECT seq FROM audit WHERE tenant_id='ten_meridian' ORDER BY seq LIMIT 1"
                                    ).fetchone()["seq"]
        client.conn.execute("UPDATE audit SET detail_json='{\"edited\":true}' WHERE seq=?", (first,))
    bad = client.post("/api/audit/verify-anchor", headers=H("customer"),
                      json={"seq": rec["record_position"], "hash": rec["record_hash"]}).json()
    assert not bad["ok"] and "altered" in bad["reason"]


def test_an_edited_packet_shows_as_altered(client):
    with client.conn.tx():
        row = client.conn.execute("SELECT id, customer_json FROM milestone_packets WHERE milestone_id='ms_nf1_4'"
                                  ).fetchone()
        p = json.loads(row["customer_json"])
        p["milestone"]["amount"] = 1
        client.conn.execute("UPDATE milestone_packets SET customer_json=? WHERE id=?", (json.dumps(p), row["id"]))
    assert client.get("/api/milestones/ms_nf1_4/packet", headers=H("customer")).json()["intact"] is False


def test_changes_requested_then_resubmitted(client):
    old = client.get("/api/milestones/ms_nf1_5/packet", headers=H("customer")).json()["customer_hash"]
    r = client.post("/api/milestones/ms_nf1_5/request-changes", headers=H("customer"), json={})
    assert r.status_code == 422
    r = client.post("/api/milestones/ms_nf1_5/request-changes", headers=H("customer"),
                    json={"note": "Harrisburg isn't in the plan yet"})
    assert r.status_code == 200 and ms(client, "ms_nf1_5")["status"] == "changes_requested"
    cm = commercials(client, "em")
    assert cm["totals"]["at_risk"] == 30000
    client.post("/api/tasks", headers=H("em"), json={"deployment_id": "dep_northfield", "title": "Harrisburg plan",
                                                      "visibility": "shared"})
    r = client.post("/api/milestones/ms_nf1_5/submit", headers=H("em"), json={"note": "Added Harrisburg"})
    assert r.status_code == 200 and r.json()["customer_hash"] != old
    # accepting against the evidence they saw before is refused: they review what's there now
    assert client.post("/api/milestones/ms_nf1_5/accept", headers=H("customer"),
                       json={"packet_hash": old}).status_code == 409


def test_amounts_lock_once_submitted(client):
    assert client.patch("/api/milestones/ms_nf1_5", headers=H("em"), json={"amount": 1}).status_code == 409
    assert client.patch("/api/milestones/ms_nf1_5", headers=H("em"), json={"due_on": "2026-12-01"}).status_code == 200
    assert client.delete("/api/milestones/ms_nf1_5", headers=H("em")).status_code == 409
    assert client.patch("/api/milestones/ms_nf1_6", headers=H("em"), json={"amount": 35000}).status_code == 200
    assert ms(client, "ms_nf1_6")["amount"] == 35000
    assert client.post("/api/milestones/ms_nf1_6/submit", headers=H("em"), json={}).status_code == 409  # stage not done
    assert client.post("/api/milestones/ms_nf1_6/submit", headers=H("fde"), json={}).status_code == 403


def test_a_signoff_recorded_on_the_customers_behalf_says_so(client):
    r = client.post("/api/milestones/ms_hv1_1/record-acceptance", headers=H("em"), json={"note": "Signed by email"})
    assert r.status_code == 200, r.text
    row = ms(client, "ms_hv1_1")
    assert row["status"] == "accepted" and row["proxy"] == 1 and row["decided_by"] == "usr_marcus"
    assert trail(client, "milestone.accept")[0]["detail"]["on_behalf_of_customer"] is True
    assert client.get("/api/milestones/ms_hv1_1/packet", headers=H("em")).status_code == 200


def test_who_sees_money(client):
    fde = all_ms(commercials(client, "fde"))
    assert "amount" not in fde["ms_nf1_1"]
    assert "totals" not in commercials(client, "fde")
    head = all_ms(commercials(client, "head"))
    assert head["ms_nf1_3"]["invoice_ref"] == "INV-10388"
    assert client.get("/api/commercials", headers=H("fde")).status_code == 403
    assert client.get("/api/commercials", headers=H("customer")).status_code == 403


def test_invoiced_paid_portfolio_and_finance_export(client):
    assert client.post("/api/milestones/ms_nf1_6/invoiced", headers=H("em"), json={}).status_code == 409
    assert client.post("/api/milestones/ms_nf1_4/invoiced", headers=H("em"),
                       json={"invoice_ref": "INV-10412"}).status_code == 200
    assert client.post("/api/milestones/ms_nf1_4/paid", headers=H("em"), json={"on": "2026-10-01"}).status_code == 200
    row = ms(client, "ms_nf1_4")
    assert row["status"] == "paid" and row["invoice_ref"] == "INV-10412" and row["paid_at"] == "2026-10-01"
    p = client.get("/api/commercials", headers=H("head")).json()
    assert p["totals"]["contract"] == 420000 + 610000 + 240000
    assert {i["id"] for i in p["awaiting_signoff"]} == {"ms_nf1_5"}
    assert "ms_ks1_4" in {i["id"] for i in p["ready_to_invoice"]}
    csv = client.get("/api/commercials.csv", headers=H("head"))
    assert csv.status_code == 200 and csv.headers["content-type"].startswith("text/csv")
    lines = csv.text.strip().splitlines()
    assert lines[0].startswith("customer,deployment,sow_reference,milestone,amount")
    row = next(ln for ln in lines if "Go-live across three sites" in ln)
    assert "INV-10412" in row and "sha256:" in row
    assert trail(client, "billing.export")


def test_the_customer_report_carries_the_record_fingerprint(client):
    r = client.post("/api/deployments/dep_northfield/reports", headers=H("em"), json={"audience": "customer"})
    assert r.status_code == 201, r.text
    body = client.get("/api/deployments/dep_northfield/reports", headers=H("em")).json()[0]["body_md"]
    assert "Record fingerprint: position" in body and "sha256:" in body


# ------------------------------------------------------------ the billing bridge

def link(client, cid, ext, sow="sow_nf1"):
    r = client.put(f"/api/sows/{sow}/link", headers=H("em"), json={"connection_id": cid, "external_id": ext})
    assert r.status_code == 200, r.text


def accept_as_ruth(client, mid="ms_nf1_5"):
    h = client.get(f"/api/milestones/{mid}/packet", headers=H("customer")).json()["customer_hash"]
    r = client.post(f"/api/milestones/{mid}/accept", headers=H("customer"), json={"packet_hash": h})
    assert r.status_code == 200, r.text


def oracle_fakes(web, invoiced=False):
    state = {"events": []}

    def post(req):
        e = {**req["json"], "EventId": 300100 + len(state["events"]), "EventNumber": 7 + len(state["events"]),
             "Invoiced": "U", "InvoicedAmount": 0,
             "links": [{"rel": "self", "href": f"https://abcd.fa.us2.oraclecloud.com/fscmRestApi/resources/11.13.18.05/projectBillingEvents/HASH{len(state['events'])}"}]}
        state["events"].append(e)
        return 201, e

    def get(req):
        q = req["query"].get("q", "")
        items = state["events"]
        if "SourceReference=" in q:
            ref = q.split("SourceReference='")[1].rstrip("'")
            items = [e for e in items if e.get("SourceReference") == ref]
        if invoiced:
            items = [{**e, "Invoiced": "F", "InvoicedAmount": e["BillTrnsAmount"]} for e in items]
        return 200, {"items": items, "hasMore": False, "count": len(items)}

    web.route("GET", r"oraclecloud\.com/fscmRestApi/resources/11\.13\.18\.05/projectBillingEvents$", get)
    web.route("POST", r"oraclecloud\.com/fscmRestApi/resources/11\.13\.18\.05/projectBillingEvents$", post)
    return state


def test_oracle_signoff_creates_one_billing_event_and_invoiced_comes_back(client, web):
    state = oracle_fakes(web)
    r = client.post("/api/connections/oracle_fusion/token", headers=H("head"), json={
        "fields": {"host": "https://abcd.fa.us2.oraclecloud.com/", "username": "fw.integration", "password": "pw"}})
    assert r.status_code == 201, r.text
    cid = r.json()["id"]
    assert web.calls[-1]["headers"]["Authorization"].startswith("Basic ")
    assert web.calls[-1]["headers"]["REST-Framework-Version"] == "4"
    client.patch(f"/api/connections/{cid}/settings", headers=H("head"), json={"settings": {"event_type": "Milestone"}})
    link(client, cid, "C-100")
    accept_as_ruth(client)
    assert ms(client, "ms_nf1_5")["erp_status"] == "queued"
    events.process(client.conn)
    posted = web.called("POST", "projectBillingEvents")
    assert len(posted) == 1
    body = posted[0]["json"]
    assert body["ContractNumber"] == "C-100" and body["BillTrnsAmount"] == 30000 and body["SourceReference"] == "ms_nf1_5"
    assert body["EventTypeName"] == "Milestone" and body["CompletionDate"]
    row = ms(client, "ms_nf1_5")
    assert row["erp_status"] == "sent" and row["external_id"] == "300100"
    assert trail(client, "milestone.erp_push")[0]["actor"] == "erp:oracle_fusion"
    # a retry never bills twice: the event is found by id and released instead
    client.post("/api/milestones/ms_nf1_5/push", headers=H("em"))
    assert len(web.called("POST", "projectBillingEvents")) == 1
    # invoiced flows back on the next sync
    oracle_fakes(web, invoiced=True)["events"].extend(state["events"])
    cx = core.get(client.conn, cid)
    out = core.run_sync(client.conn, cx)
    assert out["invoiced"] >= 1 and ms(client, "ms_nf1_5")["status"] == "invoiced"
    assert trail(client, "milestone.invoiced")[0]["actor"] == "erp:oracle_fusion"


def test_oracle_needs_contract_and_event_type_and_says_so(client, web):
    oracle_fakes(web)
    cid = client.post("/api/connections/oracle_fusion/token", headers=H("head"), json={
        "fields": {"host": "abcd.fa.us2.oraclecloud.com", "username": "u", "password": "p"}}).json()["id"]
    link(client, cid, "C-100")
    accept_as_ruth(client)
    events.process(client.conn)
    row = ms(client, "ms_nf1_5")
    assert row["erp_status"] == "error" and "event type" in row["erp_error"]
    assert client.post("/api/connections/oracle_fusion/token", headers=H("head"),
                       json={"fields": {"host": "abcd.fa.us2.oraclecloud.com"}}).status_code == 422
    assert client.post("/api/connections/oracle_fusion/token", headers=H("em"), json={
        "fields": {"host": "abcd.fa.us2.oraclecloud.com", "username": "u", "password": "p"}}).status_code == 403


def netsuite_fakes(web):
    host = r"1234567-sb1\.suitetalk\.api\.netsuite\.com/services/rest"
    web.json("POST", host + r"/auth/oauth2/v1/token", {"access_token": "ns-at", "refresh_token": "ns-rt",
                                                      "expires_in": 3600, "token_type": "bearer"})

    def suiteql(req):
        q = req["json"]["q"]
        if "FROM job" in q:
            return 200, {"items": [{"id": "881", "entityid": "NSC-AP", "companyname": "Northfield AP agent"}],
                         "hasMore": False}
        if "FROM projecttask" in q:
            return 200, {"items": [{"id": "5001", "title": "Adoption plan", "enddate": "2026-10-01", "status_name": "Not Started"},
                                   {"id": "5002", "title": "Value readout", "enddate": "2026-11-01", "status_name": "Not Started"}],
                         "hasMore": False}
        if "CustInvc" in q:
            return 200, {"items": [{"id": "9", "tranid": "INV-2001", "trandate": "2026-10-02", "foreigntotal": "30000.00",
                                    "foreignamountunpaid": "0.00"}], "hasMore": False}
        return 400, {"title": "unexpected query"}

    web.route("POST", host + r"/query/v1/suiteql", suiteql)
    web.route("PATCH", host + r"/record/v1/projectTask/\d+", lambda req: (204, b""))


def test_netsuite_connects_with_the_customers_integration_record(client, web):
    netsuite_fakes(web)
    r = client.post("/api/connections/netsuite/start", headers=H("head"),
                    json={"fields": {"account": "1234567_SB1", "client_id": "ns-client-123", "client_secret": "ns-secret-456"}})
    assert r.status_code == 200, r.text
    url = urlparse(r.json()["url"])
    q = parse_qs(url.query)
    assert url.netloc == "1234567-sb1.app.netsuite.com" and url.path == "/app/login/oauth2/authorize.nl"
    assert q["client_id"] == ["ns-client-123"] and q["scope"] == ["rest_webservices"]
    assert q["code_challenge_method"] == ["S256"]
    meta = client.conn.execute("SELECT meta FROM oauth_states WHERE state=?", (q["state"][0],)).fetchone()["meta"]
    assert "ns-secret-456" not in meta  # the client secret is encrypted while the sign-in is in flight
    cookie = r.headers["set-cookie"].split(";")[0]
    back = client.get(f"/oauth/callback?state={q['state'][0]}&code=c1", headers={"Cookie": cookie},
                      follow_redirects=False)
    assert "connected=netsuite" in back.headers["location"], back.headers["location"]
    tok = web.called("POST", "oauth2/v1/token")[0]
    assert tok["headers"]["Authorization"].startswith("Basic ") and tok["form"]["code_verifier"]
    cx = core.active(client.conn, "ten_meridian", "netsuite")
    assert cx.tokens["access_token"] == "ns-at" and cx["account_name"] == "NetSuite 1234567_SB1"
    assert client.post("/api/connections/netsuite/start", headers=H("head"),
                       json={"fields": {"account": "bad acct!", "client_id": "x" * 10, "client_secret": "y" * 10}}
                       ).status_code == 422
    return cx


def test_netsuite_import_complete_and_paid(client, web):
    cx = test_netsuite_connects_with_the_customers_integration_record(client, web)
    projs = client.get(f"/api/billing/connections/{cx.id}/projects", headers=H("em")).json()
    assert projs == [{"id": "881", "name": "Northfield AP agent", "customer": "", "currency": None}]
    r = client.post("/api/deployments/dep_redline/sows/import", headers=H("head"),
                    json={"connection_id": cx.id, "project_id": "881"})
    assert r.status_code == 201, r.text
    sid = r.json()["id"]
    rows = client.conn.execute("SELECT * FROM milestones WHERE sow_id=? ORDER BY position", (sid,)).fetchall()
    assert [x["external_id"] for x in rows] == ["5001", "5002"] and rows[0]["name"] == "Adoption plan"
    assert client.post("/api/deployments/dep_redline/sows/import", headers=H("head"),
                       json={"connection_id": cx.id, "project_id": "881"}).status_code == 409
    # sign-off on a hand-marked milestone, then NetSuite completes the task and finds the paid invoice
    mid = rows[0]["id"]
    client.patch(f"/api/milestones/{mid}", headers=H("head"), json={"amount": 30000})
    assert client.post(f"/api/milestones/{mid}/record-acceptance", headers=H("head"),
                       json={"note": "Signed PDF from Redline ops"}).status_code == 200
    events.process(client.conn)
    patch = web.called("PATCH", r"projectTask/5001")
    assert patch and patch[0]["json"] == {"status": {"id": "COMPLETE"}}
    assert ms(client, mid)["erp_status"] == "sent"
    out = core.run_sync(client.conn, core.get(client.conn, cx.id))
    assert out["paid"] == 1
    row = ms(client, mid)
    assert row["status"] == "paid" and row["invoice_ref"] == "INV-2001"


def certinia_fakes(web, refuse=False):
    inst = r"na1\.my\.salesforce\.com"
    web.json("POST", r"login\.salesforce\.com/services/oauth2/token", {
        "access_token": "sf-at", "refresh_token": "sf-rt", "instance_url": "https://na1.my.salesforce.com",
        "id": "https://login.salesforce.com/id/00D1/0051"})
    web.json("GET", r"login\.salesforce\.com/id/", {"organization_id": "00D1", "username": "ops@meridian.example"})
    web.json("GET", inst + r"/services/data/v\d+\.0/sobjects/pse__Milestone__c/describe", {"fields": [
        {"name": n} for n in ("Id", "Name", "pse__Project__c", "pse__Status__c", "pse__Milestone_Amount__c",
                              "pse__Target_Date__c", "pse__Actual_Date__c", "pse__Approved__c",
                              "pse__Include_In_Financials__c", "pse__Invoiced__c")]})
    web.json("GET", inst + r"/services/data/v\d+\.0/sobjects/pse__Proj__c/describe", {"fields": [
        {"name": n} for n in ("Id", "Name", "pse__Account__c", "pse__Is_Active__c")]})
    web.route("GET", inst + r"/services/data/v\d+\.0/query", lambda req: (200, {"done": True, "records": [
        {"Id": "a1B000000000001AAA", "Name": "Go-live", "pse__Milestone_Amount__c": 120000, "pse__Status__c": "Approved",
         "pse__Invoiced__c": True, "pse__Target_Date__c": "2026-09-01"}]}))
    if refuse:
        web.json("PATCH", inst + r"/services/data/v\d+\.0/sobjects/pse__Milestone__c/", [
            {"message": "Milestone must go through the approval process", "errorCode": "FIELD_CUSTOM_VALIDATION_EXCEPTION"}],
            status=400)
    else:
        web.route("PATCH", inst + r"/services/data/v\d+\.0/sobjects/pse__Milestone__c/", lambda req: (204, b""))


def test_certinia_approves_the_milestone_with_only_the_fields_the_org_has(client, web):
    certinia_fakes(web)
    install(client, "certinia")
    cx = core.active(client.conn, "ten_meridian", "certinia")
    assert "pse__Approved__c" in cx.extra["ms_fields"]
    assert cx.settings == {"approve": True, "include_in_financials": True, "approve_for_billing": False}
    link(client, cx.id, "a1A000000000001AAA")
    client.put("/api/milestones/ms_nf1_5/link", headers=H("em"), json={"external_id": "a1B000000000001AAA"})
    accept_as_ruth(client)
    events.process(client.conn)
    body = web.called("PATCH", "pse__Milestone__c")[0]["json"]
    assert body["pse__Status__c"] == "Approved" and body["pse__Approved__c"] is True
    assert body["pse__Include_In_Financials__c"] is True and "pse__Approved_for_Billing__c" not in body
    out = core.run_sync(client.conn, core.get(client.conn, cx.id))
    assert out["invoiced"] == 1 and ms(client, "ms_nf1_5")["status"] == "invoiced"
    # it isn't a pipeline source: no opportunities come in from Certinia
    assert not web.called("GET", "Opportunity")


def test_certinia_refusal_shows_on_the_milestone_and_can_be_retried(client, web):
    certinia_fakes(web, refuse=True)
    install(client, "certinia")
    cx = core.active(client.conn, "ten_meridian", "certinia")
    link(client, cx.id, "a1A000000000001AAA")
    client.put("/api/milestones/ms_nf1_5/link", headers=H("em"), json={"external_id": "a1B000000000001AAA"})
    accept_as_ruth(client)
    events.process(client.conn)
    row = ms(client, "ms_nf1_5")
    assert row["erp_status"] == "error" and "approval process" in row["erp_error"]
    web.route("PATCH", r"/sobjects/pse__Milestone__c/", lambda req: (204, b""))
    assert client.post("/api/milestones/ms_nf1_5/push", headers=H("em")).json()["erp_status"] == "sent"


def sap_fakes(web, cleared="C"):
    base = r"my123456-api\.s4hana\.ondemand\.com/sap/opu/odata/sap"
    web.json("GET", base + r"/API_ENTERPRISE_PROJECT_SRV;v=0002/A_EnterpriseProject$", {"d": {"results": [
        {"ProjectUUID": "fa163e8b-1111-1edb-a000-000000000001", "Project": "NSC-01", "ProjectDescription": "AP agent"}]}})
    web.json("GET", base + r"/API_ENTERPRISE_PROJECT_SRV;v=0002/A_EnterpriseProjectElement$", {"d": {"results": [
        {"ProjectElementUUID": "fa163e8b-2222-1edb-a000-000000000002", "ProjectElement": "NSC-01.3",
         "ProjectElementDescription": "Adoption plan", "PlannedEndDate": "/Date(1790380800000)/", "ActualEndDate": None}]}})
    web.route("GET", base + r"/API_ENTERPRISE_PROJECT_SRV;v=0002/A_EnterpriseProjectElement\(", lambda req: (
        200, {"d": {"ProjectElementUUID": "x"}}, {"x-csrf-token": "tok-123", "set-cookie": "SAP_SESSIONID=abc; path=/, sap-usercontext=x; path=/"}))
    web.route("PATCH", base + r"/API_ENTERPRISE_PROJECT_SRV;v=0002/A_EnterpriseProjectElement\(", lambda req: (204, b""))
    web.json("GET", base + r"/API_BILLING_DOCUMENT_SRV/A_BillingDocumentItem$", {"d": {"results": [
        {"BillingDocument": "90000123"}]}})
    web.json("GET", base + r"/API_BILLING_DOCUMENT_SRV/A_BillingDocument\(", {"d": {
        "BillingDocument": "90000123", "InvoiceClearingStatus": cleared, "BillingDocumentIsCancelled": False,
        "BillingDocumentDate": "/Date(1790467200000)/"}})


def test_sap_confirms_the_milestone_with_a_csrf_token_and_reads_clearing(client, web):
    sap_fakes(web)
    r = client.post("/api/connections/sap_s4/token", headers=H("head"), json={"fields": {
        "host": "my123456-api.s4hana.ondemand.com", "username": "COMM_USER", "password": "pw"}})
    assert r.status_code == 201, r.text
    cid = r.json()["id"]
    lines = client.get(f"/api/billing/connections/{cid}/projects/fa163e8b-1111-1edb-a000-000000000001/lines",
                       headers=H("em")).json()
    assert lines[0]["id"] == "fa163e8b-2222-1edb-a000-000000000002|NSC-01.3" and lines[0]["due_on"]
    link(client, cid, "fa163e8b-1111-1edb-a000-000000000001")
    client.put("/api/milestones/ms_nf1_5/link", headers=H("em"), json={"external_id": lines[0]["id"]})
    accept_as_ruth(client)
    events.process(client.conn)
    fetch = [c for c in web.calls if c["headers"].get("x-csrf-token") == "Fetch"]
    patch = web.called("PATCH", "A_EnterpriseProjectElement")
    assert fetch and patch and patch[0]["headers"]["x-csrf-token"] == "tok-123"
    assert "SAP_SESSIONID=abc" in patch[0]["headers"]["Cookie"] and patch[0]["json"]["ActualEndDate"].startswith("/Date(")
    core.run_sync(client.conn, core.get(client.conn, cid))
    row = ms(client, "ms_nf1_5")
    assert row["status"] == "paid" and row["invoice_ref"] == "90000123"


def test_workday_reads_installments_and_hears_about_signoffs_by_webhook(client, web):
    report = "https://wd2-impl-services1.workday.com/ccx/service/customreport2/acme/ISU_FW/Billing_Installments"
    rows = [{"Contract": "CC-77", "Installment_ID": "INST-1", "Installment": "Adoption plan", "Amount": "30000",
             "Installment_Date": "2026-10-01", "Invoice": None, "Payment_Status": None}]
    web.route("GET", r"workday\.com/ccx/service/customreport2/", lambda req: (200, {"Report_Entry": rows}))
    r = client.post("/api/connections/workday/token", headers=H("head"), json={"fields": {
        "report_url": report, "username": "ISU_FW", "password": "pw"}})
    assert r.status_code == 201, r.text
    assert web.calls[-1]["query"]["format"] == "json"
    cid = r.json()["id"]
    imp = client.post("/api/deployments/dep_redline/sows/import", headers=H("head"),
                      json={"connection_id": cid, "project_id": "CC-77"}).json()
    mid = client.conn.execute("SELECT id FROM milestones WHERE sow_id=?", (imp["id"],)).fetchone()["id"]
    assert ms(client, mid)["amount"] == 30000
    client.post(f"/api/milestones/{mid}/record-acceptance", headers=H("head"), json={"note": "Email from Redline"})
    assert ms(client, mid)["erp_status"] == "notified"  # Workday hears through the signed webhook, not a write here
    assert not client.conn.execute("SELECT 1 FROM outbox WHERE kind='billing_push'").fetchone()
    rows[0].update(Invoice={"Descriptor": "CI-5501"}, Payment_Status={"Descriptor": "Paid"})
    core.run_sync(client.conn, core.get(client.conn, cid))
    row = ms(client, mid)
    assert row["status"] == "paid" and row["invoice_ref"] == "CI-5501"


def test_signoff_webhook_carries_the_proof(client, web):
    got = []
    web.route("POST", r"finance\.example/hook", lambda req: (got.append(req) or (200, {})))
    cfg = client.get("/api/config", headers=H("head")).json()["config"]
    cfg["integrations"]["webhooks"].append({"id": "fin", "url": "https://finance.example/hook",
                                            "events": ["milestone.accepted"]})
    assert client.put("/api/config", headers=H("head"), json=cfg).status_code == 200
    accept_as_ruth(client)
    row = client.conn.execute("SELECT payload_json FROM outbox WHERE kind='webhook'").fetchone()
    data = json.loads(row["payload_json"])["data"]
    assert data["milestone_id"] == "ms_nf1_5" and data["customer_packet_hash"].startswith("sha256:")
    assert data["record_position"] and data["record_hash"].startswith("sha256:") and data["amount"] == 30000


def test_billing_routes_are_for_sow_editors(client, web):
    oracle_fakes(web)
    cid = client.post("/api/connections/oracle_fusion/token", headers=H("head"), json={
        "fields": {"host": "abcd.fa.us2.oraclecloud.com", "username": "u", "password": "p"}}).json()["id"]
    assert client.get("/api/billing/connections", headers=H("fde")).status_code == 403
    assert client.get(f"/api/billing/connections/{cid}/projects", headers=H("customer")).status_code == 403
    assert [c["provider"] for c in client.get("/api/billing/connections", headers=H("em")).json()] == ["oracle_fusion"]
    other = {"Authorization": "Bearer demo-head-orbital"}
    assert client.get(f"/api/billing/connections/{cid}/projects", headers=other).status_code == 404
