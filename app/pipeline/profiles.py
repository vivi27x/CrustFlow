"""Works out who signed up with the endpoints we have, and fills in what we know about them.

Nothing on our key turns an email into a person, so we ask for a name and match it against the
people at the email's company.

`find` always runs first: it's cheap, and it stops repeats and clear students before anything
expensive. A clear match then takes the fast path through `deepen`, which needs a few seconds. The
ReAct loop's report handles everyone `find` can't place, and `deepen` also covers the loop being
off, late or wrong.

The loop lives in `app/pipeline/react_profiles.py` and is turned on with PROFILE_BUILDER=react:

    async def build_report(client, ctx, email, name, persona) -> str  # A user-profile/v1 document.

Rules for the loop:
- Keep the front-matter keys, section headings and `[tool · field · date]` citations, because
  `profile_doc.py` reads them. A report for a different email is rejected.
- Finish inside PROFILE_TIMEOUT_S (240 s from when its turn starts), or the basic builder takes over,
  and the signup is flagged as having fallen back.
- Use `client.post(..., ctx)` for Crustdata calls, so they show in the live call log. Whatever it
  spends outside that must be in `crustdata_credits`, because the signup gets charged the difference.
"""

from __future__ import annotations

import asyncio
import dataclasses
import re
from typing import Any

from app import db
from app.crustdata.client import BudgetExceeded, CrustdataClient, CrustdataError, RunContext
from app.models import Company, Person, dig
from app.personas import VendorPersona
from app.pipeline.context import add_context
from app.pipeline.gates import Email, is_freemail
from app.pipeline.profile_doc import same_site

ROLE_ADDRESSES = {
    "admin", "billing", "careers", "contact", "dev", "founders", "hello", "help", "hi", "hr", "info", "jobs",
    "marketing", "office", "ops", "sales", "support", "team", "test",
}
HONORIFICS = {"dr", "mr", "mrs", "ms", "prof", "professor"}
COMPANY_SUFFIX = re.compile(r"[,.(]|\s(?:llc|inc|ltd|pvt|limited|corp|gmbh)\b", re.I)


def short_name(company: str) -> str:
    """People say Acme, not Acme Pvt. Ltd."""
    return COMPANY_SUFFIX.split(company)[0].strip()

NAME = "basic_profile.name"
CURRENT = "experience.employment_details.current"
SEARCH_FIELDS = ["crustdata_person_id", "basic_profile", "social_handles", "metadata.updated_at"]
ENRICH_FIELDS = [
    "crustdata_person_id", "basic_profile", "experience", "education", "skills", "social_handles", "updated_at",
]

ALL_PARTS_CONFIDENCE = 0.9  # Every part of the name, at this company.
SOME_PARTS_CONFIDENCE = 0.8  # Only part of it, but nobody else at this company fits.


def _cond(field: str, kind: str, value: Any) -> dict[str, Any]:
    return {"field": field, "type": kind, "value": value}


def name_parts(name: str, email: Email) -> list[str]:
    """The name they typed, or the name in the address. Role addresses don't name anyone."""
    words = [w for w in re.split(r"[^a-z]+", (name or email.local).lower()) if w]
    if not name and words and words[0] in ROLE_ADDRESSES:
        return []
    return [w for w in words if len(w) > 1 and w not in HONORIFICS]


def _person_from(data: dict[str, Any], *, confidence: float, method: str, note: str) -> Person:
    profile = data.get("basic_profile") or {}
    return Person(
        status="matched",
        confidence=confidence,
        person_id=data.get("crustdata_person_id"),
        name=profile.get("name") or profile.get("professional_network_name") or "",
        headline=profile.get("headline") or "",
        title=profile.get("current_title") or "",
        department=dig(profile, "normalized_title", "department", default=""),
        city=dig(profile, "location", "city", default=""),
        country=dig(profile, "location", "country", default=""),
        profile_url=dig(data, "social_handles", "professional_network_identifier", "profile_url", default=""),
        updated_at=str(dig(data, "metadata", "updated_at", default="")),
        match_method=method,
        match_note=note,
    )


