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
from datetime import date, datetime, timedelta, timezone

from . import audit, config, crypto, db, ops
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

def value_study_csv(seed: int = 11, effective: str = "2026-07-01", drop: float = 3.1,
                    metric: str = "minutes per claim") -> str:
    """Minutes per claim for adjusters who got the agent (treatment) and a comparison
    group who didn't yet, three weeks either side of the day it went live."""
    rnd = random.Random(seed)
    lines = ["# effective: " + effective, "# metric: " + metric, "# unit: min",
             "# volume_per_month: 9000", "arm,subject,day,value"]
    eff = date.fromisoformat(effective)
    for arm, n, change in (("treatment", 14, -drop), ("control", 12, -0.5)):
        for i in range(1, n + 1):
            base = 12 + rnd.gauss(0, 1.4)
            for k in (-20, -13, -6, 6, 13, 20):
                v = base + (change if k > 0 else 0) + rnd.gauss(0, 0.7)
                lines.append(f"{arm},{arm[0]}{i:02d},{eff + timedelta(days=k)},{v:.2f}")
    return "\n".join(lines)


SAMPLE_INPUTS = {
    "census": REDLINE_CENSUS, "cortex": HARBORVIEW_AUTHORITY, "conformance": CASTELLAN_CONFORMANCE,
    "golive": CASTELLAN_ISSUES, "attribution": NORTHFIELD_VALUE, "readiness": READINESS_CHECKLIST,
    "value_study": value_study_csv(),
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
    return build_demo(conn)


DEMO_TENANTS = ("ten_meridian", "ten_orbital")
# Children before parents, so foreign keys hold on both databases.
_TENANT_TABLES = ("ai_eval_jobs", "ai_cortex_approvals", "ai_authority_decisions", "ai_lessons", "ai_releases", "ai_eval_runs", "ai_versions", "onboarding_plans", "audit_anchors", "milestone_packets", "milestones", "sows", "feedback", "contact_signals", "time_off", "inbound_events", "oauth_states", "connections",
                  "checklist_items", "approvals", "flags", "opportunities", "time_entries", "delays", "reports",
                  "task_links", "outbox", "deployment_stages", "findings", "tasks", "stage_events")


def reseed_demo(conn) -> dict:
    """Reset only the demo workspaces, leaving every real workspace alone (hosted beta + public demo)."""
    ph = ",".join("?" * len(DEMO_TENANTS))
    with db.tx(conn):
        for t in _TENANT_TABLES:
            conn.execute(f"DELETE FROM {t} WHERE tenant_id IN ({ph})", DEMO_TENANTS)
        conn.execute(f"DELETE FROM deployment_members WHERE deployment_id IN (SELECT id FROM deployments"
                     f" WHERE tenant_id IN ({ph}))", DEMO_TENANTS)
        for t in ("deployments", "customers", "personal_tokens", "sessions", "sso_states", "tenant_secrets",
                  "engine_credentials"):
            conn.execute(f"DELETE FROM {t} WHERE tenant_id IN ({ph})", DEMO_TENANTS)
        conn.execute(f"DELETE FROM users WHERE tenant_id IN ({ph})", DEMO_TENANTS)
        conn.execute(f"DELETE FROM audit WHERE tenant_id IN ({ph})", DEMO_TENANTS)
        conn.execute(f"DELETE FROM tenants WHERE id IN ({ph})", DEMO_TENANTS)
    return build_demo(conn)


def build_demo(conn) -> dict:
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
            domain = "northfieldsupply.example" if role == "customer" else "meridian.example"
            ins("users", id=uid, tenant_id=T, name=name, email=f"{name.split()[0].lower()}@{domain}",
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
            ("cus_northfield", "Northfield Supply Co.", "Distribution", {"arr": 420000, "exec_sponsor": "CFO, R. Albrecht"},
             ""),
            ("cus_harborview", "Harborview Health", "Healthcare", {"arr": 610000, "exec_sponsor": "CIO, T. Nakamura"},
             "harborviewhealth.example"),
            ("cus_castellan", "Castellan Mutual", "Insurance", {"arr": 380000}, "castellanmutual.example"),
            ("cus_redline", "Redline Logistics", "Logistics", {"arr": 150000}, ""),
        ]
        for cid, name, ind, fields, domains in customers:
            ins("customers", id=cid, tenant_id=T, name=name, industry=ind, fields_json=json.dumps(fields),
                created_at=ts, domains=domains)
        ins("customers", id="cus_orb1", tenant_id="ten_orbital", name="Private Orbital Customer",
            industry="Aerospace", fields_json="{}", created_at=ts)

        ins("customers", id="cus_keystone", tenant_id=T, name="Keystone Freight", industry="Logistics",
            fields_json=json.dumps({"arr": 240000, "exec_sponsor": "COO, P. Brandt"}), created_at=ts)

        # history: (stage, days ago it was entered), oldest first
        deps = [
            ("dep_northfield", "cus_northfield", "AP exception agent", "at_risk", "usr_marcus",
             {"usr_marcus": .1, "usr_maya": .5, "usr_sam": .5, "usr_rosa": .3, "usr_ruth": 0},
             {"target_golive": D(-40), "tier": "Strategic"}, NORTHFIELD_STAFFING,
             [("discover", 98), ("integrate", 86), ("test", 59), ("golive", 47), ("adopt", 38)],
             (-98, 30, 1150)),
            ("dep_harborview", "cus_harborview", "Prior-auth intake agent", "on_track", "usr_marcus",
             {"usr_marcus": .1, "usr_jordan": .8}, {"target_golive": D(65), "tier": "Strategic"},
             HARBORVIEW_STAFFING, [("discover", 27), ("integrate", 16)], (-27, 95, 260)),
            ("dep_castellan", "cus_castellan", "Claims triage copilot", "blocked", "usr_lena",
             {"usr_lena": .6, "usr_sam": .4, "usr_maya": .5}, {"target_golive": D(4), "tier": "Standard"}, "",
             [("discover", 73), ("integrate", 60), ("test", 32), ("golive", 19)], (-73, 12, 700)),
            ("dep_redline", "cus_redline", "Carrier dispute pilot", "on_track", "usr_rosa",
             {"usr_rosa": .5, "usr_sam": .3}, {"tier": "Pilot"}, "", [("discover", 6)], (-6, 60, 300)),
            ("dep_keystone", "cus_keystone", "Freight audit agent", "on_track", "usr_lena",
             {"usr_lena": 0, "usr_marcus": 0}, {"tier": "Standard"}, "",
             [("discover", 158), ("integrate", 145), ("test", 121), ("golive", 109), ("adopt", 101),
              ("value", 72)], (-158, -20, 1000)),
        ]
        for did, cid, name, health, lead, members, fields, staffing, history, (s_on, e_on, budget) in deps:
            opened = TS(history[0][1])
            ins("deployments", id=did, tenant_id=T, customer_id=cid, name=name, stage=history[-1][0],
                health=health, lead_id=lead, fields_json=json.dumps(fields), staffing_req=staffing,
                created_at=opened, updated_at=TS(history[-1][1]), start_on=D(s_on), end_on=D(e_on),
                budget_hours=budget)
            for m, alloc in members.items():
                ins("deployment_members", deployment_id=did, user_id=m, allocation=alloc)
            prev = None
            for stage, ago in history:
                ins("stage_events", tenant_id=T, deployment_id=did, from_stage=prev, to_stage=stage,
                    actor_id="usr_dana" if prev is None else lead, note="opened" if prev is None else "",
                    at=TS(ago))
                prev = stage
            audit.record(conn, T, "usr_dana", "deployment.create", did, {"name": name, "stage": history[-1][0]})
        ins("deployments", id="dep_orb1", tenant_id="ten_orbital", customer_id="cus_orb1",
            name="Orbital secret project", stage="discover", health="on_track", lead_id="usr_orb",
            fields_json="{}", staffing_req="", created_at=ts, updated_at=ts)

        tasks = [
            ("dep_northfield", "adopt", "Run build-vs-training on invoice exception times", "usr_maya", "in_progress", 3, "internal"),
            ("dep_northfield", "adopt", "Walk Harrisburg AP lead through exception queue", "usr_rosa", "open", 5, "shared"),
            ("dep_northfield", "adopt", "Confirm 3-way-match tolerance config with customer IT", "usr_sam", "blocked", 0, "internal"),
            ("dep_northfield", "adopt", "Monthly value readout with CFO", "usr_marcus", "open", 12, "shared"),
            ("dep_northfield", "adopt", "Send September AP exception export", "usr_ruth", "open", 4, "shared"),
            ("dep_harborview", "integrate", "Scope FHIR read permissions for intake agent", "usr_jordan", "in_progress", 7, "internal"),
            ("dep_harborview", "integrate", "Human approval gate on payer submissions", "usr_jordan", "open", 14, "internal"),
            ("dep_castellan", "golive", "Close security review of model gateway", "usr_lena", "blocked", -2, "internal"),
            ("dep_castellan", "golive", "Book exec go/no-go", "usr_maya", "open", 6, "internal"),
            ("dep_redline", "discover", "Systems inventory with Redline ops", "usr_rosa", "open", 10, "internal"),
            ("dep_harborview", "integrate", "Draft payer test matrix", None, "open", 9, "internal"),
            ("dep_castellan", "golive", "Hypercare rota for week two", None, "open", 8, "internal"),
            ("dep_castellan", "golive", "Fix fraud-flag miss on F-03 and rerun conformance", "usr_sam", "in_progress", 2, "internal"),
        ]
        blocked_since = {"tsk_003": 5, "tsk_008": 12}
        for i, (did, stage, title, who, status, due, vis) in enumerate(tasks, 1):
            tid = f"tsk_{i:03d}"
            made = TS(blocked_since.get(tid, 0) + 3)
            ins("tasks", id=tid, tenant_id=T, deployment_id=did, stage=stage, title=title, assignee_id=who,
                status=status, due=D(due), created_by="usr_marcus", created_at=made,
                updated_at=TS(blocked_since[tid]) if tid in blocked_since else made, visibility=vis)
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
        seed_operations(conn, T, ts)
    ops.sweep(conn, T)
    return DEMO_TOKENS


def D(days: int) -> str:
    """A date relative to today, so the demo never goes stale."""
    return (datetime.now(timezone.utc).date() + timedelta(days=days)).isoformat()


def TS(days_ago: float) -> str:
    return ops.iso(datetime.now(timezone.utc) - timedelta(days=days_ago))


def seed_operations(conn, T: str, ts: str) -> None:
    """Hours, time sheets, pipeline, delay history, flags, approvals, checklist, value study."""
    def ins(table: str, **cols):
        conn.execute(f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                     tuple(cols.values()))
    rnd = random.Random(42)
    conn.execute("UPDATE users SET weekly_hours=0 WHERE id IN ('usr_dana', 'usr_ruth')")

    # Time off (as a calendar would bring in) and the email signal (as opted-in mailboxes would).
    today = datetime.now(timezone.utc).date()
    this_week = ops.week_start(today)
    far = this_week + timedelta(weeks=6)
    ins("time_off", id="off_rosa1", tenant_id=T, user_id="usr_rosa", start_on=(far).isoformat(),
        end_on=(far + timedelta(days=4)).isoformat(), source="google_calendar", external_id=None, connection_id=None,
        title="Out of office", created_at=ts)
    now_ = datetime.now(timezone.utc)
    for i, (cust, dom, direction, days_ago) in enumerate([
            ("cus_northfield", "northfieldsupply.example", "in", 2.2), ("cus_northfield", "northfieldsupply.example", "out", 0.8),
            ("cus_harborview", "harborviewhealth.example", "in", 4.1), ("cus_harborview", "harborviewhealth.example", "out", 1.5),
            ("cus_castellan", "castellanmutual.example", "in", 6.0), ("cus_castellan", "castellanmutual.example", "out", 0.3)]):
        ins("contact_signals", tenant_id=T, user_id="usr_marcus", connection_id="seed", customer_id=cust, domain=dom,
            direction=direction, at=(now_ - timedelta(days=days_ago)).isoformat(timespec="seconds"), external_id=f"seed{i}")

    # Time sheets: weekly totals for past weeks, day by day this week.
    members = conn.execute("SELECT m.deployment_id, m.user_id, m.allocation, d.start_on, d.end_on"
                           " FROM deployment_members m JOIN deployments d ON d.id=m.deployment_id"
                           " WHERE d.tenant_id=?", (T,)).fetchall()
    hist_alloc = {("dep_keystone", "usr_lena"): .6, ("dep_keystone", "usr_marcus"): .1}
    for m in members:
        alloc = m["allocation"] or hist_alloc.get((m["deployment_id"], m["user_id"]), 0)
        if not alloc:
            continue
        start, end = date.fromisoformat(m["start_on"]), min(date.fromisoformat(m["end_on"]), today)
        ws = ops.week_start(start)
        while ws < this_week and ws <= end:
            ins("time_entries", tenant_id=T, user_id=m["user_id"], deployment_id=m["deployment_id"],
                day=(ws + timedelta(days=2)).isoformat(), hours=round(alloc * 40 * rnd.uniform(.82, 1.08), 1),
                source="import", created_at=ts)
            ws += timedelta(weeks=1)
        day = this_week
        while day < today and day.weekday() < 5 and day <= end:
            ins("time_entries", tenant_id=T, user_id=m["user_id"], deployment_id=m["deployment_id"],
                day=day.isoformat(), hours=round(alloc * 8 * rnd.uniform(.8, 1.15), 1), source="console",
                created_at=ts)
            day += timedelta(days=1)
    # Harborview spent ahead of plan (the discovery ran long, off-contract)
    ins("time_entries", tenant_id=T, user_id="usr_jordan", deployment_id="dep_harborview", day=D(-30),
        hours=24, source="import", created_at=ts)

    # Pipeline from the CRM
    opps = [
        ("Northfield · Order-to-cash agent", "Northfield Supply Co.", "Order-to-cash exceptions", 280000, .6, "commit", 21, 40),
        ("Cobalt Energy · Field ticket agent", "Cobalt Energy", "Field ticket reconciliation", 240000, .5, "commit", 21, 90),
        ("Harborview · Denials appeal agent", "Harborview Health", "Claim denial appeals", 350000, .35, "proposal", 45, 60),
        ("Pinecrest Bank · KYC review copilot", "Pinecrest Bank", "KYC file review", 520000, .2, "qualified", 70, 80),
        ("Atlas Freight · Carrier onboarding agent", "Atlas Freight", "Carrier onboarding", 180000, .1, "lead", 90, 30),
        ("Keystone Freight · Freight audit agent", "Keystone Freight", "Freight audit", 240000, 1, "won", -170, 30),
        ("Vireo Retail · Returns agent", "Vireo Retail", "Returns triage", 150000, 0, "lost", None, 30),
    ]
    for i, (name, cust, uc, val, p, st, start, hrs) in enumerate(opps, 1):
        ins("opportunities", id=f"opp_{i:02d}", tenant_id=T, name=name, customer=cust, use_case=uc, value=val,
            probability=p, stage=st, expected_start=D(start) if start is not None else None, weekly_hours=hrs,
            source="import", external_id=f"crm-{1000 + i}", updated_at=ts,
            deployment_id="dep_keystone" if st == "won" else None)

    # Delay history. Keystone's closed deployment taught the workspace that "blocked"
    # tasks here usually sit with the customer, so new ones are proposed that way.
    def span(sid, dep, stage, signal, started, days, proposed, reason, confirmed=None, by="usr_marcus",
             weight=1.0, status=None):
        ins("delays", id=sid, tenant_id=T, deployment_id=dep, stage=stage, signal=signal, started_at=TS(started),
            ended_at=TS(started - days) if days is not None else None, proposed_owner=proposed,
            proposed_reason=reason, proposal_basis=f"default for {signal.replace('_', ' ')}", evidence="",
            dedupe_key=sid, status=status or ("open" if confirmed is None else
                                              ("confirmed" if confirmed == proposed else "reassigned")),
            confirmed_owner=confirmed, confirmed_reason=reason if confirmed else None,
            confirmed_by=by if confirmed else None, confirmed_at=TS(started - (days or 0)) if confirmed else None,
            weight=weight if confirmed else 0, created_at=TS(started))
    ks = [("integrate", 142, 5, "customer", "Waiting on customer VPN access"),
          ("integrate", 134, 3, "customer", "Customer API keys not issued"),
          ("test", 119, 4, "customer", "Customer test data extract late"),
          ("test", 113, 2, "team", "Our fixture loader broke"),
          ("golive", 108, 3, "customer", "Customer change freeze"),
          ("adopt", 99, 2, "team", "Training deck rework"),
          ("adopt", 92, 4, "customer", "Supervisors unavailable for training")]
    for i, (stage, ago, days, owner, why) in enumerate(ks, 1):
        span(f"dly_ks{i}", "dep_keystone", stage, "task_blocked", ago, days, "team", why, confirmed=owner,
             by="usr_lena")
    span("dly_ks8", "dep_keystone", "integrate", "security_review", 144, 9, "customer",
         "Customer security review", confirmed="customer", by="usr_lena")
    span("dly_ks9", "dep_keystone", "golive", "model_access", 107, 3, "model_vendor",
         "Model access or rate limits", confirmed="model_vendor", by="rule", weight=0, status="deduced")
    span("dly_nf1", "dep_northfield", "integrate", "security_review", 82, 9, "customer",
         "Customer security review of the model gateway", confirmed="customer")
    span("dly_nf2", "dep_northfield", "integrate", "manual", 70, 4, "team", "NetSuite connector rework",
         confirmed="team")
    span("dly_nf3", "dep_northfield", "test", "vendor_error", 55, 3, "software_vendor",
         "NetSuite sandbox refresh window", confirmed="software_vendor")
    span("dly_ca1", "dep_castellan", "test", "change_board", 30, 6, "customer", "Customer change board",
         confirmed="customer", by="usr_lena")
    span("dly_ca2", "dep_castellan", "test", "task_blocked", 25, 5, "team", "Fraud model threshold rework",
         confirmed="team", by="usr_lena")

    tenant = conn.execute("SELECT * FROM tenants WHERE id=?", (T,)).fetchone()
    cfg = config.upgrade(json.loads(tenant["config_json"]))
    deps = {r["id"]: r for r in conn.execute("SELECT * FROM deployments WHERE tenant_id=?", (T,))}
    for tid, ago in (("tsk_003", 5), ("tsk_008", 12)):
        t = conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
        ops.open_span(conn, T, deps[t["deployment_id"]], signal="task_blocked", dedupe_key=f"task:{tid}",
                      evidence=f"“{t['title']}” marked blocked", started_at=datetime.now(timezone.utc) - timedelta(days=ago),
                      tenant_name=tenant["name"], stage=t["stage"])
    ops.open_span(conn, T, deps["dep_harborview"], signal="waiting_on_customer", dedupe_key="manual_hv1",
                  evidence="Asked Harborview IT for payer sandbox credentials; no answer yet",
                  started_at=datetime.now(timezone.utc) - timedelta(days=4), tenant_name=tenant["name"],
                  reason="Payer sandbox credentials from Harborview IT")
    ops.open_span(conn, T, deps["dep_castellan"], signal="model_access", dedupe_key="manual_ca_rl",
                  evidence="Model provider returned 429s during the go-live load test",
                  started_at=datetime.now(timezone.utc) - timedelta(days=3), tenant_name=tenant["name"])

    # Deployments don't move in a straight line: Harborview started testing while integration
    # is still open, and Redline is paused while the customer reorganizes.
    deps = {r["id"]: r for r in conn.execute("SELECT * FROM deployments WHERE tenant_id=?", (T,))}
    ops.apply_states(conn, T, cfg, deps["dep_harborview"], {"test": "in_progress"}, "usr_jordan")
    conn.execute("UPDATE deployment_stages SET entered_at=? WHERE deployment_id='dep_harborview' AND stage='test'",
                 (TS(5),))
    conn.execute("UPDATE stage_events SET at=? WHERE deployment_id='dep_harborview' AND to_stage='test'", (TS(5),))
    conn.execute("UPDATE deployments SET hold_since=?, hold_reason=? WHERE id='dep_redline'",
                 (TS(3), "Customer reorg; waiting on their new ops lead"))
    ops.open_span(conn, T, deps["dep_redline"], signal="on_hold", dedupe_key="hold:dep_redline",
                  evidence="Customer reorg; waiting on their new ops lead", started_at=datetime.now(timezone.utc) - timedelta(days=3),
                  tenant_name=tenant["name"], reason="Customer reorg; waiting on their new ops lead")
    conn.execute("UPDATE tasks SET waiting_on='customer', blocked_reason='Customer IT owns the tolerance setting'"
                 " WHERE id='tsk_003'")

    # Flags raised by people (the sweep adds the rule flags)
    ins("flags", id="flg_ca_sponsor", tenant_id=T, deployment_id="dep_castellan", severity="high",
        text="Exec sponsor hasn't confirmed the go/no-go; go-live date at risk", rule_key=None,
        raised_by="usr_lena", status="open", created_at=TS(2))
    ins("flags", id="flg_nf_site", tenant_id=T, deployment_id="dep_northfield", severity="med",
        text="Harrisburg adoption trailing the other two sites", rule_key=None, raised_by="usr_rosa",
        status="returned", handled_by="usr_dana", handled_at=TS(1),
        note="Plan a site visit with the Harrisburg AP lead before the CFO readout", created_at=TS(4))

    # Agent actions waiting for a person
    ins("approvals", id="apr_hv1", tenant_id=T, deployment_id="dep_harborview", agent="Intake agent",
        request="Write intake notes to the Epic sandbox", detail="42 notes from the prior-auth test set, sandbox only",
        requested_by="usr_jordan", status="pending", created_at=TS(.3))
    ins("approvals", id="apr_ca1", tenant_id=T, deployment_id="dep_castellan", agent="Claims triage agent",
        request="Re-route 214 misrouted claims in production",
        detail="Claims opened since the routing fix; each move is logged with the old and new queue",
        requested_by="usr_lena", status="pending", created_at=TS(.8))
    ins("approvals", id="apr_nf1", tenant_id=T, deployment_id="dep_northfield", agent="AP exception agent",
        request="Auto-close duplicate exceptions under $50", detail="", requested_by="usr_maya",
        status="approved", decided_by="usr_marcus", decided_at=TS(6), created_at=TS(7))

    # Go-live checklist on Castellan
    for i, label in enumerate(ops.CHECKLIST_DEFAULT, 1):
        done = i in (1, 3, 6)
        ins("checklist_items", id=f"chk_ca{i}", tenant_id=T, deployment_id="dep_castellan", label=label,
            position=i, done=1 if done else 0, done_by="usr_lena" if done else None,
            done_at=TS(3) if done else None, created_at=TS(19))

    # A failing conformance run on Castellan and a confirmed value study on Keystone
    conf = stages.conformance(CASTELLAN_CONFORMANCE)
    ins("findings", id="fnd_conf1", tenant_id=T, deployment_id="dep_castellan", engine="conformance",
        title="Conformance · claims routing suite", result_json=json.dumps(conf), confirmed_by="usr_lena",
        confirmed_at=TS(20), created_by="usr_sam", created_at=TS(21), visibility="internal")
    vs = stages.value_study(value_study_csv(effective=D(-50), metric="minutes per freight audit"))
    ins("findings", id="fnd_vs1", tenant_id=T, deployment_id="dep_keystone", engine="value_study",
        title="Value study · minutes per freight audit", result_json=json.dumps(vs), confirmed_by="usr_marcus",
        confirmed_at=TS(10), created_by="usr_lena", created_at=TS(12), visibility="internal")

    # One result on each stage page so none of them opens empty
    from .engines import run_sendero, parse_csv
    more = [
        ("fnd_census1", "dep_redline", "census", "Systems census · Redline", stages.census(REDLINE_CENSUS),
         "usr_rosa", 2, "usr_marcus", 1),
        ("fnd_cortex1", "dep_harborview", "cortex", "Authority check · intake agent", stages.authority(HARBORVIEW_AUTHORITY),
         "usr_jordan", 1, None, None),
        ("fnd_sendero1", "dep_northfield", "sendero", "Invoice exception handling time",
         run_sendero(parse_csv(northfield_adoption_csv()), "minutes_per_exception", 6), "usr_maya", 6, "usr_marcus", 5),
        ("fnd_golive1", "dep_castellan", "golive", "Command center · launch week", stages.command_center(CASTELLAN_ISSUES),
         "usr_sam", 3, None, None),
    ]
    for fid, dep, eng, title, res, by, ago, conf, conf_ago in more:
        ins("findings", id=fid, tenant_id=T, deployment_id=dep, engine=eng, title=title, result_json=json.dumps(res),
            confirmed_by=conf, confirmed_at=TS(conf_ago) if conf else None, created_by=by, created_at=TS(ago),
            visibility="internal")
    seed_commercials(conn, T, cfg)


# (name, stage or None, amount, status, days ago submitted, days ago decided, invoice ref, days ago invoiced, paid days ago)
NORTHFIELD_SOW = [
    ("Discovery and solution design signed off", "discover", 60000, "paid", 88, 86, "INV-10231", 85, 60),
    ("Integrations complete (NetSuite, AP inbox)", "integrate", 90000, "paid", 61, 59, "INV-10302", 58, 31),
    ("UAT passed on customer data", "test", 90000, "invoiced", 49, 48, "INV-10388", 46, None),
    ("Go-live across three sites", "golive", 120000, "accepted", 5, 3, None, None, None),
    ("Adoption plan approved", None, 30000, "submitted", 1, None, None, None, None),
    ("Value readout accepted by the CFO", "value", 30000, "pending", None, None, None, None, None),
]
HARBORVIEW_SOW = [
    ("Discovery report", "discover", 75000, "ready", None, None, None, None, None),
    ("Integration build complete", "integrate", 150000, "pending", None, None, None, None, None),
    ("Pilot passed with payer test set", "test", 150000, "pending", None, None, None, None, None),
    ("Production go-live", "golive", 235000, "pending", None, None, None, None, None),
]
KEYSTONE_SOW = [
    ("Discovery", "discover", 30000, "paid", 146, 145, "KF-2201", 144, 120),
    ("Integration and test", "test", 90000, "paid", 110, 108, "KF-2240", 107, 80),
    ("Go-live", "golive", 80000, "paid", 102, 100, "KF-2262", 99, 70),
    ("Value study confirmed", "value", 40000, "accepted", 12, 9, None, None, None),
]


def seed_commercials(conn, T: str, cfg: dict) -> None:
    """Statements of work and billable milestones, with real evidence packets and sign-offs on the record."""
    from . import sow as sow_mod
    real_now = audit.now
    criteria = {s["key"]: s.get("exit_criteria", "") for s in cfg["stages"]}
    signer = {"dep_northfield": "usr_ruth", "dep_harborview": None, "dep_keystone": "usr_lena"}
    plans = [("sow_nf1", "dep_northfield", "NSC-SOW-2026-014", 420000, 100, NORTHFIELD_SOW),
             ("sow_hv1", "dep_harborview", "HVH-SOW-3", 610000, 28, HARBORVIEW_SOW),
             ("sow_ks1", "dep_keystone", "KF-SOW-07", 240000, 160, KEYSTONE_SOW)]
    for tid, title, who, ago in (("tsk_nf_plan", "Draft the site-by-site adoption plan (Harrisburg, Allentown, Reading)", "usr_rosa", 2),
                                 ("tsk_nf_train", "Train AP leads at all three sites on the exception queue", "usr_rosa", 4)):
        conn.execute("INSERT INTO tasks (id, tenant_id, deployment_id, stage, title, assignee_id, status, created_by,"
                     " created_at, updated_at, visibility) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                     (tid, T, "dep_northfield", "adopt", title, who, "done", "usr_marcus", TS(ago + 10), TS(ago), "shared"))
    try:
        for sid, dep, ref, total, signed_ago, rows in plans:
            conn.execute("INSERT INTO sows (id, tenant_id, deployment_id, reference, total_value, currency, signed_on,"
                         " notes, created_by, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                         (sid, T, dep, ref, total, "USD", D(-signed_ago), "", "usr_marcus", TS(signed_ago),
                          TS(signed_ago)))
            for i, (name, stage, amount, status, sub, dec, inv, inv_ago, paid_ago) in enumerate(rows):
                mid = f"ms_{sid[4:]}_{i + 1}"
                conn.execute("INSERT INTO milestones (id, tenant_id, deployment_id, sow_id, name, amount, stage, criteria,"
                             " position, status, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                             (mid, T, dep, sid, name, amount, stage,
                              criteria.get(stage, "") if stage else "CFO signs the site-by-site adoption plan", i,
                              "pending", TS(signed_ago), TS(signed_ago)))
                if status == "ready":
                    conn.execute("UPDATE milestones SET status='ready', ready_at=? WHERE id=?", (TS(2), mid))
                if sub is None:
                    continue
                ms = conn.execute("SELECT * FROM milestones WHERE id=?", (mid,)).fetchone()
                audit.now = lambda t=TS(sub): t
                sow_mod.freeze(conn, T, cfg, ms, "usr_marcus", "", "milestone.submit")
                conn.execute("UPDATE milestones SET status='submitted', submitted_at=?, submitted_by='usr_marcus'"
                             " WHERE id=?", (TS(sub), mid))
                if dec is None:
                    continue
                audit.now = lambda t=TS(dec): t
                pk = sow_mod.current_packet(conn, mid)
                who = signer[dep]
                proxy = who != "usr_ruth"
                note = "" if not proxy else "Signed off by email; PDF on the SharePoint deal folder"
                audit.record(conn, T, who if not proxy else "usr_lena", "milestone.accept", mid,
                             {"deployment": dep, "amount": amount, "customer_packet": pk["customer_hash"], "note": note,
                              "on_behalf_of_customer": proxy})
                sow_mod.anchor(conn, T, "milestone.accept", mid)
                conn.execute("UPDATE milestones SET status='accepted', decided_at=?, decided_by=?, decision_note=?,"
                             " proxy=? WHERE id=?", (TS(dec), who or "usr_lena", note, int(proxy), mid))
                if inv:
                    audit.now = lambda t=TS(inv_ago): t
                    audit.record(conn, T, "erp:netsuite" if dep == "dep_northfield" else "usr_lena", "milestone.invoiced",
                                 mid, {"deployment": dep, "amount": amount, "invoice_ref": inv, "on": D(-inv_ago)})
                    conn.execute("UPDATE milestones SET status='invoiced', invoice_ref=?, invoiced_at=? WHERE id=?",
                                 (inv, D(-inv_ago), mid))
                if paid_ago is not None:
                    audit.now = lambda t=TS(paid_ago): t
                    audit.record(conn, T, "erp:netsuite" if dep == "dep_northfield" else "usr_lena", "milestone.paid",
                                 mid, {"deployment": dep, "amount": amount, "invoice_ref": inv, "on": D(-paid_ago)})
                    conn.execute("UPDATE milestones SET status='paid', paid_at=? WHERE id=?", (D(-paid_ago), mid))
    finally:
        audit.now = real_now

