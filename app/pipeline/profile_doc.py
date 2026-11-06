"""Reads the profile report the ReAct loop writes. It's the profile we work from.

The report has front matter for the fields our gates need, and sections of facts that each end in
a citation. We keep it as markdown because that's what the loop produces and it's what a person
can check. This module turns it into the `Person`, `Company` and evidence rows the pipeline runs on.

Not everything in a report belongs in front of Claude. Ruled-out namesakes are other people, and
how we proved someone's identity isn't something to quote back at them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.models import Company, Person

SCHEMA = "user-profile/v1"
UNRESOLVED = ("unresolved", "not_found", "failed", "error")

# The agent doesn't confirm identity, so these come from what we can check ourselves.
BOTH_FOUND_THEM = 0.95
DOMAIN_MATCHES = 0.9
NAME_ONLY = 0.8
DOMAIN_DIFFERS = 0.7  # Under match_confidence_min, so we ask rather than risk the wrong person.

EVIDENCE_SECTIONS = (
    "Current role", "Company", "Experience", "Education", "Skills and projects", "Recent activity",
    "Conflicts and caveats",
)
CAVEATS = "Conflicts and caveats"
MAX_FACT = 240

SECTION = re.compile(r"^##\s+(.+?)\s*$")
CITATION = re.compile(r"\s\[(?=[a-z_]+(?:\s·|;|\])|https?://)(.*)\]\s*$")
MATCHED_ON = re.compile(r"^Matched on:\s*(.+)$", re.M)
BILLED = re.compile(r"^\|\s*`(/[^`]+)`\s*\|\s*\d+\s*\|\s*([0-9.]+)\s*\|", re.M)
DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]*\w")
PHONE = re.compile(r"(?<![\d-])\+\d[\d\s().-]{7,}\d|\b\d{10}\b")
PERSONAL_TRAIL = re.compile(r"\b(github|gitlab|commits?|gravatar)\b", re.I)
PERSONAL_SOURCE = re.compile(r"github|(?<![\w-])x\.com\b|twitter\.com|\btweet", re.I)


class ReportError(ValueError):
    """The report can't be trusted, so the basic builder takes over."""


