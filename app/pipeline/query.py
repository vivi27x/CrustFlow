"""Turns a decision into a search: snap values, send, relax if thin, and pick what to deliver."""

from __future__ import annotations

import difflib
import re
from collections.abc import Callable
from typing import Any

from app import db, events
from app.crustdata.client import (
    BudgetExceeded,
    CrustdataClient,
    CrustdataError,
    OutOfCredits,
    RunContext,
)
from app.models import Attempt, Decision, Playbook, QueryOutcome
from app.models import dig
from app.playbooks import academic, builder, defaults, gtm, recruiting
from app.playbooks.base import (
    INDUSTRY, OPTIONAL_FIELDS, Rung, company_cost_per_result, company_rows, person_cost_per_result, person_rows,
)

TARGET_PEOPLE = 10
TARGET_COMPANIES = 5
ENOUGH_COMPANIES = 3  # Enough to find buyers at. Relaxing past this spends what the people search needs.
ENOUGH_BUYERS = 5  # A direct search that finds this many beats a wider one with vaguer titles.
FALLBACK_ROLE = "Software Engineer"
TITLE_FIELD = "experience.employment_details.current.title"
PERSON_AUTOCOMPLETE = "/person/search/autocomplete"
COMPANY_AUTOCOMPLETE = "/company/search/autocomplete"


def agent_note(ctx: RunContext, endpoint: str, note: str) -> None:
    """Shows the agent's own moves, such as a snap or a relax, in the call log."""
    events.publish("call", db.log_call(signup_id=ctx.signup_id, kind="agent", endpoint=endpoint, note=note))


def looks_like(value: str, suggestion: str) -> bool:
    """Rejects suggestions that merely happen to come first for an unrelated query."""
    a, b = value.lower(), suggestion.lower()
    if set(a.split()) & set(b.split()):
        return True
    return difflib.SequenceMatcher(None, a, b).ratio() >= 0.6


def best_suggestion(value: str, suggestions: list[dict[str, Any]]) -> str | None:
    """An exact match wins, so "Bangalore" can't become "Bangalore Rural". After that, prefer
    suggestions with every word typed, then the most common, so a title doesn't drift narrower."""
    wanted = value.strip().lower()
    for suggestion in suggestions:
        if suggestion["value"].lower() == wanted:
            return suggestion["value"]
    close = [s for s in suggestions if looks_like(value, s["value"])]
    words = set(wanted.split())
    pool = [s for s in close if words <= set(s["value"].lower().split())] or close
    return max(pool, key=lambda s: s.get("document_count") or 0)["value"] if pool else None


def strip_padding(title: str) -> str:
    """Job posts pad titles: "Security Engineer, Privy" or "Account Executive - Enterprise"."""
    return re.split(r"\s*[,(|]\s*|\s+[-–—]\s+", title, maxsplit=1)[0].strip() or title


async def snap(
    client: CrustdataClient, ctx: RunContext, path: str, field: str, value: str, outcome: QueryOutcome,
    *, closed_list: bool = False,
) -> str:
    """Returns the canonical value, or the original when nothing fits. It never blocks a search.

    On a closed list, such as countries, an unknown value would match nobody on every rung, so it's dropped.
    """
    if not value:
        return value
    try:
        response = await client.post(
            path, {"field": field, "query": value, "limit": 5}, ctx, cache=True, note=f'snap "{value}"'
        )
    except (CrustdataError, BudgetExceeded):
        return value
    suggestions = [s for s in dig(response.data, "suggestions", default=[]) if isinstance(s, dict) and s.get("value")]
    match = best_suggestion(value, suggestions)
    short_field = field.rsplit(".", 1)[-1]
    if match and match != value:
        outcome.snaps.append({"field": short_field, "from": value, "to": match})
        agent_note(ctx, "snap", f'{short_field}: "{value}" snapped to "{match}"')
        return match
    if not match:
        action = "dropped" if closed_list else "kept as typed"
        agent_note(ctx, "snap", f'{short_field}: no canonical match for "{value}", {action}')
        return "" if closed_list else value
    return value


async def send_search(
    client: CrustdataClient, ctx: RunContext, endpoint: str, body: dict[str, Any], estimate: float, note: str
) -> tuple[Any, dict[str, Any]]:
    """Sends a search and returns the body actually used, which is what the signup's code must show.

    The docs' answer to a field denial is to resend with a narrower list. Without this, one field
    our key can't have would fail every search for every signup.
    """
    try:
        return await client.post(endpoint, body, ctx, estimate=estimate, note=note), body
    except OutOfCredits:
        raise
    except CrustdataError as exc:
        narrower = [f for f in body.get("fields", []) if f not in OPTIONAL_FIELDS]
        refused_a_field = exc.status in (400, 403) and "field" in exc.message.lower()
        if not refused_a_field or len(narrower) == len(body.get("fields", [])):
            raise
        agent_note(ctx, "self-heal", f"API refused a field ({exc.message[:80]}). Retrying without optional fields")
        narrowed = {**body, "fields": narrower}
        return await client.post(endpoint, narrowed, ctx, estimate=estimate, note=f"{note}, narrower fields"), narrowed


