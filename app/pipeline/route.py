"""Decides who sales should see, and writes them a brief that can be checked."""

from __future__ import annotations

import re
from typing import Any

from app.config import Settings
from app.delivery.estimate import estimate_credits
from app.models import Company, Decision, Person, Playbook, QueryOutcome
from app.personas import VendorPersona


# A seat someone holds, not the job that decides what they buy.
HONORARY_ROLE = re.compile(r"\b(council|board|advis[eo]r\w*|ambassador|mentor|committee|member)\b", re.I)


def is_high_fit(settings: Settings, decision: Decision, company: Company, persona: VendorPersona) -> bool:
    segment = persona.segment(decision.segment)
    if segment is None or not segment.sales_alert or not company.matched:
        return False
    return (company.headcount or 0) >= settings.high_fit_min_headcount or (
        company.openings or 0
    ) >= settings.high_fit_min_openings


def job_title_for_sales(person: Person, company: Company) -> str:
    """The title to lead with. People list council seats as a current role, and those misdirect a rep."""
    if person.title and not HONORARY_ROLE.search(person.title):
        return person.title
    for part in re.split(r"\s*[|·•]\s*|\s+-\s+", person.headline):
        role = re.sub(rf"\s+(?:at|@)\s+{re.escape(company.name)}\b.*$", "", part, flags=re.I).strip() if company.name else part.strip()
        if role and not HONORARY_ROLE.search(role):
            return role
    return ""


def company_size(company: Company) -> str:
    size = f"{company.headcount:,} people" if company.headcount else ""
    if size and company.headcount_growth_6m is not None:
        size += f", {round(company.headcount_growth_6m, 1):+g}% in 6 months"
    return size


def freshest_job(jobs: list[dict[str, Any]]) -> str:
    dated = [job for job in jobs if job.get("date_added") and job.get("title")]
    if not dated:
        return ""
    job = max(dated, key=lambda item: item["date_added"])
    where = f" in {job['location']}" if job.get("location") else ""
    return f"Hiring: {job['title']}{where}, posted {job['date_added']} (job/search)"


def freshest_signal(rows: list[dict[str, Any]], jobs: list[dict[str, Any]] | None = None) -> str:
    """The newest date we hold, as proof to open a sales conversation with."""
    candidates: list[tuple[str, str]] = []
    for row in rows:
        name = row.get("name") or "A result"
        title = row.get("title") or "their role"
        company = row.get("company") or "their company"
        if row.get("start_date"):
            candidates.append((row["start_date"][:10], f"{name} started as {title} at {company} on {row['start_date'][:10]}"))
        if row.get("updated_at"):
            candidates.append((row["updated_at"][:10], f"{name}'s profile was updated on {row['updated_at'][:10]}"))
    if not candidates:
        return freshest_job(jobs or []) or "No dated signal in these results"
    return max(candidates, key=lambda item: item[0])[1]


def build_brief(
    person: Person, company: Company, decision: Decision, outcome: QueryOutcome, email: str, fallback: str = ""
) -> dict[str, Any]:
    runs_on_credits = decision.playbook in (Playbook.RECRUITING, Playbook.GTM)
    estimate = estimate_credits(decision.playbook, company) if runs_on_credits else None
    role = job_title_for_sales(person, company)
    size = company_size(company)
    who = ", ".join(part for part in (person.name or email, role, company.name) if part) + (f" ({size})" if size else "")
    return {
        "who": who,
        "email": email,
        "playbook": decision.playbook.value,
        "reasons": [r.to_dict() for r in decision.reasons],
        "angle": decision.sales_angle.to_dict() if decision.sales_angle else None,
        "match_note": person.match_note,
        "fallback": fallback,
        "credits_per_month": estimate.credits_per_month if estimate else None,
        "assumptions": estimate.assumptions if estimate else [],
        "freshest": freshest_signal(outcome.rows, company.jobs),
    }