async def _search(
    client: CrustdataClient, ctx: RunContext, filters: dict[str, Any], note: str, estimate: float
) -> tuple[list[dict[str, Any]], int] | None:
    """The first two profiles and the total. Two is enough to know whether a match is unique."""
    body = {"filters": filters, "fields": SEARCH_FIELDS, "limit": 2}
    try:
        response = await client.post("/person/search", body, ctx, estimate=estimate, note=note)
    except (CrustdataError, BudgetExceeded):
        return None
    return dig(response.data, "profiles", default=[]), dig(response.data, "total_count", default=0)


async def _match_at(
    client: CrustdataClient, ctx: RunContext, where: dict[str, Any], parts: list[str], place: str
) -> Person | None:
    """Tries every part of the name, then any part. None means nobody there fits at all.

    The email and the profile often disagree: noor_khan@ can belong to "Noor Ahmed". A partial
    match is only accepted when it's the one person at the company who fits.
    """
    names = [_cond(NAME, "(.)", part) for part in parts]
    groups = [(names, "all"), ([{"op": "or", "conditions": names}], "any")] if len(parts) > 1 else [(names, "all")]
    for conditions, which in groups:
        found = await _search(
            client, ctx, {"op": "and", "conditions": [where, *conditions]}, f"who is this? {which} of the name at {place}", 0.26
        )
        if found is None:
            return Person(status="unavailable")
        people, total = found
        if total > 1:  # Two people fit, so we can't tell which one this is.
            return Person(status="ambiguous")
        if total == 1 and people:
            full = which == "all" and len(parts) > 1
            return _person_from(
                people[0],
                confidence=ALL_PARTS_CONFIDENCE if full else SOME_PARTS_CONFIDENCE,
                method="name_and_company",
                note=f"matched on {'full name' if full else 'part of the name'} and company",
            )
    return None


async def _match_by_name(client: CrustdataClient, ctx: RunContext, name: str) -> Person:
    """A personal email gives no company, so only a name that one person has is safe to accept."""
    found = await _search(client, ctx, _cond(NAME, "=", " ".join(name.split())), "who is this? exact name, no company", 0.06)
    if found is None:
        return Person(status="unavailable")
    people, total = found
    if total == 1 and people:
        return _person_from(
            people[0], confidence=SOME_PARTS_CONFIDENCE, method="name_only",
            note="matched on name only, not verified against a company",
        )
    return Person(status="ambiguous" if total > 1 else "not_found")


async def _identify_company(client: CrustdataClient, ctx: RunContext, body: dict[str, Any]) -> Company:
    try:
        response = await client.post("/company/identify", body, ctx, cache=True, note="which company?")
    except (CrustdataError, BudgetExceeded):
        return Company()
    match = dig(response.data, 0, "matches", 0)
    if not match:
        return Company()
    company_data = match.get("company_data") or {}
    info = company_data.get("basic_info") or {}
    return Company(
        matched=True,
        company_id=company_data.get("crustdata_company_id"),
        name=info.get("name") or "",
        domain=info.get("primary_domain") or "",
        description=info.get("description") or "",
        company_type=info.get("company_type") or "",
        industries=info.get("industries") or [],
    )


async def resolve_company(client: CrustdataClient, ctx: RunContext, domain: str) -> Company:
    company = await _identify_company(client, ctx, {"domains": [domain]})
    if company.matched and not company.domain:
        company.domain = domain
    return company


async def find(
    client: CrustdataClient, ctx: RunContext, email: Email, name: str, persona: VendorPersona
) -> tuple[Person, Company]:
    """Who is this? Company identify is free, so it goes first."""
    name = " ".join((name or "").split())
    if is_freemail(email.domain):
        if len(name_parts(name, email)) < 2 or not name:
            return Person(status="unavailable"), Company()
        return await _match_by_name(client, ctx, name), Company()

    company = await resolve_company(client, ctx, email.domain)
    parts = name_parts(name, email)
    if not parts:
        return Person(status="unavailable"), company

    person = await _match_at(client, ctx, _cond(f"{CURRENT}.company_website_domain", "=", email.domain), parts, email.domain)
    if person is None and company.name:  # Some profiles record a different website for the employer.
        short = short_name(company.name)
        person = await _match_at(client, ctx, _cond(f"{CURRENT}.company_name", "(.)", short), parts, short)
    return person or Person(status="not_found"), company


