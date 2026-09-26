"""Cortex consequence-aware authorization engine.

This module is intentionally framework-free.  The API layer persists profiles
and decisions, while this engine deterministically compiles enterprise-delegated
authority into ALLOW, ALLOW_WITH_LIMITS, REQUEST_MORE_EVIDENCE, HUMAN_REVIEW,
or BLOCK.
"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any


class Decision(str, Enum):
    ALLOW = "ALLOW"
    ALLOW_WITH_LIMITS = "ALLOW_WITH_LIMITS"
    REQUEST_MORE_EVIDENCE = "REQUEST_MORE_EVIDENCE"
    HUMAN_REVIEW = "HUMAN_REVIEW"
    BLOCK = "BLOCK"


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def validate_profile(profile: dict[str, Any]) -> list[str]:
    """Return validation errors without mutating a proposed profile."""
    errors: list[str] = []
    if profile.get("default_decision", "BLOCK") not in Decision.__members__:
        errors.append("default_decision must be a Cortex decision")
    if not isinstance(profile.get("credentials", []), list):
        errors.append("credentials must be a list")
    privileges = profile.get("privileges", [])
    if not isinstance(privileges, list):
        return errors + ["privileges must be a list"]
    seen: set[str] = set()
    for index, privilege in enumerate(privileges):
        action = privilege.get("action", "")
        if not action:
            errors.append(f"privileges[{index}].action is required")
        elif action in seen:
            errors.append(f"duplicate privilege action: {action}")
        seen.add(action)
        if privilege.get("effect", "allow") not in ("allow", "deny"):
            errors.append(f"privileges[{index}].effect must be allow or deny")
        if int(privilege.get("min_evidence_items", 0)) < 0:
            errors.append(f"privileges[{index}].min_evidence_items cannot be negative")
    return errors


def evaluate(profile: dict[str, Any], request: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    """Evaluate one proposed action against an active authority profile."""
    now = now or datetime.now(timezone.utc)
    action = request.get("action", "")
    reasons: list[str] = []
    obligations: list[str] = []

    if profile.get("status", "draft") != "active":
        return _result(Decision.BLOCK, ["Authority profile is not active"], [])

    privilege = next((p for p in profile.get("privileges", []) if p.get("action") == action), None)
    if privilege is None:
        default = profile.get("default_decision", "BLOCK")
        decision = Decision[default] if default in Decision.__members__ else Decision.BLOCK
        return _result(decision, ["No explicit privilege exists for this action"], [])
    if privilege.get("effect", "allow") == "deny":
        return _result(Decision.BLOCK, ["Action is explicitly prohibited"], [])

    allowed_environments = privilege.get("environments", [])
    if allowed_environments and request.get("environment") not in allowed_environments:
        reasons.append("Environment is outside the privilege scope")
    allowed_scopes = privilege.get("data_scopes", [])
    if allowed_scopes and request.get("data_scope") not in allowed_scopes:
        reasons.append("Data scope is outside the privilege scope")
    allowed_targets = privilege.get("target_systems", [])
    if allowed_targets and request.get("target_system") not in allowed_targets:
        reasons.append("Target system is outside the privilege scope")

    credentials = {c.get("name"): c for c in profile.get("credentials", [])}
    for name in privilege.get("required_credentials", []):
        credential = credentials.get(name)
        if not credential or credential.get("status") != "valid":
            reasons.append(f"Required credential is missing or invalid: {name}")
            continue
        expires_at = _parse_time(credential.get("expires_at"))
        if expires_at and expires_at <= now:
            reasons.append(f"Required credential has expired: {name}")

    maximum = privilege.get("max_financial_impact")
    impact = request.get("financial_impact")
    if maximum is not None and impact is not None and float(impact) > float(maximum):
        reasons.append("Financial impact exceeds the permitted limit")
    hourly_limit = privilege.get("max_actions_per_hour")
    if hourly_limit is not None and int(request.get("actions_last_hour", 0)) >= int(hourly_limit):
        reasons.append("Hourly action limit has been reached")
    if reasons:
        return _result(Decision.BLOCK, reasons, [])

    evidence = [item for item in request.get("evidence", []) if item.get("current", True)]
    required_types = set(privilege.get("required_evidence_types", []))
    present_types = {item.get("type") or item.get("evidence_type") for item in evidence}
    missing_types = sorted(required_types - present_types)
    minimum = int(privilege.get("min_evidence_items", 0))
    if len(evidence) < minimum or missing_types:
        if len(evidence) < minimum:
            reasons.append(f"Requires {minimum} current evidence item(s); received {len(evidence)}")
        if missing_types:
            reasons.append("Missing required evidence types: " + ", ".join(missing_types))
        obligations.append("Supply the required current evidence")
        return _result(Decision.REQUEST_MORE_EVIDENCE, reasons, obligations)

    approval = request.get("approval") or {}
    if privilege.get("requires_human_review", False) and approval.get("status") != "approved":
        reasons.append("Policy requires human approval for this action")
        obligations.append("Obtain approval from an authorized reviewer")
        return _result(Decision.HUMAN_REVIEW, reasons, obligations)

    constraints = privilege.get("constraints", [])
    if constraints:
        obligations.extend(str(item) for item in constraints)
        return _result(Decision.ALLOW_WITH_LIMITS,
                       ["All mandatory authorization checks passed"], obligations)
    return _result(Decision.ALLOW, ["All mandatory authorization checks passed"], [])


def _result(decision: Decision, reasons: list[str], obligations: list[str]) -> dict[str, Any]:
    return {"decision": decision.value, "reasons": reasons, "obligations": obligations}

