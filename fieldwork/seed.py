"""Demo workspace: Meridian AI's deployment team running four customer deployments.

All names are fictional. The team is mixed the way real deployment orgs are:
a head of deployments, an engagement manager, an implementation consultant,
FDEs and AI engineers, plus one customer stakeholder with a read-only view.

The workspace has also made the default template its own, as a customer would:
white-labeled as "Meridian Deploy", its own go-live readiness script plugged in
as a webhook engine on the Go-live stage, and a latency probe that pushes
results in from inside a customer's environment.

Some tasks and one confirmed value readout are shared with the customer;
everything else stays internal.

A second tenant (Orbital Labs) exists so the demo and tests can show that
workspaces are isolated.

    python -m fieldwork seed
"""

from __future__ import annotations

import json
import random

from . import audit, config, crypto, db
from .engines import stages
from .app import token_hash

DEMO_TOKENS = {
    "head": "demo-head-meridian",
    "em": "demo-em-meridian",
    "ic": "demo-ic-meridian",
    "fde": "demo-fde-meridian",
    "ai": "demo-ai-meridian",
    "customer": "demo-customer-northfield",
    "other_tenant": "demo-head-orbital",
}

DEMO_LOGINS = [
    {"label": "Dana Whitfield · Head of Deployments", "token": DEMO_TOKENS["head"]},
    {"label": "Marcus Hale · Engagement Manager", "token": DEMO_TOKENS["em"]},
    {"label": "Rosa Delgado · Implementation Consultant", "token": DEMO_TOKENS["ic"]},
    {"label": "Maya Chen · Forward Deployed Engineer", "token": DEMO_TOKENS["fde"]},
    {"label": "Jordan Reyes · AI Engineer", "token": DEMO_TOKENS["ai"]},
    {"label": "Ruth Albrecht · Customer (Northfield)", "token": DEMO_TOKENS["customer"]},
]

DEMO_ENGINE_SECRET = "fws_demo_readiness_signing_secret"
DEMO_PUSH_TOKEN = "fwe_demo_latency_probe_token"
def demo_engine_url() -> str:
    """Hosted demos call the copy of the engine the app serves itself; local demos call the example script."""
    import os
    pub = os.environ.get("FIELDWORK_PUBLIC_URL", "")
    return pub.rstrip("/") + "/demo-engines/readiness" if pub.startswith("https://") else "http://127.0.0.1:8787/"

READINESS_CHECKLIST = """[x] Cutover runbook signed off by customer IT
[x] Rollback plan rehearsed
[x] Agent permissions scoped to production (blocker)
[ ] Security review of model gateway closed (blocker)
[x] Support rota for first 72 hours
[ ] Exec sponsor go/no-go meeting booked
[x] Training for claims supervisors delivered"""

NORTHFIELD_STAFFING = """Required
- 3+ years of software development experience
- Experience with Python and SQL
- Experience integrating with NetSuite
- Willing to travel to customer sites up to 25%

Preferred
- Experience with AWS
- Background in supply chain or logistics
"""

HARBORVIEW_STAFFING = """Required
- 2+ years of software development experience
- Experience with HL7 or FHIR interfaces
- Working knowledge of HIPAA

Preferred
- Experience with Epic
- Background in healthcare
"""

