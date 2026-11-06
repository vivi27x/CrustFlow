"""First query for someone selling: companies that fit, then the buyers inside them."""

from __future__ import annotations

from typing import Any

from app.playbooks.base import (
    COMPANY_FIELDS, COUNTRY, DOMAIN, EMPLOYER_INDUSTRY, EMPLOYER_SIZE, INDUSTRY, SENIORITY, TITLE, PillDef, Rung,
    add_condition, cond, conditions_of, person_body, replace_condition,
)

# Every value here was checked against company autocomplete, so Claude can't pick one that won't match.
ICP_INDUSTRIES = [
    "Software Development", "Technology, Information and Internet", "IT Services and IT Consulting",
    "Computer and Network Security", "Financial Services", "Banking", "Insurance",
    "Venture Capital and Private Equity Principals", "Human Resources Services", "Staffing and Recruiting",
    "Hospitals and Health Care", "Pharmaceutical Manufacturing", "Biotechnology Research", "Retail",
    "Retail Apparel and Fashion", "Manufacturing", "Motor Vehicle Manufacturing",
    "Transportation, Logistics, Supply Chain and Storage", "Real Estate", "Construction", "Education",
    "Higher Education", "E-Learning Providers", "Advertising Services", "Marketing Services",
    "Business Consulting and Services", "Legal Services", "Accounting", "Telecommunications",
    "Food and Beverage Services", "Hospitality", "Non-profit Organizations", "Government Administration",
    "Oil and Gas", "Utilities", "Entertainment Providers", "Media Production", "Professional Services", "Wholesale",
]
BUYERS = ["CXO", "Vice President", "Director"]
BUYERS_WIDE = [*BUYERS, "Owner / Partner", "Experienced Manager"]


def _company_body(conditions: list[dict[str, Any]], limit: int = 5) -> dict[str, Any]:
    filters = conditions[0] if len(conditions) == 1 else {"op": "and", "conditions": conditions}
    return {"filters": filters, "fields": list(COMPANY_FIELDS), "limit": limit}


def company_ladder(industry: str, country: str, exclude_domain: str) -> list[Rung]:
    """The last rung drops industry too. Our industry guess is the weakest input here: for many
    sellers it's their own industry, which can match nothing at all."""
    market = [cond(INDUSTRY, "in", [industry])] if industry else []
    place = [cond("locations.country", "=", country)] if country else []
    if exclude_domain:
        place.append(cond("basic_info.primary_domain", "!=", exclude_domain))
    size = [cond("headcount.total", "=>", 50), cond("headcount.total", "=<", 1000)]
    growth = [cond("headcount.growth_percent.6m", "=>", 10)]
    kind = f"{industry} companies" if industry else "companies"
    where = f" in {country}" if country else ""
    return [
        Rung(f"Growing {kind}{where}, 50-1000 people", _company_body([*market, *place, *size, *growth])),
        Rung(f"{kind[0].upper()}{kind[1:]}{where}, 50-1000 people, any growth", _company_body([*market, *place, *size])),
        Rung(f"Companies{where} with 50-1000 people, any industry", _company_body([*place, *size])),
    ]


def direct_ladder(buyer_title: str, industry: str, country: str) -> list[Rung]:
    """One search for the buyer's title across the whole market.

    It costs less than finding companies first, and it finds far more: five companies rarely have
    the exact title, so that route used to fall back to whichever executives happened to be there.
    """
    title = cond(TITLE, "(.)", buyer_title)
    market = [cond(EMPLOYER_INDUSTRY, "in", [industry])] if industry else []
    place = [cond(COUNTRY, "=", country)] if country else []
    size = [cond(EMPLOYER_SIZE, "=>", 50), cond(EMPLOYER_SIZE, "=<", 1000)]
    where = f"{buyer_title} in {industry}" if industry else buyer_title
    rungs = [
        Rung(f"{where}, 50-1000 people", person_body([title, *market, *place, *size])),
        Rung(where, person_body([title, *market, *place])),
    ]
    if industry:  # A rare title still leaves the right market, which beats the right title anywhere.
        seniors = cond(SENIORITY, "in", BUYERS_WIDE)
        rungs.append(Rung(f"Senior decision-makers in {industry}", person_body([seniors, *market, *place, *size])))
    return rungs


def buyer_ladder(domains: list[str], buyer_title: str = "") -> list[Rung]:
    at_companies = cond(DOMAIN, "in", domains)
    rungs = [
        Rung("CXO, VP and Director buyers", person_body([at_companies, cond(SENIORITY, "in", BUYERS)])),
        Rung("Buyers including managers and owners", person_body([at_companies, cond(SENIORITY, "in", BUYERS_WIDE)])),
        Rung("Anyone at these companies", person_body([at_companies])),
    ]
    if buyer_title:
        rungs.insert(0, Rung(f"{buyer_title} at these companies", person_body([at_companies, cond(TITLE, "(.)", buyer_title)])))
    return rungs[:3]


def pills(body: dict[str, Any]) -> list[PillDef]:
    has_seniority = any(c["field"] == SENIORITY for c in conditions_of(body))
    has_title = any(c["field"] == TITLE for c in conditions_of(body))
    result = [PillDef(
        "recent", "Recently changed jobs", "New leaders often buy new tools in their first 90 days.",
        "/person/search", add_condition(body, cond("recently_changed_jobs", "=", True)),
    )]
    if has_title or any(c["field"] == EMPLOYER_INDUSTRY for c in conditions_of(body)):
        # The buyer is already set, so widen the search instead of narrowing it.
        if any(c["field"] == EMPLOYER_SIZE for c in conditions_of(body)):
            result.append(PillDef(
                "any_size", "Any company size", "Removes the 50-1000 people filter.",
                "/person/search", replace_condition(body, EMPLOYER_SIZE, lambda c: None),
            ))
        if any(c["field"] == COUNTRY for c in conditions_of(body)):
            result.append(PillDef(
                "worldwide", "Worldwide", "Removes the country filter.",
                "/person/search", replace_condition(body, COUNTRY, lambda c: None),
            ))
    else:
        result.append(PillDef(
            "sales_titles", "Sales leaders", "Adds a title filter for sales.",
            "/person/search", add_condition(body, cond(TITLE, "(.)", "Sales")),
        ))
    if has_seniority:
        result.append(PillDef(
            "cxo", "CXO only", "Narrows seniority to the C-suite.",
            "/person/search", replace_condition(body, SENIORITY, lambda c: {**c, "value": ["CXO"]}),
        ))
    return result
