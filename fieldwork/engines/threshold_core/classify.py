"""Requirement classifier: gate, soft_required, or preference.

This is the only interesting problem in the project. Everything downstream --
the ranking, the verdict, the advice -- is a consequence of getting these three
apart, because only a missed `gate` should ever end an application.

The baseline here is deliberately dumb and deliberately transparent: regexes
over verbatim requirement text plus the section it appeared under. It exists so
that any future classifier -- an LLM, a fine-tune, a human -- has a number to
beat, and so that the failures are legible rather than mysterious.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum


class Label(str, Enum):
    GATE = "gate"                    # checkable; missing it ends the application
    SOFT_REQUIRED = "soft_required"  # printed as required; not screenable
    PREFERENCE = "preference"        # desirable, or hedged away in the text


# --- signals ---------------------------------------------------------------

# Hedges that demote a bullet even when it sits under "Required".
HEDGE = re.compile(
    r"\b(is a plus|are a plus|a plus\b|nice to have|preferred\.?$|preferred\b"
    r"|ideally|beneficial|bonus|desirable|would be great|not required)\b",
    re.I,
)

# Quantified experience: the most reliable gate signal there is.
YEARS = re.compile(r"\b\d+\s*[-–]?\s*\d*\s*\+?\s*(?:year|yr)s?\b", re.I)

CREDENTIAL = re.compile(
    r"\b(bachelor'?s?|master'?s?|ph\.?d|doctorate|degree|certifica(?:te|tion)"
    r"|green belt|licen[cs]e|clearance)\b",
    re.I,
)

# Named, checkable technologies and methodologies.
NAMED_TECH = re.compile(
    r"\b(python|sql|c\+\+|golang|\bgo\b|java|scala|rust|terraform|kubernetes"
    r"|pytorch|tensorflow|cuda|spark|airflow|snowflake|dbt|aws|gcp|azure"
    r"|netsuite|jira|confluence|salesforce|epic\b|figma"
    r"|agile|waterfall|scrum|kanban|iso ?26262|aspice|hl7|fhir|hipaa|soc ?2"
    r"|unece|six sigma)\b",
    re.I,
)

# Logistics: location, travel, schedule, authorization. Always screened.
LOGISTICS = re.compile(
    r"\b(travel|relocat\w*|based at|based in|on-?site|onsite|in (?:one of )?our offices"
    r"|days per week|work authorization|visa|sponsor\w*|shift|on-?call)\b",
    re.I,
)

# A named domain of experience is a gate: "within transportation", "in
# autonomous systems". Distinct from a domain listed as merely desirable.
DOMAIN_EXPERIENCE = re.compile(
    r"\bexperience\b[^.]{0,80}?\b(?:in|with|within|working (?:in|with|on))\b", re.I
)

# Character and competence claims. Everyone asserts these; nobody verifies them.
SOFT = re.compile(
    r"\b(excellent|superb|strong|solid|outstanding|exceptional|effective|proven ability"
    r"|demonstrated ability|passionate|comfortable|self-?starter|team player"
    r"|interpersonal|collaborat\w*|communicat\w*|leadership|mentorship|aptitude"
    r"|judgment|judgement|analytical skills|problem[- ]solving|organizational abilities"
    r"|attention to detail|thrive|capability to|ability to work)\b",
    re.I,
)

# Concrete verbs that survive the SOFT test: they point at an artifact or a
# system rather than a disposition.
CONCRETE = re.compile(
    r"\b(portfolio|publications?|conference presentations?|patent|open source"
    r"|proficien\w*|expert knowledge|deep command|working knowledge|hands-?on)\b",
    re.I,
)


@dataclass
class Prediction:
    label: Label
    confidence: float
    fired: list[str] = field(default_factory=list)

    def __str__(self) -> str:  # pragma: no cover - display only
        return f"{self.label.value} ({self.confidence:.2f}) [{', '.join(self.fired)}]"


class HeuristicClassifier:
    """Transparent baseline. Every prediction reports which signals fired."""

    name = "heuristic-v1"

    def predict(self, text: str, section: str) -> Prediction:
        fired: list[str] = []

        if section == "desirable":
            return Prediction(Label.PREFERENCE, 0.97, ["section=desirable"])

        # A hedge demotes even a required bullet. Checked before everything
        # else, because "...or equivalent experience preferred" outranks the
        # word "degree" sitting in the same sentence.
        if HEDGE.search(text):
            fired.append("hedge")
            # ...unless the hedge only qualifies a trailing extra, e.g.
            # "Proficiency in Python...; familiarity with R is a plus".
            head = re.split(r"[;.]", text)[0]
            if YEARS.search(head) or NAMED_TECH.search(head) or CREDENTIAL.search(head):
                fired.append("hedge-is-trailing")
            else:
                return Prediction(Label.PREFERENCE, 0.80, fired)

        hard = []
        if YEARS.search(text):
            hard.append("years")
        if CREDENTIAL.search(text):
            hard.append("credential")
        if NAMED_TECH.search(text):
            hard.append("named-tech")
        if LOGISTICS.search(text):
            hard.append("logistics")
        if CONCRETE.search(text):
            hard.append("concrete-artifact")
        if DOMAIN_EXPERIENCE.search(text) and not SOFT.match(text.strip()):
            hard.append("domain-experience")

        soft_hit = bool(SOFT.search(text))

        if hard:
            fired += hard
            # A soft opener with a hard object still gates: "Strong NetSuite
            # proficiency" is checkable, "Strong analytical skills" is not.
            conf = 0.90 if len(hard) > 1 else 0.75
            if soft_hit and len(hard) == 1 and hard[0] == "domain-experience":
                fired.append("soft-dominates")
                return Prediction(Label.SOFT_REQUIRED, 0.60, fired)
            return Prediction(Label.GATE, conf, fired)

        if soft_hit:
            fired.append("soft-language")
            return Prediction(Label.SOFT_REQUIRED, 0.80, fired)

        fired.append("no-signal")
        return Prediction(Label.SOFT_REQUIRED, 0.40, fired)


def classify(text: str, section: str = "required") -> Prediction:
    return HeuristicClassifier().predict(text, section)
