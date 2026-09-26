"""Built-in stage engines: Discover, Integrate, Test, Go-live, Value.

Each takes the text a person pastes or uploads (CSV or JSON) and returns the
same shape every engine returns:

    {"summary": str, "status": "pass|warn|fail|info", "result": {...}}

All deterministic: no model calls, every conclusion traceable to input rows.
Integrate runs Cortex's authorization engine (vendored unchanged in
cortex_core/) against the agent's authority profile.
"""

from __future__ import annotations

import csv
import io
import json
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from .cortex_core import authorization as cortex


class StageEngineError(ValueError):
    pass


def _csv(text: str, required: set[str]) -> list[dict]:
    lines = [l for l in text.strip().splitlines() if l.strip() and not l.lstrip().startswith("#")]
    if not lines:
        raise StageEngineError("no data rows")
    rows = [{(k or "").strip().lower(): (v or "").strip() for k, v in r.items()}
            for r in csv.DictReader(io.StringIO("\n".join(lines)))]
    missing = required - set(rows[0].keys()) if rows else required
    if missing:
        raise StageEngineError(f"missing column(s): {', '.join(sorted(missing))}")
    return rows


def _json(text: str) -> dict:
    try:
        data = json.loads(text)
    except ValueError as e:
        raise StageEngineError(f"input isn't valid JSON: {e}")
    if not isinstance(data, dict):
        raise StageEngineError("input must be a JSON object")
    return data


def _directive(text: str, name: str, default: float) -> float:
    m = re.search(rf"^\s*#\s*{name}\s*[:=]\s*([0-9.]+)", text, re.M | re.I)
    return float(m.group(1)) if m else default


def _yes(v: str) -> bool:
    return str(v).strip().lower() in ("yes", "y", "true", "1", "x")


# ------------------------------------------------------------------ Discover

SENSITIVE = {"confidential", "regulated", "phi", "pii", "pci"}


def census(text: str) -> dict:
    """Systems inventory -> evidence gaps and a findings memo.

    CSV: system, owner, data_class, interface, access, documented, controls
    """
    rows = _csv(text, {"system", "owner", "data_class", "interface", "access", "documented"})
    systems, blockers = [], 0
    for r in rows:
        gaps, severity = [], "ok"
        if not r["owner"]:
            gaps.append("no named owner"); severity = "gap"
        access = r["access"].lower()
        if access in ("none", "denied", ""):
            gaps.append("no access granted or requested"); severity = "blocker"
        elif access != "granted":
            gaps.append(f"access {access}, not granted"); severity = max(severity, "gap", key=_sev)
        if not _yes(r["documented"]):
            gaps.append("interface undocumented"); severity = max(severity, "gap", key=_sev)
        iface = r["interface"].lower()
        if iface in ("none", "ui", ""):
            gaps.append("no integration path (UI only)"); severity = max(severity, "gap", key=_sev)
        controls = r.get("controls", "").lower()
        if r["data_class"].lower() in SENSITIVE:
            missing = [c for c in ("audit", "sso") if c not in controls]
            if missing:
                gaps.append(f"{r['data_class'].lower()} data without {' or '.join(missing)} controls")
                severity = "blocker"
        blockers += severity == "blocker"
        systems.append({"system": r["system"], "severity": severity, "gaps": gaps})
    clean = sum(s["severity"] == "ok" for s in systems)
    pct = round(100 * clean / len(systems))
    status = "fail" if blockers else ("warn" if clean < len(systems) else "pass")
    memo = ["## Discovery findings", "",
            f"{len(systems)} systems inventoried; {clean} ready ({pct}%), {blockers} with blockers.", ""]
    for sev, heading in (("blocker", "Blockers"), ("gap", "Gaps to close")):
        items = [s for s in systems if s["severity"] == sev]
        if items:
            memo += [f"### {heading}"] + [f"- **{s['system']}**: {'; '.join(s['gaps'])}" for s in items] + [""]
    return {"summary": f"{clean}/{len(systems)} systems ready · {blockers} blocker(s)",
            "status": status,
            "result": {"ready_pct": pct, "systems": systems, "memo": "\n".join(memo)}}


def _sev(s: str) -> int:
    return {"ok": 0, "gap": 1, "blocker": 2}[s]


# ----------------------------------------------------------------- Integrate

WRITE_VERBS = ("submit", "write", "update", "delete", "send", "pay", "approve", "create", "post", "transfer")


