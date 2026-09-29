"""Per-tenant configuration: what makes Fieldwork a platform, not a methodology.

Deployments work similarly enough across companies that a strong default
template works out of the box, and differently enough that every team needs to
make it its own. So each workspace owns:

- branding     white-label product name, accent color, logo
- roles        its own roles (Engagement Manager, FDE, ...), named its own way
- permissions  action -> {role: scope}. scope "all" = every deployment in the
               workspace; "own" = only deployments the person is staffed on
- views        what each role sees on its home screen
- stages       the deployment chain, in order, each with an engine
- engines      its own scripts and services, plugged in next to the built-ins
- fields       custom fields on customers and deployments

validate() is strict so a bad edit fails loudly instead of corrupting the
workspace, and a workspace can never lock itself out of its own settings.
"""

from __future__ import annotations

import copy
import re
from urllib.parse import urlparse

SCOPES = ("own", "all")
FIELD_TYPES = ("text", "number", "date", "select", "bool")
BUILTIN_ENGINES = ("none", "sendero", "threshold",
                   "census", "cortex", "conformance", "golive", "attribution", "value_study")
WIDGETS = {
    "today": "Today: what needs me",
    "kpis": "Headline numbers",
    "chain": "Deployments by stage",
    "my_tasks": "My open tasks",
    "team": "Team load",
    "findings": "Findings awaiting confirmation",
    "clients": "My deployments as cards",
    "hours": "My hours this week",
    "flags": "Open flags and escalations",
    "capacity": "Team capacity this week",
    "unassigned": "Unassigned work",
    "pipeline": "Sales pipeline and staffing checks",
    "delays": "Who owns the delay",
    "approvals": "Agent actions waiting for approval",
}

# Actions whose "own" scope has no meaning (they aren't about one deployment).
WORKSPACE_ACTIONS = {"deployment.create", "people.read", "people.manage", "audit.read",
                     "audit.verify", "engine.manage", "config.edit", "integrations.manage",
                     "pipeline.view", "pipeline.edit"}

EVENTS = {
    "task.assigned": "A task is assigned to someone",
    "task.blocked": "A task is marked blocked",
    "task.done": "A task is completed",
    "finding.created": "An engine result is waiting for confirmation",
    "finding.confirmed": "A finding is confirmed",
    "finding.shared": "A finding is shared with the customer",
    "deployment.advanced": "A deployment moves stage",
    "deployment.health": "A deployment turns at risk or blocked",
    "report.created": "A status report is drafted",
    "flag.raised": "A flag is raised on a deployment",
    "delay.opened": "A delay opens and needs an owner confirmed",
    "approval.requested": "An agent action is waiting for approval",
    "milestone.ready": "A billable milestone is ready to submit for sign-off",
    "milestone.submitted": "A milestone is waiting for the customer's sign-off",
    "milestone.accepted": "The customer signed off a milestone (ready to invoice)",
    "milestone.changes_requested": "The customer asked for changes before signing off",
}
TRACKERS = ("github", "linear", "jira")

ACTIONS = {
    "deployment.view":    "See deployments",
    "deployment.create":  "Open a new customer deployment",
    "deployment.advance": "Move a deployment to its next stage and mark stages done, skipped or in progress",
    "deployment.jump":    "Move a deployment to any stage, either direction",
    "deployment.edit":    "Edit a deployment's health and details",
    "deployment.staff":   "Add or remove people on a deployment",
    "task.create":        "Create tasks for themselves",
    "task.assign":        "Assign tasks to other people",
    "task.update_any":    "Update other people's tasks",
    "engine.run":         "Run engines on a deployment",
    "finding.confirm":    "Confirm engine findings into the record",
    "bench.match":        "Match people to a deployment's staffing needs",
    "people.read":        "See the team roster and workload",
    "people.manage":      "Add people and change their roles",
    "task.view_internal": "See internal tasks (not just ones shared with the customer)",
    "finding.view_internal": "See internal findings (not just ones shared with the customer)",
    "customer.share":     "Share tasks and findings with the customer",
    "report.create":      "Draft status reports",
    "integrations.manage": "Connect Slack, webhooks, GitHub, Linear and Jira",
    "audit.read":         "Read the audit trail",
    "audit.verify":       "Verify the audit hash chain",
    "engine.manage":      "Register and manage engines (scripts and integrations)",
    "config.edit":        "Edit workspace settings",
    "delay.confirm":      "Confirm or reassign who owns a delay",
    "flag.handle":        "Take, hand back and resolve flags",
    "approval.decide":    "Approve or reject actions agents ask to take",
    "pipeline.view":      "See the sales pipeline and staffing checks",
    "pipeline.edit":      "Add and update pipeline opportunities",
    "sow.edit":           "Set up statements of work and billable milestones, and record invoicing",
    "milestone.submit":   "Submit a finished milestone for the customer's sign-off",
    "milestone.accept":   "Sign off (or ask for changes to) a milestone, as the customer",
    "billing.view":       "See contract value, billing status and what's ready to invoice",
}

