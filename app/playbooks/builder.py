"""First query for someone building a product: real companies in their market.

An engineer isn't choosing who to sell to. They want to see the API work on data they recognise,
with code they can run.
"""

from __future__ import annotations

from typing import Any

from app.playbooks.base import PillDef, conditions_of, replace_condition

GROWTH = "headcount.growth_percent.6m"
SIZE = "headcount.total"
COUNTRY = "locations.country"


def _has(body: dict[str, Any], field: str) -> bool:
    return any(c["field"] == field for c in conditions_of(body))


def pills(body: dict[str, Any]) -> list[PillDef]:
    result = []
    if _has(body, GROWTH):
        result.append(PillDef(
            "any_growth", "Any growth rate", "Removes the growth filter.",
            "/company/search", replace_condition(body, GROWTH, lambda c: None), "companies",
        ))
    if _has(body, SIZE):
        bigger = {"=>": 1000, "=<": 100000}
        result.append(PillDef(
            "bigger", "Larger companies", "Looks at companies with 1,000 people or more.",
            "/company/search", replace_condition(body, SIZE, lambda c: {**c, "value": bigger.get(c["type"], c["value"])}),
            "companies",
        ))
    if _has(body, COUNTRY):
        result.append(PillDef(
            "worldwide", "Worldwide", "Removes the country filter.",
            "/company/search", replace_condition(body, COUNTRY, lambda c: None), "companies",
        ))
    return result
