"""Company research, and the evidence table the rest of the pipeline cites."""

from __future__ import annotations

import asyncio
import datetime as dt
from typing import Any

from app.crustdata.client import BudgetExceeded, CrustdataClient, CrustdataError, RunContext
from app.models import COUNTRY_NAMES, Company, EvidenceTable, Person
from app.models import dig

ENRICH_FIELDS = ["basic_info", "headcount", "funding", "hiring", "taxonomy"]
JOB_FIELDS = [
    "job_details.title",
    "job_details.category",
    "job_details.source",
    "job_details.url",
    "company.basic_info.name",
    "location.raw",
    "metadata.date_added",
]


async def add_context(client: CrustdataClient, ctx: RunContext, company: Company) -> None:
    """Fills in the company in place. Either call can fail and the signup still gets served.

    The calls are independent, so running them together costs one wait instead of two.
    """
    await asyncio.gather(_enrich(client, ctx, company), _jobs(client, ctx, company))


async def _enrich(client: CrustdataClient, ctx: RunContext, company: Company) -> None:
    try:
        response = await client.post(
            "/company/enrich",
            {"domains": [company.domain], "fields": ENRICH_FIELDS},
            ctx,
            estimate=2.0,
            cache=True,
            note="company profile",
        )
    except (CrustdataError, BudgetExceeded):
        return
    data = dig(response.data, 0, "matches", 0, "company_data", default={})
    if not data:
        return
    info = data.get("basic_info") or {}
    company.description = info.get("description") or company.description
    company.company_type = info.get("company_type") or company.company_type
    industries = dig(data, "taxonomy", "professional_network_industries", default=[])
    if industries:
        company.industries = industries
        company.industries_cited = ("company/enrich", "taxonomy.professional_network_industries")
    company.headcount = dig(data, "headcount", "total")
    country = dig(data, "headcount", "largest_headcount_country", default="")
    company.country = COUNTRY_NAMES.get(country, country) or company.country
    company.headcount_growth_6m = dig(data, "headcount", "growth_percent", "six_months")
    company.funding_total = dig(data, "funding", "total_investment_usd")
    company.funding_round = dig(data, "funding", "last_round_type", default="")
    company.funding_date = str(dig(data, "funding", "last_fundraise_date", default=""))
    company.openings = dig(data, "hiring", "openings_count")


async def _jobs(client: CrustdataClient, ctx: RunContext, company: Company) -> None:
    body = {
        "filters": {"field": "company.basic_info.primary_domain", "type": "=", "value": company.domain},
        "sorts": [{"field": "metadata.date_added", "order": "desc"}],
        "fields": JOB_FIELDS,
        "limit": 5,
    }
    try:
        response = await client.post("/job/search", body, ctx, estimate=0.15, note="open roles")
    except (CrustdataError, BudgetExceeded):
        return
    listings = dig(response.data, "job_listings", default=[])
    company.jobs = [_job(listing) for listing in listings if isinstance(listing, dict)]
    company.jobs_total = dig(response.data, "total_count")


def _job(listing: dict[str, Any]) -> dict[str, str]:
    return {
        "title": dig(listing, "job_details", "title", default=""),
        "category": dig(listing, "job_details", "category", default=""),
        "source": dig(listing, "job_details", "source", default=""),
        "location": dig(listing, "location", "raw", default=""),
        "date_added": str(dig(listing, "metadata", "date_added", default=""))[:10],
    }


def build_evidence(person: Person, company: Company) -> EvidenceTable:
    table = EvidenceTable()
    updated = person.updated_at[:10]
    # A citation names the call that returned the fact, and how sure we are it's this person.
    source = "person/enrich" if person.enriched else "person/search"
    if person.match_method == "report":
        source = "profile report"
    if person.match_note:
        source = f"{source}, {person.match_note}"
    table.add(source, "basic_profile.current_title", person.title, updated)
    table.add(source, "basic_profile.headline", person.headline, updated)
    table.add(source, "basic_profile.normalized_title.department", person.department, updated)
    table.add(source, "basic_profile.location", ", ".join(x for x in (person.city, person.country) if x), updated)
    table.add(source, "basic_profile.summary", person.summary, updated)
    for role in person.past_roles:
        table.add(source, "experience.employment_details.past", role, updated)
    for school in person.education:
        table.add(source, "education.schools", school, updated)
    table.add(source, "skills.professional_network_skills", ", ".join(person.skills), updated)
    for item in person.extra_evidence:
        table.add(item.get("source", "agent"), item.get("field", ""), item.get("value"), item.get("date", ""))

    today = dt.date.today().isoformat()
    if company.matched:
        table.add("company/identify", "basic_info.description", company.description[:240], today)
        table.add(*company.industries_cited, ", ".join(company.industries), today)
        table.add("company/enrich", "headcount.total", company.headcount, today)
        if company.headcount_growth_6m is not None:
            table.add("company/enrich", "headcount.growth_percent.six_months", f"{company.headcount_growth_6m}%", today)
        if company.funding_total:
            label = f"${company.funding_total:,.0f}" + (f", {company.funding_round}" if company.funding_round else "")
            table.add("company/enrich", "funding.total_investment_usd", label, company.funding_date)
        table.add("company/enrich", "hiring.openings_count", company.openings, today)
    for job in company.jobs:
        where = f" in {job['location']}" if job["location"] else ""
        via = f" via {job['source']}" if job["source"] else ""
        table.add("job/search", "job_details.title", f"{job['title']}{where}{via}", job["date_added"])
    return table