DOERS = ("implementation_consultant", "fde", "ai_engineer")

DEFAULT_CONFIG: dict = {
    "branding": {"product_name": "Fieldwork", "accent": "#e8b25c", "logo_url": ""},
    "roles": [
        {"key": "head", "name": "Head of Deployments"},
        {"key": "engagement_manager", "name": "Engagement Manager"},
        {"key": "implementation_consultant", "name": "Implementation Consultant"},
        {"key": "fde", "name": "Forward Deployed Engineer"},
        {"key": "ai_engineer", "name": "AI Engineer"},
        {"key": "customer", "name": "Customer Stakeholder"},
    ],
    "permissions": {
        "deployment.view":    {"head": "all", "engagement_manager": "own", "customer": "own",
                               **{r: "own" for r in DOERS}},
        "deployment.create":  {"head": "all", "engagement_manager": "all"},
        "deployment.advance": {"head": "all", "engagement_manager": "own", **{r: "own" for r in DOERS}},
        "deployment.jump":    {"head": "all", "engagement_manager": "own", **{r: "own" for r in DOERS}},
        "deployment.edit":    {"head": "all", "engagement_manager": "own", **{r: "own" for r in DOERS}},
        "deployment.staff":   {"head": "all", "engagement_manager": "own"},
        "task.create":        {"head": "all", "engagement_manager": "own", **{r: "own" for r in DOERS}},
        "task.assign":        {"head": "all", "engagement_manager": "own"},
        "task.update_any":    {"head": "all", "engagement_manager": "own"},
        "engine.run":         {"head": "all", "engagement_manager": "own", **{r: "own" for r in DOERS}},
        "finding.confirm":    {"head": "all", "engagement_manager": "own"},
        "bench.match":        {"head": "all", "engagement_manager": "own"},
        "people.read":        {"head": "all", "engagement_manager": "all"},
        "people.manage":      {"head": "all"},
        "audit.read":         {"head": "all"},
        "audit.verify":       {"head": "all"},
        "engine.manage":      {"head": "all"},
        "config.edit":        {"head": "all"},
        "task.view_internal": {"head": "all", "engagement_manager": "own", **{r: "own" for r in DOERS}},
        "finding.view_internal": {"head": "all", "engagement_manager": "own", **{r: "own" for r in DOERS}},
        "customer.share":     {"head": "all", "engagement_manager": "own"},
        "report.create":      {"head": "all", "engagement_manager": "own", **{r: "own" for r in DOERS}},
        "integrations.manage": {"head": "all"},
        "delay.confirm":      {"head": "all", "engagement_manager": "own"},
        "flag.handle":        {"head": "all", "engagement_manager": "own"},
        "approval.decide":    {"head": "all", "engagement_manager": "own"},
        "pipeline.view":      {"head": "all", "engagement_manager": "all"},
        "pipeline.edit":      {"head": "all", "engagement_manager": "all"},
        "sow.edit":           {"head": "all", "engagement_manager": "own"},
        "milestone.submit":   {"head": "all", "engagement_manager": "own"},
        "milestone.accept":   {"customer": "own"},
        "billing.view":       {"head": "all", "engagement_manager": "own"},
    },
    "views": {
        "head":                      ["kpis", "chain", "flags", "capacity", "delays", "findings", "pipeline"],
        "engagement_manager":        ["kpis", "chain", "capacity", "unassigned", "flags", "approvals",
                                      "findings", "today"],
        "implementation_consultant": ["clients", "my_tasks", "hours", "today"],
        "fde":                       ["clients", "my_tasks", "hours", "today"],
        "ai_engineer":               ["clients", "my_tasks", "hours", "approvals", "today"],
        "customer":                  ["clients", "my_tasks"],
    },
    "stages": [
        {"key": "discover",  "name": "Discover",  "engines": ["census"], "target_days": 14,
         "exit_criteria": "Systems inventory and evidence gaps written up and read out"},
        {"key": "integrate", "name": "Integrate", "engines": ["cortex"], "target_days": 21,
         "exit_criteria": "Agents connected with scoped permissions and human gates"},
        {"key": "test",      "name": "Test",      "engines": ["conformance"], "target_days": 14,
         "exit_criteria": "Conformance suite green on customer data"},
        {"key": "golive",    "name": "Go-live",   "engines": ["golive"], "target_days": 7,
         "exit_criteria": "Cutover complete, command center stood down"},
        {"key": "adopt",     "name": "Adopt",     "engines": ["sendero"], "target_days": 30,
         "exit_criteria": "Friction points classified build vs training and routed"},
        {"key": "value",     "name": "Value",     "engines": ["value_study", "attribution"], "target_days": 30,
         "exit_criteria": "Outcome measured and confirmed with the customer"},
    ],
    "integrations": {
        "slack": {"enabled": False, "events": ["task.blocked", "finding.created", "deployment.advanced",
                                               "deployment.health", "report.created"]},
        "webhooks": [],
        "github": {"enabled": False, "api_base": "https://api.github.com"},
        "linear": {"enabled": False, "api_base": "https://api.linear.app"},
        "jira": {"enabled": False, "base_url": "", "email": ""},
    },
    "sso": {"enabled": False, "issuer": "", "client_id": "", "allowed_domains": [],
            "jit_role": "", "required": False},
    "engines": [],   # customer-registered engines; see engines.py for shapes
    "fields": {
        "customer": [
            {"key": "arr", "label": "Contract value (ARR)", "type": "number"},
            {"key": "exec_sponsor", "label": "Executive sponsor", "type": "text"},
        ],
        "deployment": [
            {"key": "target_golive", "label": "Target go-live", "type": "date"},
            {"key": "tier", "label": "Tier", "type": "select", "options": ["Pilot", "Standard", "Strategic"]},
        ],
    },
}

