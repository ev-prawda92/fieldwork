"""Demo workspace: Meridian AI's FDE org running four customer deployments.

All names are fictional. A second tenant (Orbital Labs) exists only so the
demo and tests can show that workspaces are isolated.

    python -m fieldwork seed          # writes fieldwork.db and prints demo tokens
"""

from __future__ import annotations

import json
import random

from . import audit, config, db
from .app import token_hash

DEMO_TOKENS = {
    "director": "demo-director-meridian",
    "manager": "demo-manager-meridian",
    "fde": "demo-fde-meridian",
    "fde2": "demo-fde2-meridian",
    "other_tenant": "demo-director-orbital",
}

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

FDE_PROFILES = {
    "Maya Chen": {
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
        ],
    },
    "Jordan Reyes": {
        "location": "Chicago, IL",
        "evidence": [
            {"id": "emp.jordan.1", "kind": "employment", "org": "Lakeshore Health", "title": "Integration Analyst",
             "start": "2021-01", "end": "2024-06",
             "claims": ["Built HL7 and FHIR interfaces into Epic", "HIPAA training and audits"],
             "tags": ["hl7", "fhir", "epic", "hipaa", "healthcare"]},
            {"id": "emp.jordan.2", "kind": "employment", "org": "Meridian AI", "title": "Forward Deployed Engineer",
             "start": "2024-07", "end": "2026-09", "claims": ["Python services for clinical agents"],
             "tags": ["python", "sql"]},
        ],
    },
    "Sam Okoro": {
        "location": "Pittsburgh, PA",
        "evidence": [
            {"id": "emp.sam.1", "kind": "employment", "org": "Meridian AI", "title": "Forward Deployed Engineer",
             "start": "2025-06", "end": "2026-09",
             "claims": ["Python agent tooling, SQL reporting"], "tags": ["python", "sql"]},
        ],
        "absent": ["netsuite"],
    },
    "Lena Park": {
        "location": "New York, NY",
        "evidence": [
            {"id": "emp.lena.1", "kind": "employment", "org": "Keel Systems", "title": "Backend Engineer",
             "start": "2019-03", "end": "2023-08",
             "claims": ["Python and SQL services on AWS", "ERP integrations including NetSuite"],
             "tags": ["python", "sql", "aws", "netsuite"]},
            {"id": "emp.lena.2", "kind": "employment", "org": "Meridian AI", "title": "Forward Deployed Engineer",
             "start": "2023-09", "end": "2026-09", "claims": ["Customer deployments in finance ops"],
             "tags": ["python"]},
        ],
    },
}