def authority(text: str, now: datetime | None = None) -> dict:
    """Agent authority profile + expected-decision scenarios -> Cortex verdicts.

    JSON: {"profile": {...Cortex authority profile...},
           "scenarios": [{"name", "request": {...}, "expect": "HUMAN_REVIEW"}]}
    """
    now = now or datetime.now(timezone.utc)
    data = _json(text)
    profile = data.get("profile")
    if not isinstance(profile, dict):
        raise StageEngineError("input needs a 'profile' object (a Cortex authority profile)")
    errors = cortex.validate_profile(profile)
    lint = []
    if profile.get("default_decision", "BLOCK") != "BLOCK":
        lint.append(f"default decision is {profile.get('default_decision')}, not BLOCK: "
                    "unlisted actions won't be blocked")
    for p in profile.get("privileges", []):
        a = p.get("action", "")
        if p.get("effect", "allow") != "allow":
            continue
        if any(v in a.lower() for v in WRITE_VERBS) and not p.get("requires_human_review"):
            lint.append(f"{a}: write action with no human review gate")
        if p.get("max_actions_per_hour") is None:
            lint.append(f"{a}: no rate limit")
        if not p.get("environments"):
            lint.append(f"{a}: not scoped to any environment")
    for cred in profile.get("credentials", []):
        exp = cortex._parse_time(cred.get("expires_at"))
        if exp and exp <= now:
            errors.append(f"credential {cred.get('name')} has expired")
        elif exp and exp <= now + timedelta(days=30):
            lint.append(f"credential {cred.get('name')} expires within 30 days")

    results, mismatches = [], 0
    for s in data.get("scenarios", []):
        got = cortex.evaluate(profile, s.get("request", {}), now)
        expect = s.get("expect")
        ok = expect is None or got["decision"] == expect
        mismatches += not ok
        results.append({"scenario": s.get("name", "unnamed"), "expected": expect,
                        "decision": got["decision"], "ok": ok, "reasons": got["reasons"],
                        "obligations": got["obligations"]})
    status = "fail" if errors or mismatches else ("warn" if lint else "pass")
    passed = len(results) - mismatches
    return {"summary": (f"{passed}/{len(results)} scenarios behave as expected"
                        + (f" · {len(errors)} profile error(s)" if errors else "")
                        + (f" · {len(lint)} risk(s)" if lint else "")),
            "status": status,
            "result": {"scenarios": results, "profile_errors": errors, "risks": lint}}


# ---------------------------------------------------------------------- Test

def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.strip().lower())


def _num(s: str):
    try:
        return float(s.replace(",", "").replace("$", ""))
    except ValueError:
        return None


def conformance(text: str) -> dict:
    """Test cases -> pass rate by category, failing on any critical miss.

    CSV: case_id, category, expected, actual, critical   (# threshold: 0.95)
    Numbers match within 1%; text matches ignoring case and spacing.
    """
    threshold = _directive(text, "threshold", 0.95)
    rows = _csv(text, {"case_id", "category", "expected", "actual"})
    by_cat: dict = defaultdict(lambda: [0, 0])
    failures, critical_fail = [], []
    for r in rows:
        e, a = r["expected"], r["actual"]
        en, an = _num(e), _num(a)
        ok = abs(en - an) <= 0.01 * max(abs(en), 1e-9) if en is not None and an is not None else _norm(e) == _norm(a)
        by_cat[r["category"]][0] += ok
        by_cat[r["category"]][1] += 1
        if not ok:
            f = {"case_id": r["case_id"], "category": r["category"], "expected": e, "actual": a,
                 "critical": _yes(r.get("critical", ""))}
            failures.append(f)
            if f["critical"]:
                critical_fail.append(r["case_id"])
    cats = {k: {"passed": v[0], "total": v[1], "rate": round(v[0] / v[1], 3)} for k, v in by_cat.items()}
    passed = sum(v[0] for v in by_cat.values())
    rate = passed / len(rows)
    weak = [k for k, v in cats.items() if v["rate"] < threshold]
    status = "fail" if critical_fail or rate < threshold else ("warn" if weak else "pass")
    return {"summary": (f"{passed}/{len(rows)} cases pass ({rate:.0%}) against a {threshold:.0%} bar"
                        + (f" · {len(critical_fail)} critical failure(s)" if critical_fail else "")),
            "status": status,
            "result": {"pass_rate": round(rate, 3), "threshold": threshold, "by_category": cats,
                       "below_threshold": weak, "critical_failures": critical_fail, "failures": failures}}


# ------------------------------------------------------------------- Go-live

def _sevnum(s: str) -> int:
    m = re.search(r"\d", s or "")
    return int(m.group(0)) if m else 4


def _ts(s: str):
    if not s:
        return None
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        raise StageEngineError(f"bad timestamp {s!r} (use ISO 8601, e.g. 2026-10-02T14:30)")