PEOPLE = [
    # uid, name, role, token key, bench profile
    ("usr_dana", "Dana Whitfield", "head", "head", {}),
    ("usr_marcus", "Marcus Hale", "engagement_manager", "em", {}),
    ("usr_rosa", "Rosa Delgado", "implementation_consultant", "ic", {
        "location": "Pittsburgh, PA",
        "evidence": [
            {"id": "emp.rosa.1", "kind": "employment", "org": "Crestline ERP Partners",
             "title": "Implementation Consultant", "start": "2018-05", "end": "2023-02",
             "claims": ["Led NetSuite implementations for distributors", "Supply chain process design, SQL reporting"],
             "tags": ["netsuite", "sql", "supply chain", "logistics"]},
            {"id": "emp.rosa.2", "kind": "employment", "org": "Meridian AI",
             "title": "Implementation Consultant", "start": "2023-03", "end": "2026-09",
             "claims": ["Customer onboarding and training"], "tags": []},
        ]}),
    ("usr_maya", "Maya Chen", "fde", "fde", {
        "location": "Pittsburgh, PA",
        "evidence": [
            {"id": "emp.maya.1", "kind": "employment", "org": "Brightline Freight", "title": "Software Engineer",
             "start": "2020-06", "end": "2024-01",
             "claims": ["Built Python and SQL pipelines reconciling carrier invoices against NetSuite",
                        "Owned the supply chain data model"],
             "tags": ["python", "sql", "netsuite", "logistics"]},
            {"id": "emp.maya.2", "kind": "employment", "org": "Meridian AI", "title": "Forward Deployed Engineer",
             "start": "2024-02", "end": "2026-09",
             "claims": ["Deployed agents on AWS for three customers"], "tags": ["aws", "python"]},
        ]}),
    ("usr_sam", "Sam Okoro", "fde", None, {
        "location": "Pittsburgh, PA",
        "evidence": [
            {"id": "emp.sam.1", "kind": "employment", "org": "Meridian AI", "title": "Forward Deployed Engineer",
             "start": "2025-06", "end": "2026-09",
             "claims": ["Python agent tooling, SQL reporting"], "tags": ["python", "sql"]},
        ],
        "absent": ["netsuite"]}),
    ("usr_jordan", "Jordan Reyes", "ai_engineer", "ai", {
        "location": "Chicago, IL",
        "evidence": [
            {"id": "emp.jordan.1", "kind": "employment", "org": "Lakeshore Health", "title": "Integration Analyst",
             "start": "2021-01", "end": "2024-06",
             "claims": ["Built HL7 and FHIR interfaces into Epic", "HIPAA training and audits"],
             "tags": ["hl7", "fhir", "epic", "hipaa", "healthcare"]},
            {"id": "emp.jordan.2", "kind": "employment", "org": "Meridian AI", "title": "AI Engineer",
             "start": "2024-07", "end": "2026-09", "claims": ["Python services for clinical agents"],
             "tags": ["python", "sql"]},
        ]}),
    ("usr_lena", "Lena Park", "ai_engineer", None, {
        "location": "New York, NY",
        "evidence": [
            {"id": "emp.lena.1", "kind": "employment", "org": "Keel Systems", "title": "Backend Engineer",
             "start": "2019-03", "end": "2023-08",
             "claims": ["Python and SQL services on AWS", "ERP integrations including NetSuite"],
             "tags": ["python", "sql", "aws", "netsuite"]},
            {"id": "emp.lena.2", "kind": "employment", "org": "Meridian AI", "title": "AI Engineer",
             "start": "2023-09", "end": "2026-09", "claims": ["Customer deployments in finance ops"],
             "tags": ["python"]},
        ]}),
    ("usr_ruth", "Ruth Albrecht", "customer", "customer", {}),
]


def meridian_config() -> dict:
    cfg = config.default()
    cfg["branding"] = {"product_name": "Meridian Deploy", "accent": "#7fb8a4", "logo_url": ""}
    cfg["engines"] = [
        {"key": "readiness", "kind": "webhook", "name": "Go-live readiness check",
         "does": "Meridian's own cutover checklist scorer. Fails on any open blocker.",
         "input_hint": "One item per line: [x] done / [ ] open. Tag blockers with (blocker).",
         "url": demo_engine_url()},
        {"key": "latency_probe", "kind": "push", "name": "Latency probe",
         "does": "Runs inside the customer's environment and posts agent latency against SLO."},
    ]
    for st in cfg["stages"]:
        if st["key"] == "golive":
            st["engines"] = ["golive", "readiness"]
    return config.validate(cfg, allow_http_engines=True)

REDLINE_CENSUS = """system,owner,data_class,interface,access,documented,controls
MercuryGate TMS,Redline IT (J. Ortiz),internal,api,granted,yes,sso; audit logging
Carrier portal (vendor-hosted),,internal,ui,requested,no,
NetSuite ERP,Finance systems (A. Patel),confidential,api,granted,yes,sso; audit logging
Disputes inbox (Microsoft 365),Ops (K. Lee),confidential,api,requested,yes,sso
Rate database,Pricing (D. Wu),internal,db,none,no,
Claims archive (SharePoint),Ops (K. Lee),regulated,api,granted,no,sso"""