class ReportMeta(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    schema_name: str = Field(alias="schema")
    email: str
    status: str = ""  # Early reports only: the agent's format has neither of these.
    confidence: str = ""
    name: str = ""
    resolved_name: str = ""
    crustdata_person_id: str | int | None = None
    crustdata_company_id: str | int | None = None
    linkedin_url: str = ""
    headline: str = ""
    current_title: str = ""
    current_company: str = ""
    company_domain: str = ""
    location: str = ""
    persona: str = ""
    crustdata_credits: float = 0.0
    tools_used: list[str] = []


@dataclass
class Fact:
    text: str
    source: str = "profile report"
    field: str = ""
    date: str = ""


@dataclass
class Report:
    meta: ReportMeta
    sections: dict[str, list[Fact]] = field(default_factory=dict)
    raw: str = ""
    matched_on: str = ""
    billed: dict[str, float] = field(default_factory=dict)  # Endpoint to credits, from the agent's table.

    @property
    def person_name(self) -> str:
        return self.meta.name or self.meta.resolved_name

    @property
    def resolved(self) -> bool:
        return bool(self.person_name) and self.meta.status not in UNRESOLVED

    @property
    def credits(self) -> float:
        return self.meta.crustdata_credits or round(sum(self.billed.values()), 4)

    @property
    def tools(self) -> list[str]:
        return self.meta.tools_used or list(self.billed)


def mask(text: str) -> str:
    """Email citations print the cited fact, so contact details never become evidence."""
    return PHONE.sub("[phone]", EMAIL.sub("[email]", text))


def _fact(line: str) -> Fact:
    body = line[2:].strip()
    found = CITATION.search(body)
    if not found:
        return Fact(body)
    first = [part.strip() for part in found.group(1).split(";")[0].split("·")]
    date = next((part for part in first if DATE.fullmatch(part)), "")
    text = body[:found.start()].strip()
    if first[0].startswith("http"):
        return Fact(text, "web", first[0], date)
    return Fact(text, first[0], first[1] if len(first) > 1 else "", date)


def _sections(body: str) -> dict[str, list[Fact]]:
    sections: dict[str, list[Fact]] = {}
    current = ""
    for line in body.splitlines():
        heading = SECTION.match(line)
        if heading:
            current = heading.group(1)
            sections.setdefault(current, [])
        elif current and line.startswith("- ") and not line[2:].lower().startswith("nothing found"):
            sections[current].append(_fact(line))
    return sections


def parse(text: str, email: str) -> Report:
    """Raises ReportError for anything we can't trust, including a report about someone else."""
    parts = (text or "").split("---\n", 2)
    if len(parts) != 3 or parts[0].strip():
        raise ReportError("no front matter")
    try:
        data: Any = yaml.safe_load(parts[1])
        meta = ReportMeta.model_validate(data)
    except (yaml.YAMLError, ValidationError) as exc:
        raise ReportError(f"bad front matter: {type(exc).__name__}") from exc
    if meta.schema_name != SCHEMA:
        raise ReportError(f"unknown schema {meta.schema_name}")
    if meta.email.strip().lower() != email.strip().lower():
        raise ReportError("the report is about a different email")
    matched = MATCHED_ON.search(parts[2])
    billed = {path: float(credits) for path, credits in BILLED.findall(parts[2])}
    return Report(meta, _sections(parts[2]), text, mask(matched.group(1).strip())[:200] if matched else "", billed)


def _is_personal_trail(fact: Fact) -> bool:
    """Commit history proves identity, and posts are their own words. Neither is something to say to them."""
    if fact.source in ("email_lookup", "social_post_search"):
        return True
    return bool(PERSONAL_SOURCE.search(f"{fact.source} {fact.field} {fact.text}"))


def evidence_rows(report: Report) -> list[dict[str, str]]:
    rows = []
    for section in EVIDENCE_SECTIONS:
        for fact in report.sections.get(section, []):
            if _is_personal_trail(fact):
                continue
            text = mask(fact.text)[:MAX_FACT]
            value = f"Caveat: {text}" if section == CAVEATS else text
            rows.append({"source": fact.source, "field": fact.field or section, "value": value, "date": fact.date})
    return rows


def same_site(one: str, other: str) -> bool:
    """cs.princeton.edu and princeton.edu are the same place."""
    one, other = one.lower(), other.lower()
    return bool(one and other) and (one == other or one.endswith(f".{other}") or other.endswith(f".{one}"))


def same_person(report: Report, person: Person) -> bool:
    """Did the name check find the same person? Agents often leave the id blank, so names count then."""
    person_id = str(report.meta.crustdata_person_id or "")
    if person_id and person.person_id is not None:
        return person_id == str(person.person_id)
    return person.matched and report.person_name.strip().lower() == person.name.strip().lower()


def work_summary(text: str) -> str:
    """The report's one-line summary, without the commit trail that sometimes ends up in it."""
    clauses = [c.strip() for c in re.split(r"(?<=[.;])\s+", text) if c.strip()]
    kept = [c for c in clauses if not PERSONAL_TRAIL.search(c)]
    return mask(" ".join(kept))[:MAX_FACT]


def identity(report: Report, person: Person, domain: str, personal_email: bool) -> tuple[float, str]:
    """How sure we are this report is about the person who signed up, and how to say so.

    The agent can settle on a namesake, so we check what we can before trusting it.
    """
    meta = report.meta
    if same_person(report, person):
        return BOTH_FOUND_THEM, "identity confirmed: two searches found them"
    if meta.confidence == "confirmed":  # Early reports state this themselves.
        return BOTH_FOUND_THEM, "identity confirmed"
    if personal_email:
        return NAME_ONLY, "matched on name only, not verified against a company"
    if same_site(meta.company_domain, domain):
        return DOMAIN_MATCHES, "matched on name and company"
    return DOMAIN_DIFFERS, "the profile's employer doesn't match the email's domain"


def apply(
    report: Report, person: Person, company: Company, domain: str, personal_email: bool
) -> tuple[Person, Company]:
    """The report wins when it resolved someone. If it didn't, what the pre-check found stands.

    When both found the same person, a blank in the report keeps what the pre-check knew.
    """
    if not report.resolved:
        return person, company
    meta = report.meta
    confidence, note = identity(report, person, domain, personal_email)
    places = [part.strip() for part in meta.location.split(",") if part.strip()]
    person_id = str(meta.crustdata_person_id or "")
    known = person if same_person(report, person) else Person()
    resolved = Person(
        status="matched",
        confidence=confidence,
        person_id=int(person_id) if person_id.isdigit() else known.person_id,
        name=report.person_name,
        headline=meta.headline or known.headline,
        title=meta.current_title or known.title,
        city=places[0] if places else known.city,
        country=places[-1] if len(places) > 1 else known.country,
        profile_url=meta.linkedin_url or known.profile_url,
        match_method="report",
        match_note=note,
        summary=work_summary(meta.persona),
        employer=meta.current_company or (company.name if known.matched else ""),
        employer_domain=meta.company_domain or (company.domain if known.matched else ""),
        extra_evidence=evidence_rows(report),
    )
    if company.matched and (same_site(company.domain, meta.company_domain) or (known.matched and not meta.company_domain)):
        return resolved, company
    company_id = str(meta.crustdata_company_id or "")
    return resolved, Company(
        matched=bool(meta.company_domain),
        company_id=int(company_id) if company_id.isdigit() else None,
        name=meta.current_company,
        domain=meta.company_domain,
    )