def northfield_adoption_csv(seed: int = 7) -> str:
    """Minutes per invoice exception for AP clerks at three Northfield sites.

    Built so the Harrisburg site is uniformly slow (a configuration problem)
    while new clerks everywhere trail experienced ones a little (some training).
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

    def tenant(tid, name):
        conn.execute("INSERT INTO tenants VALUES (?,?,?,?)",
                     (tid, name, json.dumps(config.default()), ts))

    def user(uid, tid, name, email, role, token, manager=None, profile=None):
        conn.execute("INSERT INTO users VALUES (?,?,?,?,?,?,?,?,?)",
                     (uid, tid, name, email, role, manager, token_hash(token),
                      json.dumps(profile or {}), ts))

    with db.tx(conn):
        tenant("ten_meridian", "Meridian AI")
        tenant("ten_orbital", "Orbital Labs")

        user("usr_dana", "ten_meridian", "Dana Whitfield", "dana@meridian.example", "director",
             DEMO_TOKENS["director"])
        user("usr_marcus", "ten_meridian", "Marcus Hale", "marcus@meridian.example", "manager",
             DEMO_TOKENS["manager"], manager="usr_dana")
        fde_ids = {}
        for i, (name, prof) in enumerate(FDE_PROFILES.items()):
            uid = "usr_" + name.split()[0].lower()
            fde_ids[name] = uid
            tok = DEMO_TOKENS["fde"] if i == 0 else (DEMO_TOKENS["fde2"] if i == 1 else f"demo-fde{i+1}-meridian")
            user(uid, "ten_meridian", name, f"{name.split()[0].lower()}@meridian.example", "fde",
                 tok, manager="usr_marcus", profile=prof)
        user("usr_orb", "ten_orbital", "Ines Duarte", "ines@orbital.example", "director",
             DEMO_TOKENS["other_tenant"])

        customers = [
            ("cus_northfield", "Northfield Supply Co.", "Distribution", {"arr": 420000, "exec_sponsor": "CFO, R. Albrecht"}),
            ("cus_harborview", "Harborview Health", "Healthcare", {"arr": 610000, "exec_sponsor": "CIO, T. Nakamura"}),
            ("cus_castellan", "Castellan Mutual", "Insurance", {"arr": 380000}),
            ("cus_redline", "Redline Logistics", "Logistics", {"arr": 150000}),
        ]
        for cid, name, ind, fields in customers:
            conn.execute("INSERT INTO customers VALUES (?,?,?,?,?,?)",
                         (cid, "ten_meridian", name, ind, json.dumps(fields), ts))
        conn.execute("INSERT INTO customers VALUES (?,?,?,?,?,?)",
                     ("cus_orb1", "ten_orbital", "Private Orbital Customer", "Aerospace", "{}", ts))

        deps = [
            ("dep_northfield", "cus_northfield", "AP exception agent", "adopt", "at_risk", "usr_maya",
             ["usr_maya", "usr_sam"], {"target_golive": "2026-08-18", "tier": "Strategic"}, NORTHFIELD_STAFFING),
            ("dep_harborview", "cus_harborview", "Prior-auth intake agent", "integrate", "on_track", "usr_jordan",
             ["usr_jordan"], {"target_golive": "2026-11-30", "tier": "Strategic"}, HARBORVIEW_STAFFING),
            ("dep_castellan", "cus_castellan", "Claims triage copilot", "test", "blocked", "usr_lena",
             ["usr_lena", "usr_sam"], {"target_golive": "2026-10-20", "tier": "Standard"}, ""),
            ("dep_redline", "cus_redline", "Carrier dispute pilot", "discover", "on_track", "usr_sam",
             ["usr_sam"], {"tier": "Pilot"}, ""),
        ]
        for did, cid, name, stage, health, lead, members, fields, staffing in deps:
            conn.execute("INSERT INTO deployments VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                         (did, "ten_meridian", cid, name, stage, health, lead,
                          json.dumps(fields), staffing, ts, ts))
            for m in members:
                conn.execute("INSERT INTO deployment_members VALUES (?,?)", (did, m))
            conn.execute("INSERT INTO stage_events (tenant_id, deployment_id, from_stage, to_stage,"
                         " actor_id, note, at) VALUES (?,?,?,?,?,?,?)",
                         ("ten_meridian", did, None, stage, "usr_marcus", "imported", ts))
            audit.record(conn, "ten_meridian", "usr_marcus", "deployment.create", did,
                         {"name": name, "stage": stage})
        conn.execute("INSERT INTO deployments VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                     ("dep_orb1", "ten_orbital", "cus_orb1", "Orbital secret project", "discover",
                      "on_track", "usr_orb", "{}", "", ts, ts))

        tasks = [
            ("dep_northfield", "adopt", "Run build-vs-training on invoice exception times", "usr_maya", "in_progress", "2026-09-29"),
            ("dep_northfield", "adopt", "Walk Harrisburg AP lead through exception queue", "usr_sam", "open", "2026-10-01"),
            ("dep_northfield", "adopt", "Confirm 3-way-match tolerance config with customer IT", "usr_maya", "blocked", "2026-09-26"),
            ("dep_harborview", "integrate", "Scope FHIR read permissions for intake agent", "usr_jordan", "in_progress", "2026-10-03"),
            ("dep_harborview", "integrate", "Human approval gate on payer submissions", "usr_jordan", "open", "2026-10-10"),
            ("dep_castellan", "test", "Customer security review of model gateway", "usr_lena", "blocked", "2026-09-24"),
            ("dep_castellan", "test", "Conformance run on claims sample set", "usr_sam", "open", "2026-10-02"),
            ("dep_redline", "discover", "Systems inventory with Redline ops", "usr_sam", "open", "2026-10-06"),
        ]
        for i, (did, stage, title, who, status, due) in enumerate(tasks, 1):
            tid = f"tsk_{i:03d}"
            conn.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                         (tid, "ten_meridian", did, stage, title, who, status, due, "usr_marcus", ts, ts))
            audit.record(conn, "ten_meridian", "usr_marcus", "task.create", tid,
                         {"deployment": did, "assignee": who, "title": title})
    return DEMO_TOKENS
