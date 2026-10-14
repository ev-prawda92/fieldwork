"""Run a whole delivery organisation through Fieldwork, end to end, on a simulated calendar.

    fieldwork simulate --projects 56 --weeks 26 --db sim.db

A Head of Deployments sets up a fresh workspace (two extra roles: Project Manager and Deployed
Product Manager), hires a team, and wins deals. Engagement managers open deployments, staff them
and set budgets and statements of work. Forward-deployed engineers, AI engineers, implementation
consultants, project managers, deployed PMs and customer stakeholders then work every project
through Discover, Integrate, Test, Go-live, Adopt and Value, one weekday at a time: tasks, the
ball, engine runs, findings, approvals, delays, holds, flags, time, status reports, milestones,
sign-off, invoices and payment. Model choice changes how projects go.

Every action goes through the HTTP API as the person doing it, with their own token and
permissions; nothing writes to the database behind the API's back except the one bootstrap step
a real workspace gets from OAuth sign-in. The clock is moved with freezegun, so stage clocks,
burn, the delay ledger and the audit chain all see real elapsed time.

At the end it checks the invariants that have to hold (audit chain, tenant isolation, money,
the ball, the delay ledger) and writes a JSON report of every call that didn't go as expected.
The database it leaves behind can be served (`fieldwork serve --db sim.db`) and signed into with
the tokens in the report.
"""

import json
import random
import statistics
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

START = date(2026, 4, 13)  # a Monday, 26 weeks before the day this was written

INDUSTRIES = {
    "Insurance": ["Claims triage copilot", "Subrogation recovery agent", "Underwriting intake agent", "FNOL voice agent"],
    "Logistics": ["Freight audit agent", "Carrier dispute agent", "Dock scheduling assistant", "Customs docs extraction"],
    "Healthcare payer": ["Prior-auth intake agent", "Provider credentialing agent", "Appeals summarizer", "Claims edit assistant"],
    "Banking": ["KYC refresh agent", "Dispute resolution copilot", "Loan doc extraction", "AML alert triage"],
    "Retail": ["Returns triage agent", "Vendor onboarding agent", "Store ops copilot", "Catalog enrichment agent"],
    "Manufacturing": ["Supplier quality agent", "Maintenance work-order copilot", "PO exception agent", "Spec sheet extraction"],
    "Utilities": ["Outage comms agent", "Field service dispatch copilot", "Billing exception agent", "Interconnection intake agent"],
    "SaaS": ["Support deflection agent", "Renewal risk copilot", "Implementation intake agent", "Contract review agent"],
    "Public sector": ["Benefits intake agent", "Permit review copilot", "Records request agent", "Caseworker assistant"],
    "Telecom": ["Order fallout agent", "Network ticket triage", "Churn save copilot", "Porting exception agent"],
}
SYSTEMS = {
    "Insurance": ["Guidewire ClaimCenter", "Duck Creek", "Salesforce", "SharePoint", "Snowflake"],
    "Logistics": ["MercuryGate TMS", "NetSuite", "SAP S/4HANA", "Carrier portal", "Snowflake"],
    "Healthcare payer": ["Epic Payer Platform", "Availity", "Salesforce Health Cloud", "Facets", "Snowflake"],
    "Banking": ["nCino", "Fiserv", "Salesforce", "Actimize", "Databricks"],
    "Retail": ["Shopify", "Manhattan WMS", "Zendesk", "SAP S/4HANA", "BigQuery"],
    "Manufacturing": ["SAP S/4HANA", "Oracle EBS", "ServiceNow", "Maximo", "Snowflake"],
    "Utilities": ["Oracle CC&B", "ServiceNow", "Esri ArcGIS", "Salesforce", "Snowflake"],
    "SaaS": ["Salesforce", "Zendesk", "Gainsight", "HubSpot", "Snowflake"],
    "Public sector": ["Salesforce Public Sector", "ServiceNow", "SharePoint", "Workday", "Oracle EBS"],
    "Telecom": ["Amdocs", "ServiceNow", "Salesforce", "Netcracker", "BigQuery"],
}
NAMES = ["Aster", "Birch", "Cobalt", "Delmar", "Eastgate", "Fairhaven", "Granite", "Harbor", "Ironwood", "Juniper",
         "Keystone", "Lakeshore", "Meridian", "Northfield", "Oakridge", "Pinecrest", "Quarry", "Redline", "Summit",
         "Tidewater", "Union", "Vantage", "Westbrook", "Yardley", "Zephyr", "Alder", "Bluewater", "Cedar", "Driftwood",
         "Ember", "Foxglove", "Glenview", "Highland", "Inlet", "Jasper", "Kestrel", "Linden", "Maple", "Norwood",
         "Orchard", "Prairie", "Ridgeway", "Sterling", "Thornton", "Upland", "Valley", "Willow", "Crestline",
         "Bayview", "Copper", "Fieldstone", "Garnet", "Hollis", "Larkspur", "Mesa", "Northstar", "Osprey", "Palisade"]
SUFFIX = {"Insurance": "Mutual", "Logistics": "Freight", "Healthcare payer": "Health Plan", "Banking": "Bank",
          "Retail": "Retail", "Manufacturing": "Industries", "Utilities": "Energy", "SaaS": "Software",
          "Public sector": "County", "Telecom": "Telecom"}

# The model a team picks changes how the project goes: how well it does on the test suite, how
# often access or rate limits get in the way, and what it costs to run per month at full volume.
MODELS = {
    "Claude Opus":         {"quality": 0.97, "access_risk": 0.06, "spend": 9000},
    "Claude Sonnet":       {"quality": 0.95, "access_risk": 0.05, "spend": 3600},
    "GPT":                 {"quality": 0.94, "access_risk": 0.10, "spend": 5200},
    "Gemini":              {"quality": 0.93, "access_risk": 0.08, "spend": 3900},
    "Llama (self-hosted)": {"quality": 0.89, "access_risk": 0.22, "spend": 2400},
}
PROFILES = [("smooth", 40), ("customer_slow", 18), ("model_trouble", 10), ("test_failures", 10),
            ("scope_creep", 10), ("paused", 7), ("stalled", 5)]
STAGES = ["discover", "integrate", "test", "golive", "adopt", "value"]
TARGET = {"discover": 14, "integrate": 21, "test": 14, "golive": 7, "adopt": 30, "value": 30}
MILESTONE_SPLIT = [("Discovery read-out", "discover", .10), ("Integrations live in staging", "integrate", .25),
                   ("Conformance passed", "test", .15), ("Production go-live", "golive", .25),
                   ("Value confirmed", "value", .25)]

TEAM = [  # name, role, weekly hours
    ("Dana Whitfield", "head", 40),
    ("Marcus Hale", "engagement_manager", 40), ("Priya Raman", "engagement_manager", 40),
    ("Tomás Ibarra", "engagement_manager", 40),
    ("Grace Okafor", "project_manager", 40), ("Ben Adler", "project_manager", 40),
    ("Hana Sato", "project_manager", 40), ("Luis Moreno", "project_manager", 40),
    ("Nadia Petrova", "deployed_pm", 40), ("Owen Clarke", "deployed_pm", 40),
    ("Imani Brooks", "deployed_pm", 40), ("Felix Wagner", "deployed_pm", 40),
    ("Maya Chen", "fde", 40), ("Sam Okoro", "fde", 40), ("Rosa Delgado", "fde", 40),
    ("Kai Nakamura", "fde", 40), ("Elena Rossi", "fde", 40), ("Dev Patel", "fde", 40),
    ("Chloe Martin", "fde", 40), ("Arjun Mehta", "fde", 40),
    ("Jordan Reyes", "ai_engineer", 40), ("Lena Park", "ai_engineer", 40), ("Theo Laurent", "ai_engineer", 40),
    ("Zara Ahmed", "ai_engineer", 40), ("Noah Fischer", "ai_engineer", 40), ("Ivy Chen", "ai_engineer", 40),
    ("Ruth Albrecht", "implementation_consultant", 40), ("Carlos Vega", "implementation_consultant", 40),
    ("Mei Lin", "implementation_consultant", 40),
]
STAKEHOLDER_FIRST = ["Alex", "Jamie", "Morgan", "Taylor", "Casey", "Jordan", "Riley", "Avery", "Quinn", "Drew",
                     "Sydney", "Parker", "Reese", "Rowan", "Emerson", "Hayden"]
