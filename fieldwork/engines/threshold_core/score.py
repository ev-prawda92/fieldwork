"""Score one requirement ledger against one evidence corpus.

Three rules, and they are the product:

1.  Cite or report absent. A requirement matched to nothing is not a partial
    score, it is a hole, and the report says so.
2.  Only a missed GATE blocks. Soft-required language and preferences never
    produce a rejection, because in the real world they never do.
3.  Arguable is a verdict. Some gates can be argued but not claimed; naming
    which one carries the cover letter is more useful than any number.

Deterministic: regex and set membership, no model. A model may later propose
matches, but it proposes into this structure and a human confirms.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum

from .classify import CREDENTIAL, NAMED_TECH, YEARS
from .parse import Req, Requirement
from .profile import Evidence, Profile


class Status(str, Enum):
    MET = "met"
    ARGUABLE = "arguable"      # defensible, not claimable -- needs an argument
    MISSING = "missing"        # the corpus explicitly does not contain it
    UNKNOWN = "unknown"        # nothing found either way; ask, do not guess


class Verdict(str, Enum):
    APPLY = "apply"
    ARGUABLE = "arguable"
    OPEN_QUESTION = "open_question"
    BLOCKED = "blocked"


# Domain phrases worth checking explicitly. Everything else falls back to
# named-technology matching, which is deliberately narrow -- a wide keyword net
# produces confident nonsense.
DOMAIN_TERMS = (
    "autonomous vehicle", "autonomous system", "self-driving", "robotics",
    "aerospace", "automotive", "embedded system", "hardware", "manufacturing",
    "supply chain", "logistics", "publication", "conference presentation",
    "risk modeling", "probabilistic", "statistical method", "experimental design",
    "machine learning", "deep learning", "computer vision", "payroll",
    "program management", "product management", "software development",
    "regulatory", "compliance", "safety", "clinical", "healthcare", "user research",
    "root cause", "process improvement", "quality assurance", "stage gate",
    "agile", "waterfall", "travel", "relocat",
    # Word-boundary regexes cannot see these; matched as plain substrings.
    "c++", "iso 26262", "aspice", "npi", "cuda", "pytorch", "tensorflow",
)

# Travel and place are different questions. Being local answers "can you be in
# the office"; it says nothing about willingness to fly to Europe six times a
# year, and conflating them is how a tool quietly clears a gate it cannot see.
_TRAVEL = re.compile(r"\b(travel|relocat\w*|visa|sponsor\w*|work authorization)\b", re.I)
_PLACE = re.compile(
    r"\b(based (?:at|in)|on-?site|onsite|in (?:one of )?our offices|days per week"
    r"|hybrid|in[- ]office)\b", re.I)
_DEGREE_FIELD = re.compile(
    r"degree in ([^.;()]+?)(?:,? or\b|\.|;|$)", re.I)


@dataclass
class Finding:
    requirement: Requirement
    status: Status
    citations: tuple[str, ...] = ()      # evidence ids
    detail: str = ""
    terms: tuple[str, ...] = ()

    @property
    def blocks(self) -> bool:
        return self.requirement.is_gate and self.status is Status.MISSING


@dataclass
class Scorecard:
    req: Req
    profile_name: str
    findings: list[Finding] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)

    def of(self, *statuses: Status) -> list[Finding]:
        return [f for f in self.findings if f.status in statuses]

    @property
    def gate_findings(self) -> list[Finding]:
        return [f for f in self.findings if f.requirement.is_gate]

    @property
    def verdict(self) -> Verdict:
        gates = self.gate_findings
        if any(f.status is Status.MISSING for f in gates):
            return Verdict.BLOCKED
        if any(f.status is Status.UNKNOWN for f in gates):
            return Verdict.OPEN_QUESTION
        if any(f.status is Status.ARGUABLE for f in gates):
            return Verdict.ARGUABLE
        return Verdict.APPLY

    @property
    def summary(self) -> str:
        g = self.gate_findings
        counts = {s: sum(1 for f in g if f.status is s) for s in Status}
        parts = [f"{len(g)} gates"]
        if counts[Status.MISSING]:
            parts.append(f"{counts[Status.MISSING]} missed")
        if counts[Status.ARGUABLE]:
            parts.append(f"{counts[Status.ARGUABLE]} to argue")
        if counts[Status.UNKNOWN]:
            parts.append(f"{counts[Status.UNKNOWN]} unconfirmed")
        if len(parts) == 1:
            parts.append("all clear")
        return " · ".join(parts)


# An absent entry is written the way a person thinks about the gap; a
# requirement is written the way a company does. These bridge the two.
ABSENT_ALIASES: dict[str, tuple[str, ...]] = {
    "cpp": ("c++",),
    "phd": ("ph.d", "phd", "doctorate"),
    "aws_production": ("aws",),
    "publications": ("publication",),
    "conference_presentations": ("conference presentation",),
    "autonomous_vehicles": ("autonomous vehicle", "autonomous system", "self-driving"),
    "hardware_development_npi": ("npi", "evt", "dvt", "pvt", "design for manufacture"),
    "iso_26262": ("iso 26262",),
    "automotive_regulatory": ("automotive regulatory", "homologation", "type approval"),
}


def _absent_phrases(profile: Profile) -> dict[str, str]:
    """Absent-list entries -> searchable phrases, keeping the original key.

    Matching is phrase-contained-in-term, never the reverse. Allowing a short
    term to match a longer absent entry turns "hardware" into a declaration
    that all hardware work is absent, which is how a corpus starts lying.
    """
    out: dict[str, str] = {}
    for a in profile.absent:
        out[a.replace("_", " ").strip().lower()] = a
        for alias in ABSENT_ALIASES.get(a, ()):
            out[alias] = a
    return out


def _terms_in(text: str) -> list[str]:
    low = text.lower()
    found = [t for t in DOMAIN_TERMS if t in low]
    found += [m.group(0).lower() for m in NAMED_TECH.finditer(text)]
    seen, ordered = set(), []
    for t in found:
        if t not in seen:
            seen.add(t); ordered.append(t)
    return ordered


def _evidence_for(profile: Profile, term: str) -> list[Evidence]:
    return [e for e in profile.evidence if term in e.haystack]


def score_requirement(req_item: Requirement, profile: Profile) -> Finding:
    text = req_item.text
    absent = _absent_phrases(profile)

    # --- quantified experience -------------------------------------------
    m = YEARS.search(text)
    if m:
        wanted = max(int(n) for n in re.findall(r"\d+", m.group(0)))
        have = profile.employment_months() / 12
        if have >= wanted:
            ids = tuple(e.id for e in profile.evidence
                        if e.kind == "employment" and e.start)
            return Finding(req_item, Status.MET, ids,
                           f"{have:.0f} years of merged employment against {wanted} required",
                           (f"{wanted}y",))
        return Finding(req_item, Status.MISSING, (),
                       f"{have:.0f} years found against {wanted} required", (f"{wanted}y",))

    # --- travel and authorization: never inferred -------------------------
    if _TRAVEL.search(text):
        return Finding(req_item, Status.UNKNOWN, (),
                       "a condition of the job, not a qualification, and nothing in a "
                       "corpus answers it — ask the candidate")

    # --- place: answerable from the posting's own location list -----------
    if _PLACE.search(text):
        city = profile.location.split(",")[0].lower() if profile.location else ""
        where = (text + " " + getattr(req_item, "_req_location", "")).lower()
        if city and city in where:
            return Finding(req_item, Status.MET, (),
                           f"{profile.location} is one of the posting's locations")
        return Finding(req_item, Status.UNKNOWN, (),
                       "location requirement — confirm with the candidate")

    # --- credentials ------------------------------------------------------
    if CREDENTIAL.search(text):
        degrees = [e for e in profile.evidence if e.kind == "education"]
        if not degrees:
            return Finding(req_item, Status.MISSING, (), "no education entries in the corpus")
        field_m = _DEGREE_FIELD.search(text)
        wanted_fields = re.split(r",| or ", field_m.group(1)) if field_m else []
        wanted_fields = [w.strip().lower() for w in wanted_fields if len(w.strip()) > 2]
        hits = [e for e in degrees
                if any(w in e.haystack for w in wanted_fields)] if wanted_fields else degrees
        if hits:
            return Finding(req_item, Status.MET, tuple(e.id for e in hits),
                           "; ".join(e.credential or e.label for e in hits[:2]))
        return Finding(req_item, Status.ARGUABLE, tuple(e.id for e in degrees[:2]),
                       "holds degrees, none in a field this requirement names — "
                       "an argument, not a claim")

    # --- everything else: term matching, with absence declared ------------
    terms = _terms_in(text)
    if not terms:
        return Finding(req_item, Status.UNKNOWN, (),
                       "no checkable term in this requirement", ())

    blocked = [t for t in terms if any(a == t or a in t for a in absent)]
    if blocked:
        return Finding(req_item, Status.MISSING, (),
                       "declared absent from the corpus: " + ", ".join(sorted(set(blocked))),
                       tuple(terms))

    cited: list[str] = []
    matched: list[str] = []
    for t in terms:
        found = _evidence_for(profile, t)
        if found:
            matched.append(t)
            cited += [e.id for e in found[:2]]
    if not cited:
        return Finding(req_item, Status.UNKNOWN, (),
                       "nothing in the corpus mentions: " + ", ".join(terms)
                       + " — true or not, it is not written down anywhere",
                       tuple(terms))
    if len(matched) < len(terms):
        unmet = [t for t in terms if t not in matched]
        return Finding(req_item, Status.ARGUABLE, tuple(dict.fromkeys(cited)),
                       "partial — nothing found for: " + ", ".join(unmet), tuple(terms))
    return Finding(req_item, Status.MET, tuple(dict.fromkeys(cited)),
                   "matched on " + ", ".join(matched), tuple(terms))


def score(req: Req, profile: Profile) -> Scorecard:
    card = Scorecard(req=req, profile_name=profile.name)
    for item in req.requirements:
        # A location gate is answered by the posting's own location list.
        object.__setattr__(item, "_req_location", req.location or "")
        card.findings.append(score_requirement(item, profile))

    if req.comp_max and profile.target_band_floor and req.comp_max < profile.target_band_floor:
        card.flags.append(
            f"level mismatch — band tops out at ${req.comp_max:,} against a "
            f"${profile.target_band_floor:,} floor")
    return card
