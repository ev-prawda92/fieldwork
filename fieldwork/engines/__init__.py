"""Engine adapters: the existing projects, behind one interface.

Each engine is Evan's original code vendored unchanged into *_core/ (see
ENGINES.md for source commits). The adapters here translate Fieldwork's
objects into each engine's inputs and its outputs back into findings. Nothing
in *_core knows Fieldwork exists, so each engine can keep evolving in its own
repo and be re-vendored.

Stage engines that are not wired yet are declared with status "planned" so the
console can say so plainly instead of showing canned data.
"""

from __future__ import annotations

import csv
import io

from .sendero_core import classify as sendero
from .threshold_core.parse import parse_req
from .threshold_core.profile import Evidence, Profile
from .threshold_core.score import Status, Verdict, score

REGISTRY = {
    "sendero":     {"name": "Sendero",      "status": "live",
                    "does": "Classifies a friction point as BUILD vs TRAINING from per-user performance data"},
    "threshold":   {"name": "Threshold",    "status": "live",
                    "does": "Scores bench people against a deployment's staffing needs, gate by gate, with citations"},
    "cortex":      {"name": "Cortex",       "status": "planned",
                    "does": "Agent permissions, scoped credentials and human approval gates for integrations"},
    "census":      {"name": "Interface Census + advisory engine", "status": "planned",
                    "does": "Systems inventory, evidence gaps, findings memo"},
    "conformance": {"name": "Conformance harness", "status": "planned",
                    "does": "Runs the agent conformance suite against customer configs"},
    "golive":      {"name": "Sendero Go-Live Command Center", "status": "planned",
                    "does": "Cutover checklist, issue triage, command-center stand-down"},
    "attribution": {"name": "Coyote attribution", "status": "planned",
                    "does": "Attributes delay and value to causes, confirmed in a ledger"},
    "none":        {"name": "No engine", "status": "n/a", "does": ""},
}


class EngineError(ValueError):
    pass


# ---------------------------------------------------------------- Sendero ---

def parse_csv(text: str) -> list[dict]:
    reader = csv.DictReader(io.StringIO(text.strip()))
    rows = [dict(r) for r in reader]
    if not rows:
        raise EngineError("CSV has no data rows")
    return rows


def run_sendero(rows: list[dict], metric: str, baseline: float | None = None,
                higher_is_worse: bool = True, capability: list[str] | None = None,
                config: list[str] | None = None) -> dict:
    if not rows:
        raise EngineError("no rows")
    header = list(rows[0].keys())
    if metric not in header:
        raise EngineError(f"metric column {metric!r} not in data (columns: {', '.join(header)})")
    if capability is None and config is None:
        capability, config = sendero._auto_cohorts(header, metric)
    for col in (capability or []) + (config or []):
        if col not in header:
            raise EngineError(f"cohort column {col!r} not in data")
    result = sendero.classify(rows, metric, baseline=baseline, higher_is_worse=higher_is_worse,
                              capability_cols=capability or [], config_cols=config or [])
    if "error" in result:
        raise EngineError(result["error"])
    result.pop("emoji", None)
    result["cohorts"] = {"capability": capability or [], "config": config or []}
    return result


# -------------------------------------------------------------- Threshold ---

_VERDICT_RANK = {Verdict.APPLY: 0, Verdict.ARGUABLE: 1, Verdict.OPEN_QUESTION: 2, Verdict.BLOCKED: 3}


def profile_from_dict(name: str, data: dict) -> Profile:
    prof = Profile(name=name, location=data.get("location", ""),
                   current_scope=data.get("current_scope", ""),
                   absent=tuple(data.get("absent", []) or ()))
    for item in data.get("evidence", []) or []:
        prof.evidence.append(Evidence(
            id=item["id"], kind=item.get("kind", "other"), title=item.get("title", ""),
            org=item.get("org", ""), claims=tuple(item.get("claims", []) or ()),
            tags=tuple(item.get("tags", []) or ()), source=str(item.get("source", "")),
            start=item.get("start"), end=item.get("end"),
            credential=item.get("credential", ""), year=item.get("year"),
        ))
    return prof


def run_threshold(staffing_text: str, title: str, people: list[dict]) -> dict:
    """people: [{id, name, profile: {...}}]. Returns ranked scorecards."""
    if not staffing_text.strip():
        raise EngineError("this deployment has no staffing requirements written yet")
    req = parse_req(staffing_text, title=title)
    if not req.requirements:
        raise EngineError("no requirements found — put bullets under a 'Required' "
                          "or 'Preferred' heading")
    cards = []
    for p in people:
        prof = profile_from_dict(p["name"], p.get("profile") or {})
        card = score(req, prof)
        cards.append({
            "user_id": p["id"], "name": p["name"], "verdict": card.verdict.value,
            "summary": card.summary,
            "met": len(card.of(Status.MET)), "total": len(card.findings),
            "findings": [{
                "requirement": f.requirement.text, "section": f.requirement.section,
                "gate": f.requirement.is_gate, "status": f.status.value,
                "detail": f.detail, "citations": list(f.citations),
            } for f in card.findings],
            "_rank": (_VERDICT_RANK[card.verdict], -len(card.of(Status.MET))),
        })
    cards.sort(key=lambda c: c["_rank"])
    for c in cards:
        c.pop("_rank")
    return {
        "requirements": [{"text": r.text, "section": r.section, "label": r.label.value,
                          "confidence": r.confidence} for r in req.requirements],
        "ranked": cards,
    }
