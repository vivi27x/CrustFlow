"""Fixed queries for people who picked an intent button, where we know little about them."""

from __future__ import annotations

from typing import Any

from app.playbooks.base import COUNTRY, SENIORITY, TITLE, PillDef, Rung, add_condition, cond, person_body, replace_condition

FALLBACK_COUNTRY = "United States"


def investor_ladder(country: str) -> list[Rung]:
    where = country or FALLBACK_COUNTRY
    founders = [cond(TITLE, "(.)", "Founder"), cond(SENIORITY, "in", ["CXO", "Owner / Partner"])]
    return [
        Rung(f"Founders in {where}", person_body([*founders, cond(COUNTRY, "=", where)])),
        Rung(f"Anyone with a founder title in {where}", person_body([founders[0], cond(COUNTRY, "=", where)])),
        Rung("Founders anywhere", person_body(founders)),
    ]


def investor_pills(body: dict[str, Any]) -> list[PillDef]:
    return [
        PillDef(
            "recent", "Recently changed jobs", "Founders who just started something new.",
            "/person/search", add_condition(body, cond("recently_changed_jobs", "=", True)),
        ),
        PillDef(
            "worldwide", "Worldwide", "Removes the country filter.",
            "/person/search", replace_condition(body, COUNTRY, lambda c: None),
        ),
    ]
