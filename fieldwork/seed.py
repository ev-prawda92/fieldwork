"""Demo workspace: Meridian AI's deployment team running four customer deployments.

All names are fictional. The team is mixed the way real deployment orgs are:
a head of deployments, an engagement manager, an implementation consultant,
FDEs and AI engineers, plus one customer stakeholder with a read-only view.

The workspace has also made the default template its own, as a customer would:
white-labeled as "Meridian Deploy", its own go-live readiness script plugged in
as a webhook engine on the Go-live stage, and a latency probe that pushes
results in from inside a customer's environment.

A second tenant (Orbital Labs) exists so the demo and tests can show that
workspaces are isolated.

    python -m fieldwork seed
"""

from __future__ import annotations

import json
import random

from . import audit, config, db
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
DEMO_ENGINE_URL = "http://127.0.0.1:8787/"

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
         "url": DEMO_ENGINE_URL},
        {"key": "latency_probe", "kind": "push", "name": "Latency probe",
         "does": "Runs inside the customer's environment and posts agent latency against SLO."},
    ]
    for s in cfg["stages"]:
        if s["key"] == "golive":
            s["engine"] = "readiness"
    return config.validate(cfg, allow_http_engines=True)


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


def seed(db_file: str) -> dict:
    conn = db.connect(db_file)
    db.init(conn)
    ts = audit.now()
    T = "ten_meridian"

    with db.tx(conn):
        conn.execute("INSERT INTO tenants VALUES (?,?,?,?)", (T, "Meridian AI", json.dumps(meridian_config()), ts))
        conn.execute("INSERT INTO tenants VALUES (?,?,?,?)",
                     ("ten_orbital", "Orbital Labs", json.dumps(config.default()), ts))

        for i, (uid, name, role, tkey, prof) in enumerate(PEOPLE):
            tok = DEMO_TOKENS[tkey] if tkey else f"demo-{uid}-meridian"
            conn.execute("INSERT INTO users VALUES (?,?,?,?,?,?,?,?,?)",
                         (uid, T, name, f"{name.split()[0].lower()}@meridian.example", role, None,
                          token_hash(tok), json.dumps(prof), ts))
        conn.execute("INSERT INTO users VALUES (?,?,?,?,?,?,?,?,?)",
                     ("usr_orb", "ten_orbital", "Ines Duarte", "ines@orbital.example", "head", None,
                      token_hash(DEMO_TOKENS["other_tenant"]), "{}", ts))

        conn.execute("INSERT INTO engine_credentials VALUES (?,?,?,?,?)",
                     (T, "readiness", DEMO_ENGINE_SECRET, None, ts))
        conn.execute("INSERT INTO engine_credentials VALUES (?,?,?,?,?)",
                     (T, "latency_probe", None, token_hash(DEMO_PUSH_TOKEN), ts))

        customers = [
            ("cus_northfield", "Northfield Supply Co.", "Distribution", {"arr": 420000, "exec_sponsor": "CFO, R. Albrecht"}),
            ("cus_harborview", "Harborview Health", "Healthcare", {"arr": 610000, "exec_sponsor": "CIO, T. Nakamura"}),
            ("cus_castellan", "Castellan Mutual", "Insurance", {"arr": 380000}),
            ("cus_redline", "Redline Logistics", "Logistics", {"arr": 150000}),
        ]
        for cid, name, ind, fields in customers:
            conn.execute("INSERT INTO customers VALUES (?,?,?,?,?,?)", (cid, T, name, ind, json.dumps(fields), ts))
        conn.execute("INSERT INTO customers VALUES (?,?,?,?,?,?)",
                     ("cus_orb1", "ten_orbital", "Private Orbital Customer", "Aerospace", "{}", ts))

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
            conn.execute("INSERT INTO deployments VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                         (did, T, cid, name, stage, health, lead, json.dumps(fields), staffing, ts, ts))
            for m in members:
                conn.execute("INSERT INTO deployment_members VALUES (?,?)", (did, m))
            conn.execute("INSERT INTO stage_events (tenant_id, deployment_id, from_stage, to_stage,"
                         " actor_id, note, at) VALUES (?,?,?,?,?,?,?)",
                         (T, did, None, stage, "usr_dana", "imported", ts))
            audit.record(conn, T, "usr_dana", "deployment.create", did, {"name": name, "stage": stage})
        conn.execute("INSERT INTO deployments VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                     ("dep_orb1", "ten_orbital", "cus_orb1", "Orbital secret project", "discover",
                      "on_track", "usr_orb", "{}", "", ts, ts))

        tasks = [
            ("dep_northfield", "adopt", "Run build-vs-training on invoice exception times", "usr_maya", "in_progress", "2026-09-29"),
            ("dep_northfield", "adopt", "Walk Harrisburg AP lead through exception queue", "usr_rosa", "open", "2026-10-01"),
            ("dep_northfield", "adopt", "Confirm 3-way-match tolerance config with customer IT", "usr_sam", "blocked", "2026-09-26"),
            ("dep_northfield", "adopt", "Monthly value readout with CFO", "usr_marcus", "open", "2026-10-08"),
            ("dep_harborview", "integrate", "Scope FHIR read permissions for intake agent", "usr_jordan", "in_progress", "2026-10-03"),
            ("dep_harborview", "integrate", "Human approval gate on payer submissions", "usr_jordan", "open", "2026-10-10"),
            ("dep_castellan", "golive", "Close security review of model gateway", "usr_lena", "blocked", "2026-09-24"),
            ("dep_castellan", "golive", "Book exec go/no-go", "usr_maya", "open", "2026-10-02"),
            ("dep_redline", "discover", "Systems inventory with Redline ops", "usr_rosa", "open", "2026-10-06"),
        ]
        for i, (did, stage, title, who, status, due) in enumerate(tasks, 1):
            tid = f"tsk_{i:03d}"
            conn.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                         (tid, T, did, stage, title, who, status, due, "usr_marcus", ts, ts))
            audit.record(conn, T, "usr_marcus", "task.create", tid,
                         {"deployment": did, "assignee": who, "title": title})

        # One result already pushed in by the customer-side latency probe.
        result = {"p95_ms": 1840, "error_rate": 0.031, "calls": 12480, "slo_p95_ms": 1500}
        finding = {"summary": "p95 1840 ms against a 1500 ms SLO · 3.1% errors over 12,480 calls",
                   "status": "fail", "result": result}
        conn.execute("INSERT INTO findings VALUES (?,?,?,?,?,?,?,?,?,?)",
                     ("fnd_probe1", T, "dep_castellan", "latency_probe", "Agent latency vs SLO (last 24h)",
                      json.dumps(finding), None, None, "engine:latency_probe", ts))
        audit.record(conn, T, "engine:latency_probe", "engine.run", "fnd_probe1",
                     {"engine": "latency_probe", "deployment": "dep_castellan",
                      "title": "Agent latency vs SLO (last 24h)"})
    return DEMO_TOKENS
