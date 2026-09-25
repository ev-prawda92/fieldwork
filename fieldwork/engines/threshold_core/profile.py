"""The evidence corpus: atomic, sourced claims about one person.

A claim with no source is an assertion, and the scorer will not cite it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import yaml


@dataclass
class Evidence:
    id: str
    kind: str
    title: str = ""
    org: str = ""
    claims: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    source: str = ""
    start: str | None = None
    end: str | None = None
    credential: str = ""
    year: int | None = None
    backed_by: tuple[str, ...] = ()

    @property
    def label(self) -> str:
        bits = [b for b in (self.title, self.org) if b]
        return " · ".join(bits) or self.credential or self.id

    @property
    def haystack(self) -> str:
        return " ".join([self.title, self.org, self.credential, *self.claims,
                         *self.tags]).lower()

    def months(self) -> int:
        return _months(self.start, self.end)


@dataclass
class Profile:
    name: str
    location: str = ""
    current_scope: str = ""
    target_band_floor: int | None = None
    evidence: list[Evidence] = field(default_factory=list)
    absent: tuple[str, ...] = ()

    def by_id(self, eid: str) -> Evidence | None:
        return next((e for e in self.evidence if e.id == eid), None)

    def tagged(self, tag: str) -> list[Evidence]:
        return [e for e in self.evidence if tag in e.tags]

    def employment_months(self) -> int:
        """Total months of employment, merging overlapping spans.

        Founding a company while employed elsewhere is one stretch of time, not
        two. Summing spans double counts and inflates every years-of-experience
        answer, which is the most commonly checked gate there is.
        """
        spans = [(_ym(e.start), _ym(e.end)) for e in self.evidence
                 if e.kind in ("employment", "project") and e.start]
        spans = [(a, b) for a, b in spans if a is not None and b is not None]
        if not spans:
            return 0
        spans.sort()
        merged = [list(spans[0])]
        for a, b in spans[1:]:
            if a <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], b)
            else:
                merged.append([a, b])
        return sum(b - a for a, b in merged)


_YM = re.compile(r"^(\d{4})-(\d{2})$")


def _ym(value: str | None) -> int | None:
    if not value:
        return None
    if str(value).strip().lower() in {"present", "current"}:
        today = date.today()
        return today.year * 12 + today.month
    m = _YM.match(str(value).strip())
    if not m:
        return None
    return int(m.group(1)) * 12 + int(m.group(2))


def _months(start: str | None, end: str | None) -> int:
    a, b = _ym(start), _ym(end)
    return 0 if a is None or b is None else max(0, b - a)


def load_profile(path: str | Path) -> Profile:
    raw = yaml.safe_load(Path(path).read_text())
    person = raw.get("person", {})
    prof = Profile(
        name=person.get("name", "unnamed"),
        location=person.get("location", ""),
        current_scope=person.get("current_scope", ""),
        target_band_floor=person.get("target_band_floor_usd"),
        absent=tuple(raw.get("absent", []) or ()),
    )
    for item in raw.get("evidence", []) or []:
        prof.evidence.append(Evidence(
            id=item["id"], kind=item.get("kind", "other"),
            title=item.get("title", "") or item.get("name", ""),
            org=item.get("org", ""),
            claims=tuple(item.get("claims", []) or ()),
            tags=tuple(item.get("tags", []) or ()),
            source=str(item.get("source", "")),
            start=str(item["start"]) if item.get("start") else None,
            end=str(item["end"]) if item.get("end") else None,
            credential=item.get("credential", ""),
            year=item.get("year"),
            backed_by=tuple(item.get("backed_by", []) or ()),
        ))
    return prof