STAKEHOLDER_LAST = ["Nguyen", "Garcia", "Kowalski", "Haddad", "Lindqvist", "Osei", "Ferreira", "Mbeki", "Novak",
                    "Iyer", "Brennan", "Dubois", "Kim", "Silva", "Abara", "Weiss"]


def _real_clock() -> float:
    """Wall-clock seconds that freezegun doesn't move, for timing the API itself."""
    try:
        from freezegun.api import real_perf_counter
        return real_perf_counter()
    except ImportError:
        return time.perf_counter()


def weekday(d: date) -> bool:
    return d.weekday() < 5


@dataclass
class Project:
    n: int
    customer: str
    industry: str
    use_case: str
    systems: list
    model: str
    profile: str
    start: date
    budget_hours: float
    budget_usd: float
    durations: dict
    em: str = ""
    pm: str = ""
    dpm: str = ""
    fdes: list = field(default_factory=list)
    ai: str = ""
    ic: str | None = None
    stakeholder: str = ""
    dep_id: str = ""
    cust_id: str = ""
    stage_i: int = -1                  # -1: not opened yet
    stage_day: int = 0
    done: bool = False
    stalled_at: int | None = None
    pause: tuple | None = None         # (start_day, end_day) on hold
    tasks: dict = field(default_factory=dict)       # stage -> [task ids]
    conformance_failed: bool = False
    milestones: dict = field(default_factory=dict)  # id -> status we believe
    events: list = field(default_factory=list)

    @property
    def stage(self) -> str | None:
        return STAGES[self.stage_i] if 0 <= self.stage_i < len(STAGES) else None

    @property
    def team(self) -> list:
        return [x for x in [self.em, self.pm, self.dpm, *self.fdes, self.ai, self.ic] if x]

    @property
    def doers(self) -> list:
        return [x for x in [*self.fdes, self.ai, self.ic, self.dpm] if x]