HARBORVIEW_AUTHORITY = json.dumps({
    "profile": {
        "status": "active", "default_decision": "BLOCK",
        "credentials": [{"name": "prior-auth-policy-evaluation", "status": "valid",
                         "expires_at": "2027-09-01T00:00:00Z"}],
        "privileges": [
            {"action": "integration.payer.submit_request", "effect": "allow",
             "environments": ["production"], "data_scopes": ["assigned_commercial_members"],
             "target_systems": ["payer"], "required_credentials": ["prior-auth-policy-evaluation"],
             "required_evidence_types": ["benefit_policy", "clinical_documentation"],
             "min_evidence_items": 2, "requires_human_review": True, "max_actions_per_hour": 10,
             "constraints": ["Retain the Cortex attestation with the case record"]},
            {"action": "ehr.write_intake_note", "effect": "allow", "environments": ["production"],
             "target_systems": ["ehr"]},
        ]},
    "scenarios": [
        {"name": "Complete packet, no reviewer yet", "expect": "HUMAN_REVIEW",
         "request": {"action": "integration.payer.submit_request", "environment": "production",
                     "data_scope": "assigned_commercial_members", "target_system": "payer",
                     "evidence": [{"type": "benefit_policy"}, {"type": "clinical_documentation"}]}},
        {"name": "Complete packet, reviewer approved", "expect": "ALLOW_WITH_LIMITS",
         "request": {"action": "integration.payer.submit_request", "environment": "production",
                     "data_scope": "assigned_commercial_members", "target_system": "payer",
                     "evidence": [{"type": "benefit_policy"}, {"type": "clinical_documentation"}],
                     "approval": {"status": "approved"}}},
        {"name": "Missing clinical documentation", "expect": "REQUEST_MORE_EVIDENCE",
         "request": {"action": "integration.payer.submit_request", "environment": "production",
                     "data_scope": "assigned_commercial_members", "target_system": "payer",
                     "evidence": [{"type": "benefit_policy"}]}},
        {"name": "Tries it from staging", "expect": "BLOCK",
         "request": {"action": "integration.payer.submit_request", "environment": "staging",
                     "data_scope": "assigned_commercial_members", "target_system": "payer",
                     "evidence": [{"type": "benefit_policy"}, {"type": "clinical_documentation"}]}},
        {"name": "Cancels a request (never granted)", "expect": "BLOCK",
         "request": {"action": "integration.payer.cancel_request", "environment": "production"}},
    ]}, indent=2)

CASTELLAN_CONFORMANCE = """# threshold: 0.95
case_id,category,expected,actual,critical
R-01,routing,Auto physical damage,Auto physical damage,no
R-02,routing,Property water,Property water,no
R-03,routing,Bodily injury,Bodily injury,yes
R-04,routing,Auto glass,Auto glass,no
R-05,routing,Property fire,Property - fire,no
S-01,severity,High,High,yes
S-02,severity,Low,Low,no
S-03,severity,Medium,Medium,no
S-04,severity,High,High,yes
F-01,fraud_flag,flag,flag,yes
F-02,fraud_flag,clear,clear,no
F-03,fraud_flag,flag,clear,yes
F-04,fraud_flag,clear,clear,no
E-01,extraction,4210.50,4210.50,no
E-02,extraction,1875,1875.00,no
E-03,extraction,12999.99,13000,no
E-04,extraction,2024-11-03,2024-11-03,no
E-05,extraction,POL-883201,POL-883201,no"""

CASTELLAN_ISSUES = """# max_open_sev2: 2
id,severity,status,opened_at,resolved_at,area
INC-101,sev2,resolved,2026-10-20T08:10,2026-10-20T11:40,routing
INC-102,sev3,resolved,2026-10-20T09:05,2026-10-20T15:00,ui
INC-103,sev1,resolved,2026-10-20T10:30,2026-10-20T12:05,model gateway
INC-104,sev2,resolved,2026-10-20T13:15,2026-10-21T09:00,extraction
INC-105,sev3,open,2026-10-20T16:45,,ui
INC-106,sev2,resolved,2026-10-21T07:50,2026-10-21T10:10,routing
INC-107,sev3,resolved,2026-10-21T09:20,2026-10-21T13:30,training
INC-108,sev2,open,2026-10-21T14:05,,extraction
INC-109,sev4,open,2026-10-21T18:40,,ui"""

NORTHFIELD_VALUE = json.dumps({
    "metric": "minutes per invoice exception", "baseline": 11.2, "current": 7.4, "lower_is_better": True,
    "volume_per_month": 5200, "cost_per_hour": 38, "planned_days": 60, "actual_days": 78,
    "delays": [
        {"event": "Customer security review of the model gateway", "days": 9, "cause": "customer"},
        {"event": "Our NetSuite connector rework", "days": 4, "cause": "vendor"},
        {"event": "NetSuite sandbox refresh window", "days": 3, "cause": "third_party"},
    ]}, indent=2)

SAMPLE_INPUTS = {
    "census": REDLINE_CENSUS, "cortex": HARBORVIEW_AUTHORITY, "conformance": CASTELLAN_CONFORMANCE,
    "golive": CASTELLAN_ISSUES, "attribution": NORTHFIELD_VALUE, "readiness": READINESS_CHECKLIST,
}