async def _run_ladder(
    client: CrustdataClient,
    ctx: RunContext,
    outcome: QueryOutcome,
    endpoint: str,
    rungs: list[Rung],
    cost_of: Callable[[dict[str, Any]], float],
    rows_of: Callable[[Any], list[dict[str, Any]]],
    target: int,
    *,
    enough: int | None = None,
    reserve: float = 0,
) -> tuple[Attempt | None, list[dict[str, Any]]]:
    """Walks the ladder. Results are never merged, so the emailed code reproduces the emailed rows.

    `enough` stops relaxing early, and `reserve` keeps credits back for a later step.
    """
    best: Attempt | None = None
    best_rows: list[dict[str, Any]] = []

    for index, rung in enumerate(rungs):
        cost = cost_of(rung.body)
        body = dict(rung.body)
        if "limit" in body:
            spendable = max(ctx.ledger.remaining - reserve, 0)
            limit = min(target, int((spendable + 1e-9) // cost)) if cost else target
            if limit < 1 or limit <= len(best_rows):  # A rung that can't beat what we have isn't worth paying for.
                agent_note(ctx, "budget", f"Stopped before {rung.label!r}: credit cap reached")
                break
            if limit < target:
                agent_note(ctx, "budget", f"Limit reduced to {limit} to stay inside the credit cap")
            body["limit"] = limit
        else:
            limit = target
        attempt = Attempt(label=rung.label, endpoint=endpoint, body=body)
        outcome.attempts.append(attempt)
        try:
            estimate = cost * limit if "limit" in body else cost
            response, attempt.body = await send_search(client, ctx, endpoint, body, estimate, rung.label)
        except BudgetExceeded:
            attempt.error = "credit cap"
            break
        except OutOfCredits as exc:
            attempt.error = exc.error_type
            break
        except CrustdataError as exc:
            attempt.error = f"{exc.status} {exc.error_type}"
            agent_note(ctx, "relax", f"{rung.label!r} failed ({exc.error_type}), trying the next step")
            continue

        rows = rows_of(response.data)
        attempt.result_count = len(rows)
        attempt.total_count = dig(response.data, "total_count")
        attempt.credits = response.credits
        if rows and len(rows) > len(best_rows):
            best, best_rows = attempt, rows
        if len(rows) >= min(enough or limit, limit):
            break
        if index + 1 < len(rungs):
            next_label = rungs[index + 1].label
            outcome.relaxed.append(next_label)
            agent_note(ctx, "relax", f"{rung.label!r} gave {len(rows)} of {target}. Relaxing to {next_label!r}")
    return best, best_rows


async def _people(client, ctx, outcome, rungs) -> None:
    best, rows = await _run_ladder(
        client, ctx, outcome, "/person/search", rungs, person_cost_per_result, person_rows, TARGET_PEOPLE
    )
    outcome.primary, outcome.rows = best, rows


async def run_recruiting(client: CrustdataClient, ctx: RunContext, decision: Decision, outcome: QueryOutcome) -> None:
    slots = decision.slots
    role = strip_padding(slots.target_role) or FALLBACK_ROLE
    title = await snap(client, ctx, PERSON_AUTOCOMPLETE, TITLE_FIELD, role, outcome)
    city = await snap(client, ctx, PERSON_AUTOCOMPLETE, "basic_profile.location.city", slots.target_city, outcome)
    country = await snap(
        client, ctx, PERSON_AUTOCOMPLETE, "basic_profile.location.country",
        slots.target_country or defaults.FALLBACK_COUNTRY, outcome, closed_list=True,
    )
    await _people(client, ctx, outcome, recruiting.ladder(title, city, country))


async def run_gtm(
    client: CrustdataClient, ctx: RunContext, decision: Decision, outcome: QueryOutcome, own_domain: str
) -> None:
    slots = decision.slots
    # A wrong-guess rerun arrives with little budget left, so both steps must get a share of it.
    people_cost = TARGET_PEOPLE * person_cost_per_result(gtm.buyer_ladder([own_domain])[0].body)
    reserve = min(people_cost, ctx.ledger.remaining / 2)
    industry = await snap(client, ctx, COMPANY_AUTOCOMPLETE, INDUSTRY, slots.icp_industry, outcome)
    country = await snap(
        client, ctx, COMPANY_AUTOCOMPLETE, "locations.country", slots.target_country, outcome, closed_list=True
    )
    buyer = await snap(client, ctx, PERSON_AUTOCOMPLETE, TITLE_FIELD, strip_padding(slots.buyer_title), outcome)
    if buyer:
        await _people(client, ctx, outcome, gtm.direct_ladder(buyer, industry, country))
        if len(outcome.rows) >= ENOUGH_BUYERS:
            return
    direct_primary, direct_rows = outcome.primary, outcome.rows
    best, companies = await _run_ladder(
        client, ctx, outcome, "/company/search",
        gtm.company_ladder(industry, country, own_domain),
        company_cost_per_result, company_rows, TARGET_COMPANIES,
        enough=ENOUGH_COMPANIES,
        reserve=reserve,
    )
    outcome.supporting, outcome.supporting_rows = best, companies
    domains = [c["domain"] for c in companies if c.get("domain")]
    if domains:
        await _people(client, ctx, outcome, gtm.buyer_ladder(domains, buyer))
    if len(outcome.rows) < len(direct_rows):  # The direct search was still the better answer.
        outcome.primary, outcome.rows, outcome.supporting, outcome.supporting_rows = direct_primary, direct_rows, None, []


async def run_builder(
    client: CrustdataClient, ctx: RunContext, decision: Decision, outcome: QueryOutcome, own_domain: str
) -> None:
    slots = decision.slots
    industry = await snap(client, ctx, COMPANY_AUTOCOMPLETE, INDUSTRY, slots.icp_industry, outcome)
    country = await snap(
        client, ctx, COMPANY_AUTOCOMPLETE, "locations.country", slots.target_country, outcome, closed_list=True
    )
    outcome.primary, outcome.rows = await _run_ladder(
        client, ctx, outcome, "/company/search", gtm.company_ladder(industry, country, own_domain),
        company_cost_per_result, company_rows, TARGET_PEOPLE,
    )


async def run_academic(client: CrustdataClient, ctx: RunContext, decision: Decision, outcome: QueryOutcome) -> None:
    best, rows = await _run_ladder(
        client, ctx, outcome, "/web/search/live",
        academic.ladder(decision.slots.research_field),
        lambda body: 1.0, academic.paper_rows, 5,
    )
    outcome.primary, outcome.rows = best, rows


async def run_investor(client: CrustdataClient, ctx: RunContext, decision: Decision, outcome: QueryOutcome) -> None:
    country = await snap(
        client, ctx, PERSON_AUTOCOMPLETE, "basic_profile.location.country", decision.slots.target_country, outcome,
        closed_list=True,
    )
    await _people(client, ctx, outcome, defaults.investor_ladder(country))


async def run_query(client: CrustdataClient, ctx: RunContext, decision: Decision, own_domain: str) -> QueryOutcome:
    outcome = QueryOutcome(playbook=decision.playbook, primary=None, rows=[])
    if decision.playbook is Playbook.RECRUITING:
        await run_recruiting(client, ctx, decision, outcome)
    elif decision.playbook is Playbook.GTM:
        await run_gtm(client, ctx, decision, outcome, own_domain)
    elif decision.playbook is Playbook.BUILDER:
        await run_builder(client, ctx, decision, outcome, own_domain)
    elif decision.playbook is Playbook.ACADEMIC:
        await run_academic(client, ctx, decision, outcome)
    elif decision.playbook is Playbook.INVESTOR:
        await run_investor(client, ctx, decision, outcome)
    return outcome


def pills_for(decision: Decision, outcome: QueryOutcome) -> list[dict[str, Any]]:
    """Worked out on the server when the sandbox is created, so a click sends only an ID."""
    primary = outcome.primary
    if primary is None:
        return []
    if decision.playbook is Playbook.RECRUITING:
        title = next(
            (c["value"] for c in _conditions(primary.body) if c["field"].endswith("current.title")),
            decision.slots.target_role,
        )
        pills = recruiting.pills(primary.body, title)
    elif decision.playbook is Playbook.GTM:
        pills = gtm.pills(primary.body)
    elif decision.playbook is Playbook.BUILDER:
        pills = builder.pills(primary.body)
    elif decision.playbook is Playbook.ACADEMIC:
        pills = academic.pills(primary.body)
    elif decision.playbook is Playbook.INVESTOR:
        pills = defaults.investor_pills(primary.body)
    else:
        pills = []
    return [
        {"id": p.id, "label": p.label, "hint": p.hint, "endpoint": p.endpoint, "body": p.body, "kind": p.kind}
        for p in pills[:3]
    ]


def _conditions(body: dict[str, Any]) -> list[dict[str, Any]]:
    filters = body.get("filters") or {}
    return filters.get("conditions", [filters])


def rows_for_kind(kind: str, data: Any) -> list[dict[str, Any]]:
    if kind == "authors":
        return academic.author_rows(data)
    if kind == "papers":
        return academic.paper_rows(data)
    if kind == "companies":
        return company_rows(data)
    return person_rows(data)


def pill_cost(pill: dict[str, Any]) -> float:
    if pill["endpoint"] == "/web/search/live":
        return 1.0
    if pill["endpoint"] == "/company/search":
        return company_cost_per_result(pill["body"]) * pill["body"].get("limit", TARGET_PEOPLE)
    return person_cost_per_result(pill["body"]) * pill["body"].get("limit", TARGET_PEOPLE)

