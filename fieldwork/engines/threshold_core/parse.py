"""Posting text in, classified requirement ledger out.

Verbatim text only. Summaries drop the clauses that decide outcomes -- one
recruiter summary of a req we tested dropped "or software development" from an
experience gate, which was the difference between reachable and blocked.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable

from .classify import HeuristicClassifier, Label

_REQUIRED_HEAD = re.compile(
    r"^\s*#*\s*(?:minimum|basic|required|requirements?|qualifications?|what you.{0,12}ll need|"
    r"required qualifications?|minimum qualifications?)\b", re.I)
_DESIRABLE_HEAD = re.compile(
    r"^\s*#*\s*(?:desirable|preferred|nice to have|bonus|pluses|"
    r"desirable qualifications?|preferred qualifications?)\b", re.I)
_OTHER_HEAD = re.compile(
    r"^\s*#*\s*(?:about|responsibilities|what you.{0,12}ll do|the role|benefits|compensation|"
    r"physical requirements?|equal opportunity|our values)\b", re.I)
_BULLET = re.compile(r"^\s*(?:[-*•–—●▪]|\d+[.)])\s+")


@dataclass
class Requirement:
    ordinal: int
    section: str            # required | desirable
    text: str
    label: Label
    confidence: float
    signals: tuple[str, ...] = ()

    @property
    def is_gate(self) -> bool:
        return self.label is Label.GATE


@dataclass
class Req:
    slug: str
    title: str
    company: str = ""
    location: str = ""
    comp_min: int | None = None
    comp_max: int | None = None
    requirements: list[Requirement] = field(default_factory=list)

    @property
    def gates(self) -> list[Requirement]:
        return [r for r in self.requirements if r.is_gate]

    @property
    def stated_count(self) -> int:
        return len(self.requirements)


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:64]


def parse_bullets(text: str) -> list[tuple[str, str]]:
    """Return (section, bullet) pairs. Section is 'required' or 'desirable'.

    Anything before the first qualifications heading is skipped: responsibilities
    read like requirements and are not screened on.
    """
    section: str | None = None
    out: list[tuple[str, str]] = []
    buffer: list[str] = []

    def flush() -> None:
        if buffer and section:
            joined = " ".join(s.strip() for s in buffer).strip()
            if len(joined) > 12:
                out.append((section, joined))
        buffer.clear()

    for raw in text.splitlines():
        line = raw.rstrip()
        if not line.strip():
            flush()
            continue
        if _DESIRABLE_HEAD.match(line):
            flush(); section = "desirable"; continue
        if _REQUIRED_HEAD.match(line):
            flush(); section = "required"; continue
        if _OTHER_HEAD.match(line):
            flush(); section = None; continue
        if _BULLET.match(line):
            flush()
            buffer.append(_BULLET.sub("", line))
        elif buffer:
            buffer.append(line)          # continuation of the previous bullet
    flush()
    return out


def parse_req(
    text: str,
    title: str,
    company: str = "",
    location: str = "",
    comp: tuple[int | None, int | None] = (None, None),
    slug: str | None = None,
) -> Req:
    clf = HeuristicClassifier()
    req = Req(
        slug=slug or slugify(f"{company}-{title}"),
        title=title, company=company, location=location,
        comp_min=comp[0], comp_max=comp[1],
    )
    for i, (section, bullet) in enumerate(parse_bullets(text), 1):
        pred = clf.predict(bullet, section)
        req.requirements.append(Requirement(
            ordinal=i, section=section, text=bullet,
            label=pred.label, confidence=pred.confidence,
            signals=tuple(pred.fired),
        ))
    return req


def req_from_labeled(posting: dict, company: str = "") -> Req:
    """Build a Req from an already hand-labeled posting in the eval dataset.

    Used for demo data, so the app shows gold labels rather than the baseline
    classifier's guesses -- the two are separated on purpose.
    """
    comp = posting.get("comp") or {}
    req = Req(
        slug=posting["id"], title=posting["title"], company=company,
        location=posting.get("zone", ""),
        comp_min=comp.get("min"), comp_max=comp.get("max"),
    )
    for i, r in enumerate(posting["requirements"], 1):
        req.requirements.append(Requirement(
            ordinal=i, section=r["section"], text=r["text"],
            label=Label(r["label"]), confidence=1.0, signals=("hand-labeled",),
        ))
    return req