class Sim:
    def __init__(self, db_url: str, projects: int = 56, weeks: int = 26, seed: int = 2026, verbose: bool = True):
        from fastapi.testclient import TestClient
        from freezegun import freeze_time
        from . import db
        from .app import create_app
        self.freeze_time = freeze_time
        self.rng = random.Random(seed)
        self.n_projects, self.weeks, self.verbose = projects, weeks, verbose
        conn = db.connect(db_url)
        db.reset(conn)
        db.init(conn)
        self._clock = None
        import pydantic.v1  # noqa: F401  (import before the clock is frozen: it subclasses date)
        self.app = create_app(db_url)
        self._tick(START)
        self.conn = self.app.state.conn
        self.client = TestClient(self.app)
        self.tokens: dict = {}       # user id -> token
        self.names: dict = {}        # user id -> name
        self.roles: dict = {}        # user id -> role
        self.by_role: dict = defaultdict(list)
        self.projects: list[Project] = []
        self.calls = Counter()
        self.ok = Counter()
        self.issues: list = []       # unexpected responses
        self.expected_refusals = Counter()
        self.day = START
        self.day_n = 0
        self.timing: list = []
        self.counts = Counter()
        self.held: dict = {}         # task id -> (holder uid, return-on day number)
        self.by_model = defaultdict(Counter)

    # ---------------------------------------------------------------- plumbing

    def _tick(self, d: date, hour: int = 9) -> None:
        if self._clock:
            self._clock.stop()
        self._clock = self.freeze_time(datetime(d.year, d.month, d.day, hour, 0, tzinfo=timezone.utc), tick=True)
        self._clock.start()

    def call(self, who: str, method: str, path: str, body=None, expect=(200, 201), why: str = "",
             refusal_ok: tuple = ()):
        """One API call as one person. Anything outside `expect` is recorded; statuses in
        refusal_ok are the platform correctly saying no, and are counted, not reported."""
        tok = self.tokens[who] if who in self.tokens else who
        t0 = _real_clock()
        r = self.client.request(method, path, json=body, headers={"Authorization": f"Bearer {tok}"})
        self.timing.append((_real_clock() - t0) * 1000)
        route = "/".join("{id}" if any(p.startswith(x) for x in ("dep_", "tsk_", "usr_", "cus_", "fnd_", "ms_",
                                                                "sow_", "dly_", "flg_", "apr_", "opp_", "chk_",
                                                                "rpt_", "ten_")) or p.isdigit() else p
                         for p in path.split("?")[0].split("/"))
        key = f"{method} {route}"
        self.calls[key] += 1
        if r.status_code in expect:
            self.ok[key] += 1
            try:
                return r.json()
            except ValueError:
                return r.text
        if r.status_code in refusal_ok:
            self.expected_refusals[f"{key} -> {r.status_code}"] += 1
            return None
        try:
            detail = r.json().get("detail")
        except Exception:
            detail = r.text[:300]
        self.issues.append({"day": str(self.day), "who": self.names.get(who, who), "role": self.roles.get(who, "?"),
                            "call": key, "path": path, "status": r.status_code, "detail": detail, "why": why,
                            "body": body if not isinstance(body, dict) or len(json.dumps(body)) < 400 else "…"})
        return None

    def log(self, msg: str) -> None:
        if self.verbose:
            print(f"[{self.day}] {msg}", flush=True)

    def pick(self, seq):
        return self.rng.choice(seq)

    # ------------------------------------------------------------------ setup

    def bootstrap(self) -> None:
        """The one step a real workspace gets from OAuth: a workspace and its founder."""
        from . import audit, beta
        from .app import token_hash
        with self.conn.tx():
            tid, head = beta.create_workspace(self.conn, "Northstar Deploy", "dana@northstar.example", "Dana Whitfield")
            tok = "fwu_sim_head"
            self.conn.execute("UPDATE users SET token_hash=? WHERE id=?", (token_hash(tok), head["id"]))
            # A second, unrelated workspace, to prove nothing crosses over.
            tid2, other = beta.create_workspace(self.conn, "Orbital Partners", "ines@orbital.example", "Ines Duarte")
            self.conn.execute("UPDATE users SET token_hash=? WHERE id=?", (token_hash("fwu_sim_other"), other["id"]))
            audit.record(self.conn, tid, head["id"], "sim.bootstrap", tid, {"note": "simulation workspace"})
        self.tenant, self.head = tid, head["id"]
        self.tokens[self.head] = tok
        self.names[self.head], self.roles[self.head] = "Dana Whitfield", "head"
        self.by_role["head"].append(self.head)
        self.tokens["other"] = "fwu_sim_other"
        self.names["other"], self.roles["other"] = "Ines Duarte (Orbital)", "other_tenant"

    def configure(self) -> None:
        """Dana makes the workspace hers: two more roles, model and budget fields, home screens, a webhook."""
        cfg = self.call(self.head, "GET", "/api/config")["config"]
        cfg["roles"] += [{"key": "project_manager", "name": "Project Manager"},
                         {"key": "deployed_pm", "name": "Deployed Product Manager"}]
        pm_actions = ["deployment.view", "deployment.advance", "deployment.edit", "deployment.staff", "task.create",
                      "task.assign", "task.update_any", "task.view_internal", "finding.view_internal",
                      "customer.share", "report.create", "delay.confirm", "flag.handle", "sow.edit",
                      "milestone.submit", "billing.view", "engine.run"]
        dpm_actions = ["deployment.view", "deployment.advance", "deployment.edit", "task.create", "task.view_internal",
                       "finding.view_internal", "engine.run", "report.create", "customer.share", "finding.confirm"]
        for a in cfg["permissions"]:
            if a in pm_actions:
                cfg["permissions"][a]["project_manager"] = "own"
            if a in dpm_actions:
                cfg["permissions"][a]["deployed_pm"] = "own"
        for a in ("people.read", "pipeline.view"):
            cfg["permissions"][a]["project_manager"] = "all"
        cfg["views"]["project_manager"] = ["ball", "chain", "flags", "today", "my_tasks", "delays"]
        cfg["views"]["deployed_pm"] = ["ball", "clients", "findings", "my_tasks", "today"]
        cfg["views"]["head"] = ["kpis", "ball", "chain", "flags", "capacity", "delays", "findings", "pipeline"]
        cfg["fields"]["deployment"] += [
            {"key": "model_primary", "label": "Primary model", "type": "select", "options": list(MODELS)},
            {"key": "model_fallback", "label": "Fallback model", "type": "select", "options": list(MODELS)},
            {"key": "integrations", "label": "Systems integrated", "type": "text"},
            {"key": "budget_usd", "label": "Delivery budget (USD)", "type": "number"},
            {"key": "ai_spend_month_usd", "label": "Model spend last month (USD)", "type": "number"},
        ]
        cfg["fields"]["customer"].append({"key": "industry_segment", "label": "Segment", "type": "text"})
        cfg["integrations"]["webhooks"] = [{"id": "finance", "url": "https://hooks.northstar.example/fieldwork",
                                            "events": ["milestone.accepted", "task.passed", "deployment.advanced"]}]
        out = self.call(self.head, "PUT", "/api/config", cfg, why="add PM and DPM roles, model and budget fields")
        assert out is not None, "config was refused; see issues"

    def hire(self) -> None:
        for name, role, hours in TEAM[1:]:
            email = name.lower().replace(" ", ".").replace("á", "a") + "@northstar.example"
            r = self.call(self.head, "POST", "/api/people", {"name": name, "email": email, "role": role})
            if not r:
                continue
            uid = r["id"]
            self.tokens[uid], self.names[uid], self.roles[uid] = r["token"], name, role
            self.by_role[role].append(uid)
            self.call(self.head, "PATCH", f"/api/people/{uid}", {"weekly_hours": hours})
        self.call(self.head, "PATCH", f"/api/people/{self.head}", {"weekly_hours": 40})
        self.log(f"hired {len(self.tokens) - 2} people across {len(self.by_role)} roles")

    def plan_projects(self) -> None:
        names = self.rng.sample(NAMES, k=min(self.n_projects, len(NAMES)))
        while len(names) < self.n_projects:
            names.append(f"{self.pick(NAMES)} {len(names)}")
        weights = [w for _, w in PROFILES]
        for i in range(self.n_projects):
            ind = self.pick(list(INDUSTRIES))
            profile = self.rng.choices([p for p, _ in PROFILES], weights)[0]
            model = self.rng.choices(list(MODELS), [22, 28, 22, 14, 14])[0]
            factor = {"smooth": .9, "customer_slow": 1.45, "model_trouble": 1.25, "test_failures": 1.2,
                      "scope_creep": 1.35, "paused": 1.1, "stalled": 1.0}[profile]
            durations = {s: max(3, round(TARGET[s] * factor * self.rng.uniform(.75, 1.25))) for s in STAGES}
            hours = self.rng.choice([480, 640, 800, 960, 1200, 1600])
            p = Project(n=i + 1, customer=f"{names[i]} {SUFFIX[ind]}", industry=ind, use_case=self.pick(INDUSTRIES[ind]),
                        systems=self.rng.sample(SYSTEMS[ind], k=self.rng.randint(2, 4)), model=model, profile=profile,
                        start=START + timedelta(days=self.rng.randint(0, 105)), budget_hours=hours,
                        budget_usd=hours * self.rng.choice([210, 235, 260, 285]), durations=durations)
            if profile == "stalled":
                p.stalled_at = self.rng.randint(1, 3)
            self.projects.append(p)
        self.projects.sort(key=lambda p: p.start)
        self.log(f"planned {len(self.projects)} projects: " +
                 ", ".join(f"{k} {v}" for k, v in Counter(p.profile for p in self.projects).items()))

    # ---------------------------------------------------------------- opening

    def open_project(self, p: Project) -> None:
        em = p.em = self.pick(self.by_role["engagement_manager"])
        # Most deals come through the pipeline; a few are opened by hand.
        if self.rng.random() < .7:
            o = self.call(em, "POST", "/api/opportunities", {
                "name": f"{p.customer}: {p.use_case}", "customer": p.customer, "use_case": p.use_case,
                "value": round(p.budget_usd * 1.35), "probability": .6, "stage": "proposal",
                "expected_start": str(self.day + timedelta(days=7)), "weekly_hours": round(p.budget_hours / 16)})
            if o:
                self.call(em, "GET", "/api/pipeline")
                self.call(em, "PATCH", f"/api/opportunities/{o['id']}", {"stage": "won", "probability": 1})
                r = self.call(em, "POST", f"/api/opportunities/{o['id']}/open-deployment")
                if r:
                    p.dep_id = r["deployment_id"]
                    p.cust_id = r.get("customer_id", "")
        if not p.dep_id:
            c = self.call(em, "POST", "/api/customers", {"name": p.customer, "industry": p.industry,
                                                         "fields": {"industry_segment": p.industry}})
            if not c:
                return
            p.cust_id = c["id"]
            d = self.call(em, "POST", "/api/deployments", {"customer_id": p.cust_id, "name": p.use_case})
            if not d:
                return
            p.dep_id = d["id"]
        # The customer's stakeholder gets a seat.
        s = f"{self.pick(STAKEHOLDER_FIRST)} {self.pick(STAKEHOLDER_LAST)}"
        email = f"{s.lower().replace(' ', '.')}.{p.n}@{p.customer.split()[0].lower()}.example"
        r = self.call(self.head, "POST", "/api/people", {"name": s, "email": email, "role": "customer"})
        if r:
            p.stakeholder = r["id"]
            self.tokens[r["id"]], self.names[r["id"]], self.roles[r["id"]] = r["token"], s, "customer"
        # Staffing: the least-loaded people of each kind.
        load = Counter(x for q in self.projects if q.dep_id and not q.done for x in q.team)
        least = lambda role, k=1: sorted(self.by_role[role], key=lambda u: (load[u], self.rng.random()))[:k]
        p.pm, p.dpm, p.ai = least("project_manager")[0], least("deployed_pm")[0], least("ai_engineer")[0]
        p.fdes = least("fde", 2 if p.budget_hours >= 960 else 1)
        if self.rng.random() < .4:
            p.ic = least("implementation_consultant")[0]
        for uid in [p.pm, p.dpm, *p.fdes, p.ai, p.ic, p.stakeholder]:
            if uid:
                self.call(em, "POST", f"/api/deployments/{p.dep_id}/members", {"user_id": uid})
        alloc = {p.em: .1, p.pm: .25, p.dpm: .3, p.ai: .5, **{f: .6 for f in p.fdes}, **({p.ic: .4} if p.ic else {})}
        for uid, a in alloc.items():
            self.call(em, "PUT", f"/api/deployments/{p.dep_id}/members/{uid}", {"allocation": a})
        total = sum(p.durations.values())
        fallback = self.pick([m for m in MODELS if m != p.model])
        self.call(em, "PATCH", f"/api/deployments/{p.dep_id}", {
            "start_on": str(self.day), "end_on": str(self.day + timedelta(days=round(total * 1.4))),
            "budget_hours": p.budget_hours, "staffing_req": f"{len(p.fdes)} FDE, 1 AI engineer",
            "fields": {"tier": "Strategic" if p.budget_hours >= 1200 else "Standard" if p.budget_hours >= 800 else "Pilot",
                       "target_golive": str(self.day + timedelta(days=sum(p.durations[s] for s in STAGES[:4]))),
                       "model_primary": p.model, "model_fallback": fallback,
                       "integrations": ", ".join(p.systems), "budget_usd": p.budget_usd}})
        # The statement of work: milestones tied to stages, so delivery turns into billing.
        sow_value = round(p.budget_usd * self.rng.uniform(1.25, 1.45), -2)
        ms = [{"name": n, "stage": st, "amount": round(sow_value * share, -2),
               "criteria": f"{n} accepted by {self.names.get(p.stakeholder, 'the customer')}"}
              for n, st, share in MILESTONE_SPLIT]
        sow = self.call(p.pm, "POST", f"/api/deployments/{p.dep_id}/sows", {
            "reference": f"SOW-{p.n:03d}", "total_value": sum(m["amount"] for m in ms),
            "signed_on": str(self.day), "milestones": ms}, why="PM sets up the statement of work")
        if sow:
            self.counts["sows"] += 1
        p.stage_i, p.stage_day = 0, 0
        self.counts["projects_opened"] += 1
        self.enter_stage(p)
        self.log(f"opened #{p.n} {p.customer} · {p.use_case} · {p.model} · {p.profile} · {len(p.team)} staffed")

    # ----------------------------------------------------------------- stages

    def task(self, p: Project, who: str, title: str, by: str | None = None, shared: bool = False,
             due_days: int = 7) -> str | None:
        by = by or p.pm
        body = {"deployment_id": p.dep_id, "title": title[:200], "assignee_id": who,
                "due": str(self.day + timedelta(days=due_days)), "visibility": "shared" if shared else "internal"}
        r = self.call(by, "POST", "/api/tasks", body, why="create task")
        if r:
            p.tasks.setdefault(p.stage, []).append(r["id"])
            self.counts["tasks_created"] += 1
            return r["id"]
        return None

    def run_engine(self, p: Project, who: str, key: str, text: str, title: str = "") -> dict | None:
        r = self.call(who, "POST", f"/api/deployments/{p.dep_id}/engines/stage/{key}",
                      {"title": title or f"{key} · {p.customer}", "input": text}, why=f"run {key}")
        if r:
            self.counts[f"engine_{key}"] += 1
        return r

    def confirm_findings(self, p: Project) -> None:
        for f in self.call(p.em, "GET", f"/api/deployments/{p.dep_id}/findings") or []:
            if not f.get("confirmed_by") and f.get("created_by") != p.em:
                if self.call(p.em, "POST", f"/api/findings/{f['id']}/confirm", {}):
                    self.counts["findings_confirmed"] += 1

    def enter_stage(self, p: Project) -> None:
        st, d = p.stage, p.durations[p.stage]
        sys_ = p.systems
        if st == "discover":
            self.task(p, p.em, f"Kickoff with {self.names.get(p.stakeholder, 'the sponsor')}", due_days=3)
            self.task(p, p.fdes[0], "Systems inventory and data classes", due_days=d - 3)
            for s in sys_:
                t = self.task(p, p.fdes[0], f"Access request: {s}", due_days=d - 2)
                if t and p.stakeholder and self.rng.random() < .7:
                    self.pass_ball(p, t, p.fdes[0], None, f"Need a service account for {s}", visibility_first=True)
            self.task(p, p.ai, f"Model shortlist and eval plan ({p.model} primary)", due_days=d - 1)
            self.task(p, p.stakeholder, "Share last quarter's volumes and baseline handling times", shared=True,
                      by=p.pm, due_days=d - 4) if p.stakeholder else None
        elif st == "integrate":
            for i, s in enumerate(sys_):
                self.task(p, p.fdes[i % len(p.fdes)], f"Build {s} connector", due_days=d - 4)
            self.task(p, p.ai, f"Eval harness on {p.model}; fallback wired", due_days=d - 3)
            self.task(p, p.ai, "Agent authority profile: scopes, limits, human gates", due_days=d - 2)
            if p.ic:
                self.task(p, p.ic, "Process maps and SOP updates", due_days=d - 2)
            # The agent asks before writing to a customer system; someone else decides.
            a = self.call(p.ai, "POST", f"/api/deployments/{p.dep_id}/approvals",
                          {"agent": f"{p.use_case} agent", "request": f"write to {sys_[0]} in staging",
                           "detail": "Creates draft records only; a person releases them."})
            if a:
                self.counts["approvals_requested"] += 1
                p.events.append(("approval", a["id"], self.day_n + self.rng.randint(1, 4)))
            if p.profile == "model_trouble" or self.rng.random() < MODELS[p.model]["access_risk"]:
                r = self.call(p.ai, "POST", f"/api/deployments/{p.dep_id}/delays",
                              {"signal": "model_access", "reason": f"{p.model}: rate limits / quota on the eval run",
                               "evidence": "429s from the provider during the nightly eval"})
                if r:
                    self.counts["model_access_delays"] += 1
                    self.by_model[p.model]["access_delays"] += 1
        elif st == "test":
            self.task(p, p.ai, "Conformance suite on customer data", due_days=d - 4)
            t = self.task(p, p.stakeholder, "UAT sign-off on 25 real cases", shared=True, by=p.pm, due_days=d - 2) \
                if p.stakeholder else None
            self.task(p, p.dpm, "UAT scripts and acceptance criteria with the customer", due_days=d - 6)
        elif st == "golive":
            self.call(p.pm, "POST", f"/api/deployments/{p.dep_id}/checklist", {"defaults": True})
            self.task(p, p.fdes[0], "Cutover runbook and rollback rehearsal", due_days=2)
            self.task(p, p.dpm, f"Train end users ({self.rng.randint(12, 140)} people, 3 sessions)", due_days=d - 1)
            self.task(p, p.ai, "Production model gateway: keys, limits, monitoring", due_days=2)
        elif st == "adopt":
            self.task(p, p.dpm, "Adoption review: build vs training", due_days=d - 5)
            self.task(p, p.ai, "Prompt and retrieval tuning from production misses", due_days=d - 3)
            if p.ic:
                self.task(p, p.ic, "Refresher training at the slowest site", due_days=d - 2)
        elif st == "value":
            self.task(p, p.dpm, "Value study: treatment vs comparison group", due_days=d - 6)
            self.task(p, p.pm, "Value read-out with the executive sponsor", due_days=d - 2)

    def stage_work(self, p: Project) -> None:
        """What happens on a project today."""
        st, i = p.stage, p.stage_day
        dur = p.durations[st]
        # Engines run partway through each stage, by the people who'd run them.
        if st == "discover" and i == dur // 2:
            rows = ["system,owner,data_class,interface,access,documented,controls"]
            for s in p.systems:
                rows.append(f"{s},{self.pick(['IT', 'Ops', 'Finance systems', ''])},"
                            f"{self.pick(['internal', 'confidential', 'regulated'])},{self.pick(['api', 'db', 'ui'])},"
                            f"{self.pick(['granted', 'requested', 'none'])},{self.pick(['yes', 'no'])},"
                            f"{self.pick(['sso; audit logging', 'sso', ''])}")
            self.run_engine(p, p.fdes[0], "census", "\n".join(rows))
        if st == "integrate" and i == dur - 3:
            from .seed import HARBORVIEW_AUTHORITY
            self.run_engine(p, p.ai, "cortex", HARBORVIEW_AUTHORITY, f"Authority check · {p.use_case}")
        if st == "test" and i in (dur // 3, dur - 2):
            q = MODELS[p.model]["quality"] - (.08 if p.profile == "test_failures" and i == dur // 3 else 0)
            if i == dur - 2:
                q = max(q, .97)
            rows = ["# threshold: 0.95", "case_id,category,expected,actual,critical"]
            for k in range(40):
                cat = self.pick(["routing", "extraction", "severity", "policy"])
                ok = self.rng.random() < q
                crit = "yes" if k % 7 == 0 else "no"
                rows.append(f"C-{k:02d},{cat},A{k},{'A' if ok else 'B'}{k},{crit}")
            r = self.run_engine(p, p.ai, "conformance", "\n".join(rows), f"Conformance · {p.model}")
            status = ((r or {}).get("result") or {}).get("status")
            if r and i == dur // 3:
                self.by_model[p.model]["first_runs"] += 1
                self.by_model[p.model]["first_run_passed"] += status == "pass"
            if r and status == "fail":
                p.conformance_failed = True
                self.counts["conformance_failures"] += 1
                self.task(p, p.ai, "Fix failing conformance cases and rerun", due_days=2)
        if st == "golive" and i == dur - 1:
            rows = ["# max_open_sev2: 2", "id,severity,status,opened_at,resolved_at,area"]
            base = datetime.combine(self.day - timedelta(days=2), datetime.min.time())
            for k in range(self.rng.randint(4, 12)):
                o = base + timedelta(hours=k * 4)
                sev = self.rng.choices(["sev1", "sev2", "sev3", "sev4"], [1, 4, 8, 4])[0]
                rows.append(f"INC-{k:03d},{sev},resolved,{o:%Y-%m-%dT%H:%M},{o + timedelta(hours=3):%Y-%m-%dT%H:%M},"
                            f"{self.pick(['routing', 'ui', 'model gateway', 'extraction'])}")
            self.run_engine(p, p.fdes[0], "golive", "\n".join(rows), "Command center")
            for it in self.call(p.pm, "GET", f"/api/deployments/{p.dep_id}/checklist") or []:
                if not it.get("done"):
                    self.call(p.pm, "PATCH", f"/api/checklist/{it['id']}", {"done": True})
        if st == "adopt" and i == dur // 2:
            rows = ["user_id,minutes,tenure_months,site"]
            sites = self.rng.sample(["North", "South", "East", "West", "Central"], 3)
            slow = self.pick(sites)
            for k in range(36):
                site = sites[k % 3]
                rows.append(f"u{k:03d},{(11 if site == slow else 6) + self.rng.gauss(0, .7):.2f},"
                            f"{self.pick([2, 6, 14, 30])},{site}")
            r = self.call(p.dpm, "POST", f"/api/deployments/{p.dep_id}/engines/sendero",
                          {"title": f"Adoption friction · {p.customer}", "csv": "\n".join(rows),
                           "metric": "minutes", "baseline": 6.5}, why="sendero")
            if r:
                self.counts["engine_sendero"] += 1
        if st == "value" and i == dur // 2:
            from .seed import value_study_csv
            self.run_engine(p, p.dpm, "value_study",
                            value_study_csv(seed=p.n, effective=str(self.day - timedelta(days=21)),
                                            drop=self.rng.uniform(.5, 4.0), metric="minutes per case"),
                            "Value study")
            delays = self.call(p.pm, "GET", f"/api/delays?deployment_id={p.dep_id}") or []
            by = Counter()
            for x in delays:
                by[{"customer": "customer", "team": "vendor"}.get(x.get("owner"), "third_party")] += x.get("days", 0)
            self.run_engine(p, p.pm, "attribution", json.dumps({
                "metric": "minutes per case", "baseline": 12.0, "current": round(12 - self.rng.uniform(.5, 4), 1),
                "volume_per_month": self.rng.randint(800, 12000), "cost_per_hour": self.rng.randint(32, 70),
                "planned_days": sum(TARGET.values()), "actual_days": (self.day - p.start).days,
                "delays": [{"event": f"{k} delays", "days": round(v, 1), "cause": k} for k, v in by.items() if v]}),
                "Value attribution")
        # Findings get confirmed by the EM a day or two after they land.
        if i % 3 == 1:
            self.confirm_findings(p)

    def advance(self, p: Project) -> None:
        if p.stage_i == len(STAGES) - 1:
            r = self.call(p.pm, "PUT", f"/api/deployments/{p.dep_id}/stages/value", {"state": "done",
                                                                                   "note": "Value confirmed"})
            if r is not None:
                p.done = True
                self.counts["projects_completed"] += 1
                self.log(f"#{p.n} {p.customer} complete in {(self.day - p.start).days} days")
            return
        nxt = STAGES[p.stage_i + 1]
        mover = self.pick([p.pm, p.fdes[0], p.em])
        r = self.call(mover, "POST", f"/api/deployments/{p.dep_id}/advance", {"to_stage": nxt, "note": ""},
                      why=f"advance to {nxt}")
        if r is not None:
            p.stage_i += 1
            p.stage_day = 0
            self.counts["advances"] += 1
            self.enter_stage(p)

    # ------------------------------------------------------------------- the ball

    def pass_ball(self, p: Project, task_id: str, frm: str, to: str | None, note: str,
                  visibility_first: bool = False) -> None:
        """Pass a task for a step. to=None means the customer, which needs the task shared first."""
        if to is None:
            if not p.stakeholder:
                return
            if visibility_first:
                self.call(p.pm, "PATCH", f"/api/tasks/{task_id}", {"visibility": "shared"},
                          why="share a task so the customer can take the ball")
            to = p.stakeholder
        r = self.call(frm, "POST", f"/api/tasks/{task_id}/pass", {"to": to, "note": note}, why="pass the ball",
                      refusal_ok=(422,))
        if r:
            slow = p.profile == "customer_slow" and self.roles.get(to) == "customer"
            back = self.day_n + (self.rng.randint(3, 9) if slow else self.rng.randint(0, 3))
            self.held[task_id] = (to, back, p.n)
            self.counts["ball_passes"] += 1
            if self.roles.get(to) == "customer":
                self.counts["ball_to_customer"] += 1

    def ball_day(self, p: Project) -> None:
        """Balls come back when the person they went to is done; people pass work when they need someone."""
        for tid, (holder, back_on, pn) in list(self.held.items()):
            if pn != p.n or back_on > self.day_n:
                continue
            r = self.call(holder, "POST", f"/api/tasks/{tid}/return", {"note": self.pick([
                "Done on my side", "Access granted", "Answered in the doc", "Confirmed with the team", ""])},
                why="send the ball back", refusal_ok=(422,))
            self.held.pop(tid, None)
            if r:
                self.counts["ball_returns"] += 1
        if self.rng.random() < .35:
            tasks = [t for t in (self.call(self.pick(p.doers), "GET", f"/api/tasks?deployment_id={p.dep_id}") or [])
                     if t["status"] != "done" and t["id"] not in self.held and t.get("assignee_id") in p.doers]
            if tasks:
                t = self.pick(tasks)
                frm = t["ball"]["holder_id"]
                to = self.pick([x for x in [p.ai, *p.fdes, p.dpm, p.pm, p.ic] if x and x != frm])
                if self.rng.random() < .25 and t["visibility"] == "shared":
                    to = None
                self.pass_ball(p, t["id"], frm, to, self.pick([
                    "Need your eyes on this before I continue", "Can you own the next step?",
                    "Blocked until you confirm the field mapping", "Quick review please"]))
        # Now and then an MCP agent checks its person's ball, as an AI engineer's assistant would.
        if self.rng.random() < .05:
            self.call(p.ai, "POST", "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                             "params": {"name": "my_ball", "arguments": {}}}, why="MCP my_ball")
            self.counts["mcp_calls"] += 1

    def finish_tasks(self, p: Project) -> None:
        """People finish the work of the current stage over its duration; everything is done by its end."""
        st = p.stage
        frac = (p.stage_day + 1) / p.durations[st]
        tasks = self.call(p.pm, "GET", f"/api/tasks?deployment_id={p.dep_id}") or []
        for t in tasks:
            if t["status"] == "done" or t["stage"] != st or t["id"] in self.held:
                continue
            owner = t.get("assignee_id")
            if not owner or owner not in self.tokens:
                continue
            blocked_by_customer = p.profile == "customer_slow" and self.rng.random() < .08
            if blocked_by_customer and t["status"] != "blocked":
                self.call(owner, "PATCH", f"/api/tasks/{t['id']}", {"status": "blocked", "waiting_on": "customer",
                                                                    "blocked_reason": "Waiting on their IT team"})
                self.counts["tasks_blocked"] += 1
                continue
            if t["ball"]["out"]:
                continue  # it comes back first
            if frac >= .95 or self.rng.random() < frac * .35:
                if self.call(owner, "PATCH", f"/api/tasks/{t['id']}", {"status": "done"}, why="finish task"):
                    self.counts["tasks_done"] += 1
            elif t["status"] == "open" and self.rng.random() < .3:
                self.call(owner, "PATCH", f"/api/tasks/{t['id']}", {"status": "in_progress"})

    def stage_ready(self, p: Project) -> bool:
        tasks = self.call(p.pm, "GET", f"/api/tasks?deployment_id={p.dep_id}") or []
        open_ = [t for t in tasks if t["stage"] == p.stage and t["status"] != "done"]
        if p.stage_day >= p.durations[p.stage] and open_:
            # Past its time: the lead pulls stray balls home and closes what's left.
            for t in open_:
                if t["ball"]["out"]:
                    self.call(p.pm, "POST", f"/api/tasks/{t['id']}/return", {"note": "Closing the stage"},
                              why="PM calls the ball back")
                    self.held.pop(t["id"], None)
                self.call(p.pm, "PATCH", f"/api/tasks/{t['id']}", {"status": "done"}, why="PM closes task")
            return True
        return p.stage_day >= p.durations[p.stage] and not open_

    # ------------------------------------------------------------- money

    def billing(self, p: Project) -> None:
        cm = self.call(p.pm, "GET", f"/api/deployments/{p.dep_id}/commercials")
        if not cm:
            return
        for s in cm.get("sows", []):
            for m in s.get("milestones", []):
                mid, stt = m["id"], m["status"]
                if stt in ("ready", "changes_requested"):
                    if self.call(p.pm, "POST", f"/api/milestones/{mid}/submit",
                                 {"note": "Evidence attached; see the stage record"}, why="submit milestone"):
                        self.counts["milestones_submitted"] += 1
                        p.events.append(("signoff", mid, self.day_n + (self.rng.randint(3, 10)
                                                                       if p.profile == "customer_slow" else
                                                                       self.rng.randint(1, 4))))
                elif stt == "accepted" and self.rng.random() < .3:
                    if self.call(self.head, "POST", f"/api/milestones/{mid}/invoiced",
                                 {"invoice_ref": f"INV-{p.n:03d}-{mid[-4:]}", "on": str(self.day)}):
                        self.counts["milestones_invoiced"] += 1
                        p.events.append(("paid", mid, self.day_n + self.rng.randint(15, 45)))

    def due_events(self, p: Project) -> None:
        for ev in [e for e in p.events if e[2] <= self.day_n]:
            p.events.remove(ev)
            kind, ref, _ = ev
            if kind == "approval":
                if self.call(p.em, "POST", f"/api/approvals/{ref}/decide",
                             {"approve": self.rng.random() < .85, "note": "Scoped to staging"}):
                    self.counts["approvals_decided"] += 1
            elif kind == "signoff" and p.stakeholder:
                pk = self.call(p.stakeholder, "GET", f"/api/milestones/{ref}/packet")
                if not pk:
                    continue
                h = pk.get("customer_hash") or pk.get("hash") or (pk.get("packet") or {}).get("hash")
                if self.rng.random() < .1:
                    if self.call(p.stakeholder, "POST", f"/api/milestones/{ref}/request-changes",
                                 {"packet_hash": h, "note": "Please include the UAT case list"}):
                        self.counts["milestones_changes_requested"] += 1
                else:
                    if self.call(p.stakeholder, "POST", f"/api/milestones/{ref}/accept",
                                 {"packet_hash": h, "note": "Accepted"}, why="customer signs off"):
                        self.counts["milestones_accepted"] += 1
            elif kind == "paid":
                if self.call(self.head, "POST", f"/api/milestones/{ref}/paid", {"on": str(self.day)}):
                    self.counts["milestones_paid"] += 1
            elif kind == "unhold":
                if self.call(p.em, "POST", f"/api/deployments/{p.dep_id}/hold", {"on": False}):
                    p.pause = None
                    self.counts["holds_lifted"] += 1

    # --------------------------------------------------------------- the days

    def time_entries(self) -> None:
        """Everyone on delivery logs their day across the projects they're staffed on."""
        live = [p for p in self.projects if p.dep_id and not p.done and p.stage_i >= 0]
        by_person = defaultdict(list)
        for p in live:
            for u in p.team:
                if self.roles.get(u) != "customer":
                    by_person[u].append(p)
        for u, ps in by_person.items():
            if self.rng.random() < .08:
                continue  # out, or forgot
            hours = 8 * self.rng.uniform(.8, 1.05)
            share = hours / len(ps)
            for p in ps:
                h = round(share * (1.4 if p.profile == "scope_creep" else 1), 1)
                if h > 0 and self.call(u, "POST", "/api/time", {"day": str(self.day), "hours": min(h, 12),
                                                                "deployment_id": p.dep_id}, why="log time"):
                    self.counts["hours_logged"] += min(h, 12)

    def weekly(self) -> None:
        self.call(self.head, "POST", "/api/sweep", why="flag rules")
        self.call(self.head, "GET", "/api/portfolio")
        self.call(self.head, "GET", "/api/team")
        self.call(self.head, "GET", "/api/dashboard")
        live = [p for p in self.projects if p.dep_id and not p.done]
        for em in self.by_role["engagement_manager"]:
            q = self.call(em, "GET", "/api/delays/queue") or []
            rows = q if isinstance(q, list) else q.get("items", q.get("queue", []))
            ids = [x["id"] for x in rows if isinstance(x, dict) and x.get("status") in (None, "open")][:40]
            if ids:
                if self.call(em, "POST", "/api/delays/batch", {"span_ids": ids}, why="confirm delays in a batch"):
                    self.counts["delays_confirmed"] += len(ids)
            for f in (self.call(em, "GET", "/api/flags") or [])[:6]:
                if f.get("status") == "open" and self.rng.random() < .4:
                    self.call(em, "POST", f"/api/flags/{f['id']}/take", {"note": "On it"}, refusal_ok=(403, 409))
        for p in live:
            if p.stage_i < 0:
                continue
            r = self.call(p.pm, "POST", f"/api/deployments/{p.dep_id}/reports", {"audience": "customer"},
                          why="weekly customer status report")
            if r and self.rng.random() < .7:
                rid = r.get("id")
                if rid and self.call(p.pm, "POST", f"/api/reports/{rid}/share", {"shared": True}):
                    self.counts["reports_shared"] += 1
            # The AI engineer records last month's model spend (no native tracking: a custom field).
            if self.day.day <= 7:
                spend = MODELS[p.model]["spend"] * {"discover": .05, "integrate": .2, "test": .4, "golive": .8,
                                                    "adopt": 1, "value": 1}[p.stage] * self.rng.uniform(.7, 1.3)
                self.call(p.ai, "PATCH", f"/api/deployments/{p.dep_id}", {"fields": {"ai_spend_month_usd": round(spend)}},
                          why="record model spend")
            # Scope creep: the budget grows once, mid-project.
            if p.profile == "scope_creep" and p.stage == "integrate" and p.stage_day > 3 and "crept" not in p.tasks:
                p.tasks["crept"] = []
                self.task(p, p.fdes[0], f"Customer asked for {self.pick(['a second', 'another'])} system: "
                                        f"{self.pick(['Workday', 'ServiceNow', 'Jira', 'DocuSign'])}", due_days=10)
                self.call(p.em, "PATCH", f"/api/deployments/{p.dep_id}", {"budget_hours": p.budget_hours * 1.2})
                self.counts["budget_increases"] += 1
            # Customers look at where they stand.
            if p.stakeholder and self.rng.random() < .5:
                self.call(p.stakeholder, "GET", f"/api/deployments/{p.dep_id}")
                self.call(p.stakeholder, "GET", "/api/ball")
                self.call(p.stakeholder, "GET", f"/api/deployments/{p.dep_id}/commercials")
        if self.day.day <= 7:
            self.call(self.head, "GET", "/api/commercials")
            self.call(self.head, "GET", "/api/commercials.csv")
            v = self.call(self.head, "GET", "/api/audit/verify")
            if v and not v.get("ok"):
                self.issues.append({"day": str(self.day), "call": "audit verify", "detail": v, "status": "invariant"})

    def one_day(self) -> None:
        self._tick(self.day)
        for p in self.projects:
            if p.done:
                continue
            if not p.dep_id:
                if p.start <= self.day:
                    self.open_project(p)
                continue
            self.due_events(p)
            if p.pause:
                continue
            # Paused projects go on hold once, for two or three weeks, waiting on the customer.
            if p.profile == "paused" and p.stage == "integrate" and p.stage_day == 2 and "paused" not in p.tasks:
                p.tasks["paused"] = []
                if self.call(p.em, "POST", f"/api/deployments/{p.dep_id}/hold",
                             {"on": True, "reason": "Customer budget review", "waiting_on": "customer"}):
                    p.pause = (self.day_n, self.day_n + self.rng.randint(10, 15))
                    p.events.append(("unhold", None, p.pause[1]))
                    self.counts["holds"] += 1
                continue
            if p.stalled_at is not None and p.stage_i == p.stalled_at and p.stage_day == 5:
                self.call(p.em, "POST", f"/api/deployments/{p.dep_id}/flags",
                          {"text": "Customer froze the program pending a reorg; no date to restart", "severity": "high"})
                self.call(p.em, "PATCH", f"/api/deployments/{p.dep_id}", {"health": "blocked"})
                self.counts["stalled"] += 1
                p.stalled_at = -99  # stays here
                continue
            if p.stalled_at == -99:
                continue
            self.stage_work(p)
            self.ball_day(p)
            self.finish_tasks(p)
            p.stage_day += 1
            if self.stage_ready(p):
                self.advance(p)
            if p.stage_i >= 0 and self.rng.random() < .5:
                self.billing(p)
        self.time_entries()
        if self.day.weekday() == 0:
            self.weekly()
        # Someone leaves halfway through; their work goes to a peer.
        if self.day_n == 70:
            self.offboard(self.by_role["fde"][-1], self.by_role["fde"][0])

    def offboard(self, leaver: str, heir: str) -> None:
        """Someone leaves. Their EMs staff the heir on their deployments first, the way a team would,
        so the Head's offboarding can hand every open task over rather than leave it unassigned."""
        theirs = [p for p in self.projects if leaver in p.fdes and p.dep_id and not p.done]
        for p in theirs:
            if heir not in p.team:
                self.call(p.em, "POST", f"/api/deployments/{p.dep_id}/members", {"user_id": heir})
                self.call(p.em, "PUT", f"/api/deployments/{p.dep_id}/members/{heir}", {"allocation": .5})
        r = self.call(self.head, "POST", f"/api/people/{leaver}/deactivate", {"reassign_to": heir},
                      why="offboard an FDE")
        if not r:
            return
        self.counts["offboarded"] += 1
        self.counts["offboard_tasks_reassigned"] += r.get("tasks_reassigned", 0)
        self.counts["offboard_tasks_unassigned"] += r.get("tasks_unassigned", 0)
        self.by_role["fde"].remove(leaver)
        for p in self.projects:
            p.fdes = list(dict.fromkeys(heir if f == leaver else f for f in p.fdes))
        for tid, (h, b, pn) in list(self.held.items()):
            if h == leaver:
                self.held.pop(tid)
        self.log(f"{self.names[leaver]} left; {len(theirs)} deployments and their work went to {self.names[heir]}")

    def run(self) -> dict:
        t0 = _real_clock()
        self.bootstrap()
        self.configure()
        self.hire()
        self.plan_projects()
        end = START + timedelta(weeks=self.weeks)
        d = START
        while d < end:
            self.day = d
            if weekday(d):
                self.one_day()
                self.day_n += 1
            d += timedelta(days=1)
        self._tick(end)
        result = self.check()
        result["seconds"] = round(_real_clock() - t0, 1)
        self._clock.stop()
        return result

    # --------------------------------------------------------------- invariants

    def check(self) -> dict:
        inv = {}
        v = self.call(self.head, "GET", "/api/audit/verify") or {}
        inv["audit_chain_intact"] = bool(v.get("ok"))
        # Tenant isolation: another workspace's head sees none of it.
        leaks = 0
        for p in self.projects[:20]:
            if p.dep_id:
                r = self.client.get(f"/api/deployments/{p.dep_id}", headers={"Authorization": "Bearer fwu_sim_other"})
                leaks += r.status_code != 404
        other_view = self.client.get("/api/portfolio", headers={"Authorization": "Bearer fwu_sim_other"}).json()
        inv["tenant_isolation"] = leaks == 0 and not other_view.get("deployments")
        # Customers see only their own deployment.
        cust_leak = 0
        for p in self.projects[:20]:
            if not p.stakeholder:
                continue
            for q in self.projects[:20]:
                if q is p or not q.dep_id:
                    continue
                r = self.client.get(f"/api/deployments/{q.dep_id}",
                                    headers={"Authorization": f"Bearer {self.tokens[p.stakeholder]}"})
                cust_leak += r.status_code != 404
            ts = self.client.get(f"/api/tasks?deployment_id={p.dep_id}",
                                 headers={"Authorization": f"Bearer {self.tokens[p.stakeholder]}"}).json()
            cust_leak += sum(1 for t in ts if t.get("visibility") != "shared")
        inv["customer_isolation"] = cust_leak == 0
        c = self.conn
        T = self.tenant
        q = lambda sql, *a: c.execute(sql, a).fetchall()
        # The ball: no done task holds a ball out; every ball out has an open pass to its holder; no
        # open pass on a done task.
        bad_ball = q("SELECT id FROM tasks WHERE tenant_id=? AND status='done' AND holder_id IS NOT NULL", T)
        orphan = q("SELECT t.id FROM tasks t WHERE t.tenant_id=? AND t.holder_id IS NOT NULL AND NOT EXISTS ("
                   "SELECT 1 FROM ball_passes b WHERE b.task_id=t.id AND b.back_at IS NULL AND b.to_id=t.holder_id)", T)
        stale = q("SELECT b.id FROM ball_passes b JOIN tasks t ON t.id=b.task_id WHERE b.tenant_id=? AND "
                  "b.back_at IS NULL AND t.status='done'", T)
        inactive = q("SELECT t.id FROM tasks t JOIN users u ON u.id=t.holder_id WHERE t.tenant_id=? AND u.active=0", T)
        inv["ball_consistent"] = not (bad_ball or orphan or stale or inactive)
        inv["ball_detail"] = {"done_but_out": len(bad_ball), "holder_without_pass": len(orphan),
                              "open_pass_on_done": len(stale), "held_by_inactive": len(inactive)}
        # The ledger: no open span for a ball that's home.
        open_ball_spans = q("SELECT d.dedupe_key FROM delays d WHERE d.tenant_id=? AND d.ended_at IS NULL "
                            "AND d.dedupe_key LIKE 'ball:%'", T)
        mismatched = 0
        for r in open_ball_spans:
            tid = r["dedupe_key"].split(":", 1)[1]
            t = c.execute("SELECT holder_id, assignee_id FROM tasks WHERE id=?", (tid,)).fetchone()
            h = c.execute("SELECT role FROM users WHERE id=?", (t["holder_id"],)).fetchone() if t and t["holder_id"] else None
            mismatched += not (h and h["role"] == "customer")
        inv["ledger_matches_ball"] = mismatched == 0
        # Money: milestone amounts add up to each SOW; states only move forward.
        money_bad = q("SELECT s.id FROM sows s WHERE s.tenant_id=? AND abs(s.total_value - (SELECT COALESCE(SUM(amount),0) "
                      "FROM milestones m WHERE m.sow_id=s.id)) > 1", T)
        paid_unaccepted = q("SELECT id FROM milestones WHERE tenant_id=? AND status IN ('invoiced','paid') "
                            "AND decided_at IS NULL", T)
        inv["money_reconciles"] = not money_bad and not paid_unaccepted
        # Every completed project has every stage done and its milestones at least ready.
        incomplete = []
        for p in self.projects:
            if p.done:
                st = c.execute("SELECT COUNT(*) n FROM deployment_stages WHERE deployment_id=? AND state='done'",
                               (p.dep_id,)).fetchone()["n"]
                if st < len(STAGES):
                    incomplete.append(p.n)
        inv["completed_projects_fully_done"] = not incomplete
        portfolio = self.call(self.head, "GET", "/api/portfolio") or {}
        commercials = self.call(self.head, "GET", "/api/commercials") or {}
        rollup = self.call(self.head, "GET", "/api/delays/rollup") or {}
        stage_mix = Counter(p.stage if not p.done else "complete" for p in self.projects if p.dep_id)
        model_out = defaultdict(lambda: {"projects": 0, "conformance_failures": 0, "completed": 0, "days": []})
        for p in self.projects:
            m = model_out[p.model]
            m["projects"] += 1
            m["conformance_failures"] += p.conformance_failed
            if p.done:
                m["completed"] += 1
        hours = q("SELECT d.id, d.budget_hours, COALESCE((SELECT SUM(hours) FROM time_entries e WHERE e.deployment_id=d.id),0) h "
                  "FROM deployments d WHERE d.tenant_id=?", T)
        over = sum(1 for r in hours if r["budget_hours"] and r["h"] > r["budget_hours"])
        lat = sorted(self.timing)
        server_errors = [i for i in self.issues if isinstance(i.get("status"), int) and i["status"] >= 500]
        return {
            "workspace": "Northstar Deploy", "simulated": f"{START} to {START + timedelta(weeks=self.weeks)}",
            "projects": len(self.projects), "opened": self.counts["projects_opened"],
            "people": {r: len(v) for r, v in self.by_role.items()},
            "customers_with_logins": sum(1 for p in self.projects if p.stakeholder),
            "stage_mix_at_end": dict(stage_mix),
            "profiles": dict(Counter(p.profile for p in self.projects)),
            "models": {k: {**v, "days": None, **dict(self.by_model[k]),
                           "first_run_pass_rate": round(self.by_model[k]["first_run_passed"] /
                                                        self.by_model[k]["first_runs"], 2)
                           if self.by_model[k]["first_runs"] else None}
                       for k, v in model_out.items()},
            "counts": {k: (round(v) if isinstance(v, float) else v) for k, v in sorted(self.counts.items())},
            "over_hours_budget": over,
            "api": {"calls": sum(self.calls.values()), "ok": sum(self.ok.values()),
                    "expected_refusals": sum(self.expected_refusals.values()),
                    "unexpected": len(self.issues), "server_errors": len(server_errors),
                    "p50_ms": round(lat[len(lat) // 2], 1) if lat else None,
                    "p95_ms": round(lat[int(len(lat) * .95)], 1) if lat else None,
                    "p99_ms": round(lat[int(len(lat) * .99)], 1) if lat else None,
                    "routes_exercised": len(self.calls)},
            "invariants": inv,
            "portfolio_kpis": portfolio.get("kpis"),
            "commercials_totals": commercials.get("totals") if isinstance(commercials, dict) else None,
            "delay_rollup": {k: rollup.get(k) for k in ("confirmed_rows", "unconfirmed_rows", "total_days", "owners")},
            "issues_by_call": dict(Counter(f"{i['call']} -> {i['status']}" for i in self.issues).most_common()),
            "issues": self.issues[:400],
            "expected_refusals": dict(self.expected_refusals),
            "calls_by_route": dict(self.calls.most_common()),
            "logins": {self.names[u]: {"role": self.roles[u], "token": t} for u, t in self.tokens.items()
                       if u in self.names and self.roles.get(u) != "customer" and u != "other"}
                      | {self.names[p.stakeholder] + f" ({p.customer})": {"role": "customer",
                                                                           "token": self.tokens[p.stakeholder]}
                         for p in self.projects[:5] if p.stakeholder},
        }


def main(db_url: str, projects: int, weeks: int, seed: int, out: str | None, quiet: bool) -> int:
    sim = Sim(db_url, projects=projects, weeks=weeks, seed=seed, verbose=not quiet)
    res = sim.run()
    path = out or "simulation-report.json"
    with open(path, "w") as f:
        json.dump(res, f, indent=2, default=str)
    a, inv = res["api"], res["invariants"]
    print(f"\n{res['projects']} projects over {weeks} weeks · {a['calls']:,} API calls · "
          f"{a['unexpected']} unexpected · {a['server_errors']} server errors · p95 {a['p95_ms']} ms · {res['seconds']} s")
    for k, v in inv.items():
        if k != "ball_detail":
            print(f"  {'✓' if v else '✗'} {k.replace('_', ' ')}")
    print(f"Report: {path}")
    return 0 if a["server_errors"] == 0 and all(v for k, v in inv.items() if k != "ball_detail") else 1