def command_center(text: str) -> dict:
    """Go-live issue log -> can the command center stand down?

    CSV: id, severity, status, opened_at, resolved_at, area   (# max_open_sev2: 2)
    Stand down when: no open sev1, open sev2 within limit, and new issues in the
    last 24h at or below the 24h before (the curve is bending down).
    """
    max_sev2 = int(_directive(text, "max_open_sev2", 2))
    rows = _csv(text, {"id", "severity", "status", "opened_at"})
    issues = []
    for r in rows:
        issues.append({"id": r["id"], "sev": _sevnum(r["severity"]), "open": r["status"].lower() != "resolved",
                       "opened": _ts(r["opened_at"]), "resolved": _ts(r.get("resolved_at", "")),
                       "area": r.get("area", "")})
    latest = max(max(i["opened"] for i in issues),
                 max((i["resolved"] for i in issues if i["resolved"]), default=datetime.min.replace(tzinfo=timezone.utc)))
    last24 = sum(latest - timedelta(hours=24) < i["opened"] <= latest for i in issues)
    prev24 = sum(latest - timedelta(hours=48) < i["opened"] <= latest - timedelta(hours=24) for i in issues)
    open_by = {s: sum(i["open"] and i["sev"] == s for i in issues) for s in (1, 2, 3, 4)}
    mttr = {}
    for s in (1, 2):
        done = [(i["resolved"] - i["opened"]).total_seconds() / 3600 for i in issues
                if i["sev"] == s and i["resolved"]]
        mttr[f"sev{s}_hours"] = round(sum(done) / len(done), 1) if done else None
    areas: dict = defaultdict(int)
    for i in issues:
        if i["open"]:
            areas[i["area"] or "unassigned"] += 1
    checks = [
        {"check": "No open sev1", "ok": open_by[1] == 0, "detail": f"{open_by[1]} open"},
        {"check": f"Open sev2 at most {max_sev2}", "ok": open_by[2] <= max_sev2, "detail": f"{open_by[2]} open"},
        {"check": "New issues trending down", "ok": last24 <= prev24,
         "detail": f"{last24} in last 24h vs {prev24} the day before"},
    ]
    failed = [c for c in checks if not c["ok"]]
    status = "pass" if not failed else ("fail" if open_by[1] else "warn")
    verdict = "Ready to stand down" if not failed else ("Hold: sev1 open" if open_by[1] else "Not yet")
    return {"summary": f"{verdict} · {sum(open_by.values())} open ({open_by[1]} sev1, {open_by[2]} sev2)",
            "status": status,
            "result": {"checks": checks, "open_by_severity": {f"sev{k}": v for k, v in open_by.items()},
                       "mean_time_to_resolve": mttr, "open_by_area": dict(areas),
                       "new_last_24h": last24, "new_prior_24h": prev24}}


# --------------------------------------------------------------------- Value

CAUSES = ("customer", "vendor", "third_party")


def attribution(text: str) -> dict:
    """Value delivered vs baseline, and who the delays belong to.

    JSON: {"metric", "baseline", "current", "lower_is_better", "volume_per_month",
           "unit_minutes"?, "cost_per_hour", "planned_days", "actual_days",
           "delays": [{"event", "days", "cause": customer|vendor|third_party}]}
    If the metric is minutes per unit, hours saved = (baseline - current) x volume / 60.
    """
    d = _json(text)
    for k in ("metric", "baseline", "current"):
        if k not in d:
            raise StageEngineError(f"missing '{k}'")
    base, cur = float(d["baseline"]), float(d["current"])
    lower = bool(d.get("lower_is_better", True))
    delta = (base - cur) if lower else (cur - base)
    improvement = delta / base if base else 0.0
    value: dict = {"metric": d["metric"], "baseline": base, "current": cur,
                   "improvement_pct": round(100 * improvement, 1)}
    if "volume_per_month" in d and lower:
        hours = delta * float(d["volume_per_month"]) / 60
        value["hours_saved_per_month"] = round(hours, 1)
        if "cost_per_hour" in d:
            value["value_per_month"] = round(hours * float(d["cost_per_hour"]))
            value["value_per_year"] = value["value_per_month"] * 12

    delays = d.get("delays", [])
    by_cause = {c: 0.0 for c in CAUSES}
    for e in delays:
        cause = e.get("cause")
        if cause not in CAUSES:
            raise StageEngineError(f"delay {e.get('event')!r}: cause must be one of {CAUSES}")
        by_cause[cause] += float(e.get("days", 0))
    slip = None
    unattributed = 0.0
    if "planned_days" in d and "actual_days" in d:
        slip = float(d["actual_days"]) - float(d["planned_days"])
        unattributed = max(0.0, slip - sum(by_cause.values()))
    total = sum(by_cause.values()) + unattributed
    share = {c: round(v / total, 3) for c, v in by_cause.items()} if total else {}

    status = "fail" if improvement <= 0 else ("warn" if unattributed > 0 else "pass")
    bits = [f"{d['metric']}: {abs(value['improvement_pct'])}% {'better' if improvement > 0 else 'worse'}"]
    if "value_per_year" in value:
        bits.append(f"${value['value_per_year']:,}/yr")
    if slip:
        bits.append(f"{slip:g} days late, {unattributed:g} unattributed")
    return {"summary": " · ".join(bits), "status": status,
            "result": {"value": value, "slip_days": slip, "delay_days_by_cause": by_cause,
                       "delay_share": share, "unattributed_days": unattributed, "delays": delays}}


STAGE_ENGINES = {
    "census": census,
    "cortex": authority,
    "conformance": conformance,
    "golive": command_center,
    "attribution": attribution,
}