def northfield_adoption_csv(seed: int = 7) -> str:
    """Minutes per invoice exception for AP clerks at three Northfield sites.

    Harrisburg is uniformly slow (a configuration problem) while new clerks
    everywhere trail experienced ones a little (some training).
    """
    rnd = random.Random(seed)
    lines = ["user_id,minutes_per_exception,tenure_months,site"]
    n = 0
    for site, base in (("Allentown", 6.0), ("Erie", 6.3), ("Harrisburg", 11.5)):
        for _ in range(14):
            n += 1
            tenure = rnd.choice([2, 4, 8, 14, 26, 40])
            minutes = base + (1.2 if tenure < 6 else 0) + rnd.gauss(0, 0.6)
            lines.append(f"u{n:03d},{minutes:.2f},{tenure},{site}")
    return "\n".join(lines)


def seed(db_url) -> dict:
    """Wipe the database (a URL/path, or an open db.DB) and build the demo workspace."""
    conn = db_url if isinstance(db_url, db.DB) else db.connect(db_url)
    db.reset(conn)
    db.init(conn)
    ts = audit.now()
    T = "ten_meridian"

    def ins(table: str, **cols):
        conn.execute(f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                     tuple(cols.values()))

    with db.tx(conn):
        ins("tenants", id=T, name="Meridian AI", slug="meridian", config_json=json.dumps(meridian_config()),
            created_at=ts)
        ins("tenants", id="ten_orbital", name="Orbital Labs", slug="orbital",
            config_json=json.dumps(config.default()), created_at=ts)

        for uid, name, role, tkey, prof in PEOPLE:
            tok = DEMO_TOKENS[tkey] if tkey else f"demo-{uid}-meridian"
            ins("users", id=uid, tenant_id=T, name=name, email=f"{name.split()[0].lower()}@meridian.example",
                role=role, manager_id=None, token_hash=token_hash(tok), profile_json=json.dumps(prof),
                created_at=ts)
        ins("users", id="usr_orb", tenant_id="ten_orbital", name="Ines Duarte", email="ines@orbital.example",
            role="head", manager_id=None, token_hash=token_hash(DEMO_TOKENS["other_tenant"]),
            profile_json="{}", created_at=ts)

        ins("engine_credentials", tenant_id=T, engine_key="readiness", secret=crypto.encrypt(DEMO_ENGINE_SECRET),
            token_hash=None, created_at=ts)
        ins("engine_credentials", tenant_id=T, engine_key="latency_probe", secret=None,
            token_hash=token_hash(DEMO_PUSH_TOKEN), created_at=ts)

        customers = [
            ("cus_northfield", "Northfield Supply Co.", "Distribution", {"arr": 420000, "exec_sponsor": "CFO, R. Albrecht"}),
            ("cus_harborview", "Harborview Health", "Healthcare", {"arr": 610000, "exec_sponsor": "CIO, T. Nakamura"}),
            ("cus_castellan", "Castellan Mutual", "Insurance", {"arr": 380000}),
            ("cus_redline", "Redline Logistics", "Logistics", {"arr": 150000}),
        ]
        for cid, name, ind, fields in customers:
            ins("customers", id=cid, tenant_id=T, name=name, industry=ind, fields_json=json.dumps(fields),
                created_at=ts)
        ins("customers", id="cus_orb1", tenant_id="ten_orbital", name="Private Orbital Customer",
            industry="Aerospace", fields_json="{}", created_at=ts)

        deps = [
            ("dep_northfield", "cus_northfield", "AP exception agent", "adopt", "at_risk", "usr_marcus",
             ["usr_marcus", "usr_maya", "usr_sam", "usr_rosa", "usr_ruth"],
             {"target_golive": "2026-08-18", "tier": "Strategic"}, NORTHFIELD_STAFFING),
            ("dep_harborview", "cus_harborview", "Prior-auth intake agent", "integrate", "on_track", "usr_marcus",
             ["usr_marcus", "usr_jordan"], {"target_golive": "2026-11-30", "tier": "Strategic"}, HARBORVIEW_STAFFING),
            ("dep_castellan", "cus_castellan", "Claims triage copilot", "golive", "blocked", "usr_lena",
             ["usr_lena", "usr_sam", "usr_maya"], {"target_golive": "2026-10-20", "tier": "Standard"}, ""),
            ("dep_redline", "cus_redline", "Carrier dispute pilot", "discover", "on_track", "usr_rosa",
             ["usr_rosa", "usr_sam"], {"tier": "Pilot"}, ""),
        ]
        for did, cid, name, stage, health, lead, members, fields, staffing in deps:
            ins("deployments", id=did, tenant_id=T, customer_id=cid, name=name, stage=stage, health=health,
                lead_id=lead, fields_json=json.dumps(fields), staffing_req=staffing, created_at=ts, updated_at=ts)
            for m in members:
                ins("deployment_members", deployment_id=did, user_id=m)
            ins("stage_events", tenant_id=T, deployment_id=did, from_stage=None, to_stage=stage,
                actor_id="usr_dana", note="imported", at=ts)
            audit.record(conn, T, "usr_dana", "deployment.create", did, {"name": name, "stage": stage})
        ins("deployments", id="dep_orb1", tenant_id="ten_orbital", customer_id="cus_orb1",
            name="Orbital secret project", stage="discover", health="on_track", lead_id="usr_orb",
            fields_json="{}", staffing_req="", created_at=ts, updated_at=ts)

        tasks = [
            ("dep_northfield", "adopt", "Run build-vs-training on invoice exception times", "usr_maya", "in_progress", "2026-09-29", "internal"),
            ("dep_northfield", "adopt", "Walk Harrisburg AP lead through exception queue", "usr_rosa", "open", "2026-10-01", "shared"),
            ("dep_northfield", "adopt", "Confirm 3-way-match tolerance config with customer IT", "usr_sam", "blocked", "2026-09-26", "internal"),
            ("dep_northfield", "adopt", "Monthly value readout with CFO", "usr_marcus", "open", "2026-10-08", "shared"),
            ("dep_northfield", "adopt", "Send September AP exception export", "usr_ruth", "open", "2026-09-30", "shared"),
            ("dep_harborview", "integrate", "Scope FHIR read permissions for intake agent", "usr_jordan", "in_progress", "2026-10-03", "internal"),
            ("dep_harborview", "integrate", "Human approval gate on payer submissions", "usr_jordan", "open", "2026-10-10", "internal"),
            ("dep_castellan", "golive", "Close security review of model gateway", "usr_lena", "blocked", "2026-09-24", "internal"),
            ("dep_castellan", "golive", "Book exec go/no-go", "usr_maya", "open", "2026-10-02", "internal"),
            ("dep_redline", "discover", "Systems inventory with Redline ops", "usr_rosa", "open", "2026-10-06", "internal"),
        ]
        for i, (did, stage, title, who, status, due, vis) in enumerate(tasks, 1):
            tid = f"tsk_{i:03d}"
            ins("tasks", id=tid, tenant_id=T, deployment_id=did, stage=stage, title=title, assignee_id=who,
                status=status, due=due, created_by="usr_marcus", created_at=ts, updated_at=ts, visibility=vis)
            audit.record(conn, T, "usr_marcus", "task.create", tid,
                         {"deployment": did, "assignee": who, "title": title, "visibility": vis})

        # A pushed result from the customer-side latency probe, still internal.
        finding = {"summary": "p95 1840 ms against a 1500 ms SLO · 3.1% errors over 12,480 calls",
                   "status": "fail", "result": {"p95_ms": 1840, "error_rate": 0.031, "calls": 12480,
                                                "slo_p95_ms": 1500}}
        ins("findings", id="fnd_probe1", tenant_id=T, deployment_id="dep_castellan", engine="latency_probe",
            title="Agent latency vs SLO (last 24h)", result_json=json.dumps(finding), confirmed_by=None,
            confirmed_at=None, created_by="engine:latency_probe", created_at=ts, visibility="internal")
        audit.record(conn, T, "engine:latency_probe", "engine.run", "fnd_probe1",
                     {"engine": "latency_probe", "deployment": "dep_castellan",
                      "title": "Agent latency vs SLO (last 24h)"})

        # A confirmed value readout, shared with the Northfield customer.
        value = stages.attribution(NORTHFIELD_VALUE)
        ins("findings", id="fnd_value1", tenant_id=T, deployment_id="dep_northfield", engine="attribution",
            title="Value readout · August", result_json=json.dumps(value), confirmed_by="usr_marcus",
            confirmed_at=ts, created_by="usr_maya", created_at=ts, visibility="shared")
        audit.record(conn, T, "usr_maya", "engine.run", "fnd_value1",
                     {"engine": "attribution", "deployment": "dep_northfield", "title": "Value readout · August"})
        audit.record(conn, T, "usr_marcus", "finding.confirm", "fnd_value1",
                     {"engine": "attribution", "deployment": "dep_northfield"})
        audit.record(conn, T, "usr_marcus", "finding.share", "fnd_value1",
                     {"visibility": "shared", "deployment": "dep_northfield"})
    return DEMO_TOKENS