def _roles(entries: Any) -> list[str]:
    if not isinstance(entries, list):
        return []
    roles = [", ".join(x for x in (r.get("title"), r.get("name")) if x) for r in entries if isinstance(r, dict)]
    return [r for r in roles if r][:3]


def _schools(entries: Any) -> list[str]:
    if not isinstance(entries, list):
        return []
    schools = []
    for school in entries:
        if isinstance(school, dict):
            parts = (school.get("degree"), school.get("field_of_study"), school.get("school"))
            schools.append(", ".join(str(p) for p in parts if p))
    return [s for s in schools if s][:2]


def _main_role(data: dict[str, Any], company: Company) -> dict[str, Any]:
    """People list several current roles, and the first can be a council seat. Their job is the one here."""
    current = dig(data, "experience", "employment_details", "current", default=[])
    roles = [role for role in (current if isinstance(current, list) else [current]) if isinstance(role, dict)]
    for role in roles:
        if same_site(str(role.get("company_website_domain") or ""), company.domain):
            return role
    for role in roles:
        if company.name and company.name.lower() in str(role.get("name") or "").lower():
            return role
    return roles[0] if roles else {}


async def _enrich_person(client: CrustdataClient, ctx: RunContext, person: Person, company: Company) -> None:
    body = {"professional_network_profile_urls": [person.profile_url], "fields": ENRICH_FIELDS}
    try:
        response = await client.post("/person/enrich", body, ctx, estimate=1.0, cache=True, note="full profile")
    except (CrustdataError, BudgetExceeded):
        return  # What the search gave us is still enough to carry on.
    row = dig(response.data, 0, default={})
    if isinstance(row, dict) and row.get("match_status") == "redacted":
        person.status = "redacted"
        return
    data = dig(row, "matches", 0, "person_data", default={})
    if not data:
        return

    profile = data.get("basic_profile") or {}
    current = _main_role(data, company)
    person.enriched = True
    person.headline = profile.get("headline") or person.headline
    person.title = dig(current, "title", default="") or profile.get("current_title") or person.title
    person.summary = (profile.get("summary") or "")[:400]
    person.employer = dig(current, "name", default="")
    person.employer_domain = dig(current, "company_website_domain", default="")
    person.past_roles = _roles(_past(data))
    person.education = _schools(_education(data))
    person.skills = _skills(data)
    person.updated_at = str(data.get("updated_at") or person.updated_at)


def _past(data: dict[str, Any]) -> Any:
    details = (data.get("experience") or {}).get("employment_details") or {}
    return details.get("past")


def _education(data: dict[str, Any]) -> Any:
    return (data.get("education") or {}).get("schools")


def _skills(data: dict[str, Any]) -> list[str]:
    skills = (data.get("skills") or {}).get("professional_network_skills")
    return [str(s) for s in skills][:10] if isinstance(skills, list) else []


async def deepen(
    client: CrustdataClient, ctx: RunContext, person: Person, company: Company, persona: VendorPersona
) -> None:
    """The profile without the agent. Fills in both records after the gates pass."""
    enrich = person.matched and bool(person.profile_url)
    if company.matched:  # Independent calls, so together they only take as long as the slowest.
        await asyncio.gather(
            *([_enrich_person(client, ctx, person, company)] if enrich else []),
            add_context(client, ctx, company),
        )
        return
    if enrich:  # A personal email: the profile is how we learn the employer.
        await _enrich_person(client, ctx, person, company)
    if person.status != "redacted":
        await research_company(client, ctx, person, company)


async def research_company(client: CrustdataClient, ctx: RunContext, person: Person, company: Company) -> None:
    """Fills in the company. A report only has these numbers in prose, and routing needs them as numbers."""
    if not company.matched and (person.employer_domain or person.employer):
        found = await resolve_company(client, ctx, person.employer_domain) if person.employer_domain else Company()
        if not found.matched and person.employer:
            found = await _identify_company(client, ctx, {"names": [person.employer]})
        for item in dataclasses.fields(Company):
            setattr(company, item.name, getattr(found, item.name))
    if company.matched:
        await add_context(client, ctx, company)


def remember_person(signup_id: str, person_id: int, persona: VendorPersona) -> str:
    """Stores the identity and returns the signup that first claimed it."""
    db.update_signup(signup_id, person_id=person_id)
    return db.claim_identity(f"{persona.id}:person:{person_id}", signup_id)
