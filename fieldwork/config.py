"""Per-tenant configuration: what makes Fieldwork customizable SaaS.

Each customer (an FDE org) owns its workspace config:

- stages       the deployment chain, in order, with the engine that powers each
- fields       custom fields on customers and deployments
- permissions  which roles may take which actions
- labels       what the org calls its roles ("Deployment Strategist", ...)

Directors edit it through PUT /api/config. validate() is strict so a bad edit
fails loudly instead of corrupting the workspace, and a few invariants are
locked so a director cannot lock the org out of its own settings.
"""

from __future__ import annotations

import copy
import re

ROLES = ("fde", "manager", "director")
ENGINES = ("none", "census", "cortex", "conformance", "golive", "sendero", "attribution", "threshold")
FIELD_TYPES = ("text", "number", "date", "select", "bool")

# action -> roles allowed. FDE-scoped actions are further limited to
# deployments the FDE is a member of (enforced in the API layer).
ACTIONS = {
    "deployment.read_all": "See every deployment in the workspace, not only assigned ones",
    "deployment.create":   "Open a new customer deployment",
    "deployment.advance":  "Move a deployment to another stage",
    "deployment.staff":    "Add or remove people on a deployment",
    "task.create":         "Create tasks",
    "task.assign":         "Assign tasks to other people",
    "engine.run":          "Run an engine (Adopt, Bench, ...) on a deployment",
    "finding.confirm":     "Confirm an engine finding into the record",
    "people.read":         "See the team roster and workload",
    "bench.match":         "Match bench people to a deployment's staffing needs",
    "audit.read":          "Read the audit trail",
    "audit.verify":        "Verify the audit hash chain",
    "config.edit":         "Edit workspace configuration",
}

LOCKED = {"config.edit": ["director"]}

DEFAULT_CONFIG: dict = {
    "labels": {"fde": "Forward Deployed Engineer", "manager": "FDE Manager",
               "director": "Director of Deployments"},
    "stages": [
        {"key": "discover",  "name": "Discover",  "engine": "census",
         "exit_criteria": "Systems inventory and evidence gaps written up and read out"},
        {"key": "integrate", "name": "Integrate", "engine": "cortex",
         "exit_criteria": "Agents connected with scoped permissions and human gates"},
        {"key": "test",      "name": "Test",      "engine": "conformance",
         "exit_criteria": "Conformance suite green on customer data"},
        {"key": "golive",    "name": "Go-live",   "engine": "golive",
         "exit_criteria": "Cutover complete, command center stood down"},
        {"key": "adopt",     "name": "Adopt",     "engine": "sendero",
         "exit_criteria": "Friction points classified build vs training and routed"},
        {"key": "value",     "name": "Value",     "engine": "attribution",
         "exit_criteria": "Outcome measured and confirmed with the customer"},
    ],
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
    "permissions": {
        "deployment.read_all": ["manager", "director"],
        "deployment.create":   ["manager", "director"],
        "deployment.advance":  ["fde", "manager", "director"],
        "deployment.staff":    ["manager", "director"],
        "task.create":         ["fde", "manager", "director"],
        "task.assign":         ["manager", "director"],
        "engine.run":          ["fde", "manager", "director"],
        "finding.confirm":     ["manager", "director"],
        "people.read":         ["manager", "director"],
        "bench.match":         ["manager", "director"],
        "audit.read":          ["manager", "director"],
        "audit.verify":        ["director"],
        "config.edit":         ["director"],
    },
}

_KEY = re.compile(r"^[a-z][a-z0-9_]{0,31}$")


class ConfigError(ValueError):
    pass


def default() -> dict:
    return copy.deepcopy(DEFAULT_CONFIG)


def validate(cfg: dict) -> dict:
    """Return a normalized copy or raise ConfigError with a specific message."""
    if not isinstance(cfg, dict):
        raise ConfigError("config must be an object")
    out = default()

    labels = cfg.get("labels", out["labels"])
    if set(labels) - set(ROLES):
        raise ConfigError(f"labels: unknown role(s) {sorted(set(labels) - set(ROLES))}")
    out["labels"].update({k: str(v)[:60] for k, v in labels.items()})

    stages = cfg.get("stages", out["stages"])
    if not isinstance(stages, list) or not 2 <= len(stages) <= 12:
        raise ConfigError("stages: need between 2 and 12 stages")
    seen = set()
    norm_stages = []
    for s in stages:
        key = s.get("key", "")
        if not _KEY.match(key):
            raise ConfigError(f"stages: bad key {key!r} (lowercase, digits, underscore)")
        if key in seen:
            raise ConfigError(f"stages: duplicate key {key!r}")
        seen.add(key)
        engine = s.get("engine", "none")
        if engine not in ENGINES:
            raise ConfigError(f"stages: unknown engine {engine!r} on {key!r}")
        norm_stages.append({"key": key, "name": str(s.get("name") or key)[:40],
                            "engine": engine,
                            "exit_criteria": str(s.get("exit_criteria", ""))[:280]})
    out["stages"] = norm_stages

    fields = cfg.get("fields", out["fields"])
    for scope in ("customer", "deployment"):
        norm = []
        keys = set()
        for f in fields.get(scope, []):
            if not _KEY.match(f.get("key", "")) or f["key"] in keys:
                raise ConfigError(f"fields.{scope}: bad or duplicate key {f.get('key')!r}")
            if f.get("type") not in FIELD_TYPES:
                raise ConfigError(f"fields.{scope}.{f['key']}: type must be one of {FIELD_TYPES}")
            keys.add(f["key"])
            item = {"key": f["key"], "label": str(f.get("label") or f["key"])[:60], "type": f["type"]}
            if f["type"] == "select":
                opts = f.get("options") or []
                if not opts:
                    raise ConfigError(f"fields.{scope}.{f['key']}: select needs options")
                item["options"] = [str(o)[:40] for o in opts]
            norm.append(item)
        out["fields"][scope] = norm

    perms = cfg.get("permissions", out["permissions"])
    unknown = set(perms) - set(ACTIONS)
    if unknown:
        raise ConfigError(f"permissions: unknown action(s) {sorted(unknown)}")
    for action, roles in perms.items():
        if not isinstance(roles, list) or set(roles) - set(ROLES):
            raise ConfigError(f"permissions.{action}: roles must be a subset of {ROLES}")
        out["permissions"][action] = sorted(set(roles), key=ROLES.index)
    for action, roles in LOCKED.items():
        if out["permissions"][action] != roles:
            raise ConfigError(f"permissions.{action} is locked to {roles} so the workspace can't be locked out")
    return out


def stage_keys(cfg: dict) -> list[str]:
    return [s["key"] for s in cfg["stages"]]


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