DEFAULT_TARGETS = {s["key"]: s["target_days"] for s in DEFAULT_CONFIG["stages"]}

_KEY = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")


class ConfigError(ValueError):
    pass


def default() -> dict:
    return copy.deepcopy(DEFAULT_CONFIG)


def _key(val, where: str) -> str:
    if not isinstance(val, str) or not _KEY.match(val):
        raise ConfigError(f"{where}: bad key {val!r} (start with a letter; lowercase, digits, underscore)")
    return val


def _url_ok(u: str, allow_http: bool = False) -> bool:
    p = urlparse(u)
    return p.scheme in (("https", "http") if allow_http else ("https",)) and bool(p.netloc)


def validate(cfg: dict, allow_http_engines: bool = False, known_urls: frozenset = frozenset()) -> dict:
    """Return a normalized copy or raise ConfigError with a specific message.

    known_urls: engine URLs already registered (e.g. by an operator on a
    self-hosted install) that stay valid when other settings are edited.
    """
    if not isinstance(cfg, dict):
        raise ConfigError("config must be an object")
    base = default()
    out: dict = {}

    # branding
    b = {**base["branding"], **(cfg.get("branding") or {})}
    name = str(b.get("product_name") or "").strip()[:40]
    if not name:
        raise ConfigError("branding.product_name can't be empty")
    if not _HEX.match(str(b.get("accent", ""))):
        raise ConfigError("branding.accent must be a hex color like #e8b25c")
    logo = str(b.get("logo_url") or "").strip()
    if logo and not (_url_ok(logo) or re.match(r"^data:image/(png|svg\+xml|jpeg|webp);base64,", logo)):
        raise ConfigError("branding.logo_url must be an https URL or a data:image URL")
    if len(logo) > 200_000:
        raise ConfigError("branding.logo_url is too large (keep logos under ~150 KB)")
    out["branding"] = {"product_name": name, "accent": b["accent"].lower(), "logo_url": logo}

    # integrations (secrets live in tenant_secrets, never here)
    ints = upgrade({"integrations": cfg.get("integrations") or {}})["integrations"]
    out["integrations"] = {}
    sl = ints["slack"]
    bad = [e for e in sl.get("events", []) if e not in EVENTS]
    if bad:
        raise ConfigError(f"integrations.slack.events: unknown event(s) {bad}")
    out["integrations"]["slack"] = {"enabled": bool(sl.get("enabled")), "events": list(dict.fromkeys(sl.get("events", [])))}
    hooks, hseen = [], set()
    for h in ints.get("webhooks", []):
        hid = _key(h.get("id"), "integrations.webhooks")
        if hid in hseen:
            raise ConfigError(f"integrations.webhooks: duplicate id {hid!r}")
        hseen.add(hid)
        url = str(h.get("url") or "").strip()
        if not _url_ok(url, allow_http_engines):
            raise ConfigError(f"integrations.webhooks.{hid}: url must be https")
        evs = h.get("events") or list(EVENTS)
        if [e for e in evs if e not in EVENTS]:
            raise ConfigError(f"integrations.webhooks.{hid}: unknown event(s)")
        hooks.append({"id": hid, "url": url, "events": list(dict.fromkeys(evs))})
    out["integrations"]["webhooks"] = hooks
    for t in ("github", "linear"):
        v = ints[t]
        api = str(v.get("api_base") or "").rstrip("/")
        if not _url_ok(api, allow_http_engines):
            raise ConfigError(f"integrations.{t}.api_base must be https")
        out["integrations"][t] = {"enabled": bool(v.get("enabled")), "api_base": api}
    j = ints["jira"]
    jb = str(j.get("base_url") or "").rstrip("/")
    if j.get("enabled") and not _url_ok(jb, allow_http_engines):
        raise ConfigError("integrations.jira.base_url must be your https Atlassian site, e.g. https://yourco.atlassian.net")
    out["integrations"]["jira"] = {"enabled": bool(j.get("enabled")), "base_url": jb,
                                   "email": str(j.get("email") or "").strip()[:200]}

    # company sign-in (validated against roles below)
    sso = {**base["sso"], **(cfg.get("sso") or {})}
    out["sso"] = {"enabled": bool(sso["enabled"]), "issuer": str(sso["issuer"]).strip().rstrip("/"),
                  "client_id": str(sso["client_id"]).strip()[:200],
                  "allowed_domains": [str(d).strip().lower().lstrip("@") for d in sso.get("allowed_domains") or []
                                      if str(d).strip()][:20],
                  "jit_role": str(sso.get("jit_role") or ""), "required": bool(sso["required"])}
    if out["sso"]["enabled"]:
        if not (_url_ok(out["sso"]["issuer"]) or (allow_http_engines and _url_ok(out["sso"]["issuer"], True))):
            raise ConfigError("sso.issuer must be the identity provider's https issuer URL")
        if not out["sso"]["client_id"]:
            raise ConfigError("sso.client_id is required")
    if out["sso"]["required"] and not out["sso"]["enabled"]:
        raise ConfigError("sso.required needs sso.enabled")

    # roles
    roles = cfg.get("roles", base["roles"])
    if not isinstance(roles, list) or not 1 <= len(roles) <= 20:
        raise ConfigError("roles: need between 1 and 20 roles")
    seen: set = set()
    out["roles"] = []
    for r in roles:
        k = _key(r.get("key"), "roles")
        if k in seen:
            raise ConfigError(f"roles: duplicate key {k!r}")
        seen.add(k)
        out["roles"].append({"key": k, "name": str(r.get("name") or k).strip()[:60]})
    role_keys = seen
    if out["sso"]["jit_role"] and out["sso"]["jit_role"] not in role_keys:
        raise ConfigError(f"sso.jit_role: unknown role {out['sso']['jit_role']!r}")

    # permissions
    perms = cfg.get("permissions", base["permissions"])
    unknown = set(perms) - set(ACTIONS)
    if unknown:
        raise ConfigError(f"permissions: unknown action(s) {sorted(unknown)}")
    out["permissions"] = {}
    for action in ACTIONS:
        grants = perms.get(action, {})
        if not isinstance(grants, dict):
            raise ConfigError(f"permissions.{action}: expected {{role: scope}}")
        clean = {}
        for role, scope in grants.items():
            if role not in role_keys:
                raise ConfigError(f"permissions.{action}: unknown role {role!r}")
            if scope not in SCOPES:
                raise ConfigError(f"permissions.{action}.{role}: scope must be 'own' or 'all'")
            if action in WORKSPACE_ACTIONS and scope != "all":
                raise ConfigError(f"permissions.{action}: workspace-wide action, scope must be 'all'")
            clean[role] = scope
        out["permissions"][action] = clean
    if not out["permissions"]["config.edit"]:
        raise ConfigError("permissions.config.edit: at least one role must be able to edit settings")

    # views
    views = cfg.get("views", {})
    out["views"] = {}
    for rk in role_keys:
        ws = views.get(rk, base["views"].get(rk, ["my_tasks", "chain"]))
        bad = [w for w in ws if w not in WIDGETS]
        if bad:
            raise ConfigError(f"views.{rk}: unknown widget(s) {bad}")
        out["views"][rk] = list(dict.fromkeys(ws))
    stray = set(views) - role_keys
    if stray:
        raise ConfigError(f"views: unknown role(s) {sorted(stray)}")

    # custom engines (declared before stages so stages can reference them)
    engines = cfg.get("engines", [])
    out["engines"] = []
    eseen: set = set()
    for e in engines:
        k = _key(e.get("key"), "engines")
        if k in BUILTIN_ENGINES or k in eseen:
            raise ConfigError(f"engines: key {k!r} is taken")
        eseen.add(k)
        kind = e.get("kind")
        if kind not in ("webhook", "push"):
            raise ConfigError(f"engines.{k}: kind must be 'webhook' (we call it) or 'push' (it calls us)")
        item = {"key": k, "kind": kind, "name": str(e.get("name") or k).strip()[:60],
                "does": str(e.get("does") or "").strip()[:280],
                "input_hint": str(e.get("input_hint") or "").strip()[:200]}
        if kind == "webhook":
            url = str(e.get("url") or "").strip()
            if url not in known_urls and not _url_ok(url, allow_http_engines):
                raise ConfigError(f"engines.{k}: webhook url must be https")
            item["url"] = url
        out["engines"].append(item)
    engine_keys = set(BUILTIN_ENGINES) | eseen

    # stages
    stages = cfg.get("stages", base["stages"])
    if not isinstance(stages, list) or not 2 <= len(stages) <= 12:
        raise ConfigError("stages: need between 2 and 12 stages")
    sseen: set = set()
    out["stages"] = []
    for s in stages:
        k = _key(s.get("key"), "stages")
        if k in sseen:
            raise ConfigError(f"stages: duplicate key {k!r}")
        sseen.add(k)
        engs = s.get("engines")
        if engs is None:  # older single-engine shape
            engs = [s["engine"]] if s.get("engine") not in (None, "none") else []
        if not isinstance(engs, list) or len(engs) > 4:
            raise ConfigError(f"stages.{k}: engines must be a list of up to 4")
        engs = [e for e in dict.fromkeys(engs) if e != "none"]
        bad = [e for e in engs if e not in engine_keys]
        if bad:
            raise ConfigError(f"stages.{k}: unknown engine(s) {bad}")
        try:
            target = int(s["target_days"] if s.get("target_days") is not None else DEFAULT_TARGETS.get(k, 14))
        except (TypeError, ValueError):
            raise ConfigError(f"stages.{k}: target_days must be a whole number of days")
        if not 1 <= target <= 365:
            raise ConfigError(f"stages.{k}: target_days must be between 1 and 365")
        out["stages"].append({"key": k, "name": str(s.get("name") or k)[:40], "engines": engs,
                              "target_days": target,
                              "exit_criteria": str(s.get("exit_criteria", ""))[:280]})

    # fields
    fields = cfg.get("fields", base["fields"])
    out["fields"] = {}
    for scope in ("customer", "deployment"):
        norm, keys = [], set()
        for f in fields.get(scope, []):
            fk = _key(f.get("key"), f"fields.{scope}")
            if fk in keys:
                raise ConfigError(f"fields.{scope}: duplicate key {fk!r}")
            if f.get("type") not in FIELD_TYPES:
                raise ConfigError(f"fields.{scope}.{fk}: type must be one of {FIELD_TYPES}")
            keys.add(fk)
            item = {"key": fk, "label": str(f.get("label") or fk)[:60], "type": f["type"]}
            if f["type"] == "select":
                opts = f.get("options") or []
                if not opts:
                    raise ConfigError(f"fields.{scope}.{fk}: select needs options")
                item["options"] = [str(o)[:40] for o in opts]
            norm.append(item)
        out["fields"][scope] = norm
    return out


