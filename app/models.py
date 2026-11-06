"""Shared types. Every pipeline step reads and writes these."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict


# Crustdata returns ISO codes in some fields. Person search and people reading an email need names.
COUNTRY_NAMES = {
    "ARE": "United Arab Emirates", "AUS": "Australia", "BGD": "Bangladesh", "BRA": "Brazil", "CAN": "Canada",
    "CHE": "Switzerland", "CHN": "China", "DEU": "Germany", "ESP": "Spain", "FRA": "France",
    "GBR": "United Kingdom", "IDN": "Indonesia", "IND": "India", "IRL": "Ireland", "ISR": "Israel",
    "ITA": "Italy", "JPN": "Japan", "KOR": "South Korea", "MEX": "Mexico", "NGA": "Nigeria", "NLD": "Netherlands",
    "NZL": "New Zealand", "PAK": "Pakistan", "PHL": "Philippines", "POL": "Poland", "SGP": "Singapore",
    "SWE": "Sweden", "USA": "United States", "VNM": "Vietnam", "ZAF": "South Africa",
}


def dig(obj: Any, *path: str | int, default: Any = None) -> Any:
    """Walks nested data. Unexpected shapes give the default instead of raising."""
    for key in path:
        if isinstance(key, int):
            if not isinstance(obj, list) or len(obj) <= key:
                return default
            obj = obj[key]
        else:
            if isinstance(obj, list):  # `current` is an object in some responses, a list in others.
                obj = obj[0] if obj else None
            if not isinstance(obj, dict) or key not in obj:
                return default
            obj = obj[key]
    return default if obj is None else obj


class Status(StrEnum):
    RECEIVED = "received"
    GATING = "gating"
    IDENTIFYING = "identifying"
    RESEARCHING = "researching"
    CLASSIFYING = "classifying"
    QUERYING = "querying"
    DELIVERING = "delivering"
    # Terminal
    DELIVERED = "delivered"
    ACTIVATED = "activated"
    AWAITING_INTENT = "awaiting_intent"
    NURTURE = "nurture"
    HUMAN_QUEUE = "human_queue"
    INTERNAL = "internal"
    REJECTED = "rejected"
    WITHDRAWN = "withdrawn"
    FAILED_SAFE = "failed_safe"


class Playbook(StrEnum):
    RECRUITING = "recruiting"
    GTM = "gtm"
    ACADEMIC = "academic"
    STUDENT = "student"
    UNKNOWN = "unknown"
    INVESTOR = "investor"
    BUILDER = "builder"  # Engineers building on the API: real companies in their market.
    MESSAGE = "message"  # No search to run: the vendor isn't selling data.


@dataclass
class Evidence:
    """One fact from a real response. Citations are rendered from these, never from model text."""

    id: str
    source: str  # e.g. "job/search"
    field: str  # e.g. "job_details.title"
    value: str
    date: str = ""

    def citation(self) -> str:
        parts = [self.source, self.field]
        if self.date:
            parts.append(self.date)
        return ", ".join(parts)


class EvidenceTable:
    def __init__(self) -> None:
        self.items: list[Evidence] = []

    def add(self, source: str, field_name: str, value: Any, date: str = "") -> str | None:
        if value is None or value == "" or value == []:
            return None
        item = Evidence(f"E{len(self.items) + 1}", source, field_name, str(value), date or "")
        self.items.append(item)
        return item.id

    def get(self, evidence_id: str) -> Evidence | None:
        return next((e for e in self.items if e.id == evidence_id), None)

    def as_prompt_rows(self) -> list[dict[str, str]]:
        return [
            {"id": e.id, "source": e.source, "field": e.field, "value": e.value, "date": e.date}
            for e in self.items
        ]


@dataclass
class Reason:
    claim: str
    evidence: list[Evidence]

    def to_dict(self) -> dict[str, Any]:
        return {
            "claim": self.claim,
            "citations": [{"id": e.id, "value": e.value, "citation": e.citation()} for e in self.evidence],
        }


@dataclass
class Pick:
    """A service Claude chose for this person, and why."""

    service: str
    reason: Reason


@dataclass
class Person:
    status: str = "not_found"  # matched | not_found | redacted | unavailable
    confidence: float = 0.0
    person_id: int | None = None
    name: str = ""
    headline: str = ""
    title: str = ""
    department: str = ""
    city: str = ""
    country: str = ""
    profile_url: str = ""
    updated_at: str = ""
    match_method: str = ""  # name_and_company, name_only or report.
    match_note: str = ""  # How sure we are, in words. Shown with citations.
    enriched: bool = False
    summary: str = ""
    employer: str = ""
    employer_domain: str = ""
    past_roles: list[str] = field(default_factory=list)
    education: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    extra_evidence: list[dict[str, str]] = field(default_factory=list)  # {source, field, value, date}

    @property
    def matched(self) -> bool:
        return self.status == "matched"


@dataclass
class Company:
    matched: bool = False
    company_id: int | None = None
    name: str = ""
    domain: str = ""
    description: str = ""
    company_type: str = ""
    industries: list[str] = field(default_factory=list)
    industries_cited: tuple[str, str] = ("company/identify", "basic_info.industries")
    headcount: int | None = None
    headcount_growth_6m: float | None = None
    funding_total: float | None = None
    funding_round: str = ""
    funding_date: str = ""
    openings: int | None = None
    country: str = ""
    jobs: list[dict[str, Any]] = field(default_factory=list)
    jobs_total: int | None = None


COLUMN_TYPES = ("text", "integer", "numeric", "boolean", "timestamptz")


class StarterColumn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    type: str


class StarterSpec(BaseModel):
    """What Claude proposes for a first table. `app/pipeline/starter.py` writes the SQL from it."""

    model_config = ConfigDict(extra="forbid")

    table: str
    purpose: str
    columns: list[StarterColumn]
    rows: list[list[str]]
    search: str
    other_search: str


class Slots(BaseModel):
    """Values the model may propose. Each is snapped to a canonical value before use."""

    model_config = ConfigDict(extra="forbid")

    target_role: str = ""
    target_city: str = ""
    target_country: str = ""
    icp_industry: str = ""
    research_field: str = ""
    buyer_title: str = ""


@dataclass
class Decision:
    playbook: Playbook
    confidence: float
    reasons: list[Reason]
    slots: Slots
    decided_by: str  # "rules" | "llm" | "rules-fallback" | "intent"
    segment: str = ""  # The persona's segment id.
    opening: Reason | None = None  # Claude's first lines, with their citations.
    subject: str = ""
    picks: list[Pick] = field(default_factory=list)
    starter: StarterSpec | None = None  # Claude's spec. Compiled to SQL, or dropped, at delivery.
    sales_angle: Reason | None = None


@dataclass
class Attempt:
    """One search we actually sent."""

    label: str
    endpoint: str
    body: dict[str, Any]
    result_count: int = 0
    total_count: int | None = None
    credits: float = 0.0
    error: str = ""


@dataclass
class QueryOutcome:
    """What the signup receives. `primary` is the exact request behind `rows`."""

    playbook: Playbook
    primary: Attempt | None
    rows: list[dict[str, Any]]
    attempts: list[Attempt] = field(default_factory=list)
    supporting: Attempt | None = None  # gtm: the company search that fed the person search
    supporting_rows: list[dict[str, Any]] = field(default_factory=list)
    relaxed: list[str] = field(default_factory=list)
    snaps: list[dict[str, str]] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.rows
