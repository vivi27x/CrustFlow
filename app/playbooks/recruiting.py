"""First query for someone hiring: people who already do the role they have open."""

from __future__ import annotations

import re
from typing import Any

from app.playbooks.base import (
    COUNTRY, LOCATION, SENIORITY, TITLE, PillDef, Rung, add_condition, cond, has_filter, person_body,
    replace_condition,
)

LEVEL_WORDS = re.compile(r"\b(senior|sr\.?|lead|staff|principal|junior|jr\.?|ii|iii|iv)\b", re.I)


def core_title(title: str) -> str:
    """'Senior Backend Engineer' becomes 'Backend Engineer'."""
    return " ".join(LEVEL_WORDS.sub(" ", title).split()) or title


def ladder(title: str, city: str, country: str) -> list[Rung]:
    """Strictest first, at most three rungs, which gives the two retries the spec allows."""
    title_condition = cond(TITLE, "(.)", title)
    country_condition = [cond(COUNTRY, "=", country)] if country else []
    rungs: list[Rung] = []
    if city:
        rungs.append(Rung(
            f"{title} in {city}",
            person_body([title_condition, cond(LOCATION, "(.)", city), *country_condition]),
        ))
    rungs.append(Rung(
        f"{title} in {country}" if country else title,
        person_body([title_condition, *country_condition]),
    ))
    if core_title(title) != title:
        rungs.append(Rung(
            f"{core_title(title)} in {country}" if country else core_title(title),
            person_body([cond(TITLE, "(.)", core_title(title)), *country_condition]),
        ))
    elif country:
        rungs.append(Rung(title, person_body([title_condition])))
    return rungs[:3]


def pills(body: dict[str, Any], title: str) -> list[PillDef]:
    result = [
        PillDef(
            "recent", "Recently changed jobs", "People who moved in the last 90 days are often the most reachable.",
            "/person/search", add_condition(body, cond("recently_changed_jobs", "=", True)),
        )
    ]
    if core_title(title) != title:
        wider = core_title(title)
        result.append(PillDef(
            "all_levels", f"All levels: {wider}", "Drops the seniority word from the title.",
            "/person/search",
            replace_condition(body, TITLE, lambda c: {**c, "value": wider}),
        ))
    else:
        result.append(PillDef(
            "senior", "Senior level only", "Adds a seniority filter.",
            "/person/search", add_condition(body, cond(SENIORITY, "in", ["Senior"])),
        ))
    if has_filter(body, LOCATION):
        result.append(PillDef(
            "whole_country", "Whole country", "Removes the city filter.",
            "/person/search", replace_condition(body, LOCATION, lambda c: None),
        ))
    elif has_filter(body, COUNTRY):
        result.append(PillDef(
            "worldwide", "Worldwide", "Removes the country filter.",
            "/person/search", replace_condition(body, COUNTRY, lambda c: None),
        ))
    return result