# When a release adds an action, existing workspaces inherit it from the action
# that best matches its intent, so nobody silently loses (or gains) access.
DERIVE = {
    "task.view_internal": "task.create",
    "finding.view_internal": "engine.run",
    "customer.share": "task.assign",
    "report.create": "task.create",
    "integrations.manage": "config.edit",
    "delay.confirm": "finding.confirm",
    "flag.handle": "deployment.staff",
    "approval.decide": "finding.confirm",
    "pipeline.view": "people.read",
    "pipeline.edit": "deployment.create",
    "sow.edit": "deployment.staff",
    "milestone.submit": "deployment.staff",
    "billing.view": "deployment.staff",
}


def upgrade(cfg: dict) -> dict:
    """Bring a stored config up to the current schema without changing behavior."""
    cfg = copy.deepcopy(cfg)
    perms = cfg.setdefault("permissions", {})
    for action in ACTIONS:
        if action not in perms:
            src = DERIVE.get(action)
            perms[action] = dict(perms.get(src, {})) if src else {}
            if action == "milestone.accept":  # the customer-side roles: they see deployments but not internal work
                internal = perms.get("task.view_internal", {})
                perms[action] = {r: "own" for r, s in perms.get("deployment.view", {}).items()
                                 if s and not internal.get(r)}
    cfg.setdefault("sso", default()["sso"])
    base_int = default()["integrations"]
    ints = cfg.setdefault("integrations", {})
    for k, v in base_int.items():
        if k not in ints:
            ints[k] = copy.deepcopy(v)
        elif isinstance(v, dict):
            ints[k] = {**v, **ints[k]}
    for st in cfg.get("stages", []):
        if "engines" not in st:
            e = st.pop("engine", "none")
            st["engines"] = [] if e in (None, "none") else [e]
        st.setdefault("target_days", DEFAULT_TARGETS.get(st.get("key"), 14))
    return cfg


def stage_keys(cfg: dict) -> list[str]:
    return [s["key"] for s in cfg["stages"]]


def role_name(cfg: dict, key: str) -> str:
    return next((r["name"] for r in cfg["roles"] if r["key"] == key), key)


def check_fields(cfg: dict, scope: str, values: dict) -> dict:
    """Validate custom-field values against the tenant's field definitions."""
    defs = {f["key"]: f for f in cfg["fields"].get(scope, [])}
    out = {}
    for k, v in (values or {}).items():
        if k not in defs:
            raise ConfigError(f"unknown {scope} field {k!r}")
        d = defs[k]
        if v is None or v == "":
            continue
        if d["type"] == "number":
            try:
                v = float(v)
            except (TypeError, ValueError):
                raise ConfigError(f"{k}: expected a number")
        elif d["type"] == "bool":
            v = bool(v)
        elif d["type"] == "select" and v not in d["options"]:
            raise ConfigError(f"{k}: must be one of {d['options']}")
        elif d["type"] == "date" and not re.match(r"^\d{4}-\d{2}-\d{2}$", str(v)):
            raise ConfigError(f"{k}: expected YYYY-MM-DD")
        out[k] = v if d["type"] in ("number", "bool") else str(v)[:200]
    return out
