"""Pieces shared by every playbook. Templates here are fixed; only their values vary."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from collections.abc import Callable
from typing import Any

from app.models import COUNTRY_NAMES, dig

TITLE = "experience.employment_details.current.title"
SENIORITY = "experience.employment_details.current.seniority_level"
DOMAIN = "experience.employment_details.current.company_website_domain"
COUNTRY = "basic_profile.location.country"
LOCATION = "basic_profile.location.full_location"
INDUSTRY = "taxonomy.professional_network_industry"
EMPLOYER_INDUSTRY = "experience.employment_details.current.company_industries"
EMPLOYER_SIZE = "experience.employment_details.current.company_headcount_latest"
UPDATED_AT = "metadata.updated_at"

PERSON_FIELDS = [
    "crustdata_person_id",
    "basic_profile.name",
    "experience.employment_details.current.title",
    "experience.employment_details.current.name",
    "experience.employment_details.current.start_date",
    "social_handles.professional_network_identifier.profile_url",
    UPDATED_AT,
]
# Free fields only. Receiving headcount adds 0.2 credits per row, and taxonomy adds 0.1.
COMPANY_FIELDS = [
    "crustdata_company_id",
    "basic_info.name",
    "basic_info.primary_domain",
    "basic_info.employee_count_range",
    "locations.country",
]

# Nice to have, used only for the sales freshness signal. If a key refuses them, search without them.
OPTIONAL_FIELDS = {UPDATED_AT, "experience.employment_details.current.start_date"}

PREMIUM_GROUPS = ("experience", "education", "skills")
# Credits per row, charged once to filter on a group and once to receive it. From /account/endpoints.
COMPANY_PREMIUM = {"taxonomy": 0.1, "headcount": 0.2, "funding": 0.2, "hiring": 0.2, "followers": 0.2, "roles": 0.2}


@dataclass
class Rung:
    """One step on a relax ladder: the query, and what we gave up to get to it."""

    label: str
    body: dict[str, Any]


@dataclass
class PillDef:
    id: str
    label: str
    hint: str
    endpoint: str
    body: dict[str, Any]
    kind: str = "people"


def cond(field: str, kind: str, value: Any) -> dict[str, Any]:
    return {"field": field, "type": kind, "value": value}


def person_body(conditions: list[dict[str, Any]], limit: int = 10) -> dict[str, Any]:
    filters = conditions[0] if len(conditions) == 1 else {"op": "and", "conditions": conditions}
    return {"filters": filters, "fields": list(PERSON_FIELDS), "limit": limit}


def conditions_of(body: dict[str, Any]) -> list[dict[str, Any]]:
    filters = body.get("filters") or {}
    return list(filters["conditions"]) if "conditions" in filters else ([filters] if filters else [])


def with_conditions(body: dict[str, Any], conditions: list[dict[str, Any]]) -> dict[str, Any]:
    updated = copy.deepcopy(body)
    updated["filters"] = conditions[0] if len(conditions) == 1 else {"op": "and", "conditions": conditions}
    return updated


def add_condition(body: dict[str, Any], condition: dict[str, Any]) -> dict[str, Any]:
    return with_conditions(body, [*conditions_of(body), condition])


def replace_condition(
    body: dict[str, Any], field: str, build: Callable[[dict[str, Any]], dict[str, Any] | None]
) -> dict[str, Any]:
    """Rewrites or drops one filter. Pills use this so that each changes exactly one thing."""
    updated = []
    for condition in conditions_of(body):
        if condition["field"] != field:
            updated.append(condition)
            continue
        replacement = build(condition)
        if replacement is not None:
            updated.append(replacement)
    return with_conditions(body, updated)


def has_filter(body: dict[str, Any], field: str) -> bool:
    return any(c["field"] == field for c in conditions_of(body))


def _groups(paths: list[str]) -> int:
    return len({p.split(".")[0] for p in paths} & set(PREMIUM_GROUPS))


def person_cost_per_result(body: dict[str, Any]) -> float:
    """0.03 base, plus 0.1 per premium group filtered on and 0.1 per premium group received."""
    filter_paths = [c["field"] for c in conditions_of(body)]
    return round(0.03 + 0.1 * _groups(filter_paths) + 0.1 * _groups(body.get("fields", [])), 4)


def company_cost_per_result(body: dict[str, Any]) -> float:
    """0.03 base, plus each premium group filtered on and each premium group received."""
    filtered = {c["field"].split(".")[0] for c in conditions_of(body)}
    received = {path.split(".")[0] for path in body.get("fields", [])}
    premium = sum(COMPANY_PREMIUM.get(group, 0) for group in filtered) + sum(COMPANY_PREMIUM.get(group, 0) for group in received)
    return round(0.03 + premium, 4)


def person_rows(data: Any) -> list[dict[str, Any]]:
    rows = []
    for profile in dig(data, "profiles", default=[]) or []:
        if not isinstance(profile, dict):
            continue
        current = dig(profile, "experience", "employment_details", "current", default={})
        rows.append({
            "name": dig(profile, "basic_profile", "name", default=""),
            "title": dig(current, "title", default=""),
            "company": dig(current, "name", default=""),
            "url": dig(profile, "social_handles", "professional_network_identifier", "profile_url", default=""),
            # Kept for the sales freshness signal. Never rendered in the signup's email.
            "start_date": str(dig(current, "start_date", default="")),
            "updated_at": str(dig(profile, "metadata", "updated_at", default="")),
        })
    return [r for r in rows if r["name"]]


def company_rows(data: Any) -> list[dict[str, Any]]:
    rows = []
    for company in dig(data, "companies", default=[]) or []:
        if not isinstance(company, dict):
            continue
        rows.append({
            "name": dig(company, "basic_info", "name", default=""),
            "domain": dig(company, "basic_info", "primary_domain", default=""),
            "size": dig(company, "basic_info", "employee_count_range", default=""),
            "country": dig(company, "locations", "country", default=""),
            # The same row works in a results table: company, size, country, link.
            "title": dig(company, "basic_info", "employee_count_range", default=""),
            "company": COUNTRY_NAMES.get(raw_country := dig(company, "locations", "country", default=""), raw_country),
            "url": f"https://{domain}" if (domain := dig(company, "basic_info", "primary_domain", default="")) else "",
        })
    return [r for r in rows if r["name"]]
