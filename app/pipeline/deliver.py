"""Renders what the signup receives, from the exact requests we sent."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from app.config import Settings
from app.delivery import snippets
from app.pipeline.email_view import S, feature_match
from app.personas import VendorPersona
from app.models import Decision, Playbook, QueryOutcome

TEMPLATES = Path(__file__).resolve().parent.parent / "templates"
env = Environment(loader=FileSystemLoader(TEMPLATES), autoescape=select_autoescape(["html"]))
env.globals["S"] = S

PUBLIC_ROW_KEYS = ("name", "title", "company", "url")
COLUMNS = {
    "people": ("Name", "Title", "Company"),
    "papers": ("Paper", "Authors", "Citations"),
    "authors": ("Researcher", "Interests", "Affiliation"),
    "companies": ("Company", "Size", "Country"),
}


def public_row(row: dict[str, Any]) -> dict[str, str]:
    """The only fields a signup ever sees: names, titles, companies and profile links."""
    clean = {key: str(row.get(key) or "") for key in PUBLIC_ROW_KEYS}
    if not clean["url"].lower().startswith(("https://", "http://")):
        clean["url"] = ""  # Profile data is external. A javascript: link must never reach an href.
    return clean


def row_kind(decision: Decision) -> str:
    return {Playbook.ACADEMIC: "papers", Playbook.BUILDER: "companies"}.get(decision.playbook, "people")


def table_rows(kind: str, rows: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Maps rows onto the three columns. Papers lead with the title, not the author."""
    out = []
    for row in rows:
        clean = public_row(row)
        if kind == "papers":
            out.append({"a": clean["title"], "b": clean["name"], "c": clean["company"], "url": clean["url"]})
        else:
            out.append({"a": clean["name"], "b": clean["title"], "c": clean["company"], "url": clean["url"]})
    return out


def headline(decision: Decision, outcome: QueryOutcome) -> str:
    count = len(outcome.rows)
    label = outcome.primary.label if outcome.primary else ""
    if decision.playbook is Playbook.GTM and outcome.supporting_rows:
        companies = f"{count} buyers at {len(outcome.supporting_rows)} companies"
        dropped_market = outcome.supporting and "any industry" in outcome.supporting.label
        guessed_market = decision.decided_by == "intent"  # A button says what they do, not who they sell to.
        return companies if dropped_market or guessed_market else f"{companies} that fit your market"
    if decision.playbook is Playbook.BUILDER:
        return f"{count} real companies to try the API on"
    if decision.playbook is Playbook.ACADEMIC:
        return f"{count} papers for your research"
    if decision.playbook is Playbook.INVESTOR:
        return f"{count} founders to look at first"
    return f"{count} matches: {label}"


def code_blocks(outcome: QueryOutcome) -> list[dict[str, str]]:
    blocks = []
    for label, attempt in (("Step 1: find the companies", outcome.supporting), ("The search we ran", outcome.primary)):
        if attempt is None:
            continue
        title = label if outcome.supporting else "The search we ran"
        blocks.append({
            "label": "Step 2: find the buyers" if outcome.supporting and attempt is outcome.primary else title,
            "curl": snippets.curl(attempt.endpoint, attempt.body),
            "python": snippets.python(attempt.endpoint, attempt.body),
        })
    return blocks


def relaxed_note(outcome: QueryOutcome) -> str:
    """Says what we actually searched for when the strictest search came up short."""
    if not outcome.relaxed:
        return ""
    labels = [a.label for a in (outcome.supporting, outcome.primary) if a is not None]
    return f"The strictest search found too few, so these come from a wider one: {'; '.join(labels)}."


def render_message(
    settings: Settings, decision: Decision, persona: VendorPersona, intent_token: str,
    starter: dict[str, Any] | None = None, sandbox_token: str = "",
) -> tuple[str, str]:
    sandbox_url = f"{settings.app_base_url}/sandbox/{sandbox_token}" if sandbox_token else ""
    context = feature_match(decision, persona, f"{settings.app_base_url}/intent/{intent_token}", starter, sandbox_url)
    return context["hero"]["title"], env.get_template("email/t7_feature_match.html").render(**context)


def render_results(
    settings: Settings, decision: Decision, outcome: QueryOutcome, sandbox_token: str, intent_token: str
) -> tuple[str, str]:
    subject = headline(decision, outcome)
    kind = row_kind(decision)
    html = env.get_template("email/results.html").render(
        headline=subject,
        columns=COLUMNS[kind],
        rows=table_rows(kind, outcome.rows),
        companies=outcome.supporting_rows,
        relaxed=relaxed_note(outcome),
        searched=outcome.primary.label if outcome.primary else "",
        opening=decision.opening.to_dict() if decision.opening else None,
        reasons=[r.to_dict() for r in decision.reasons],
        code=code_blocks(outcome),
        sandbox_url=f"{settings.app_base_url}/sandbox/{sandbox_token}",
        intent_url=f"{settings.app_base_url}/intent/{intent_token}",
    )
    return subject, html


def render_intent(settings: Settings, intent_token: str, persona: VendorPersona) -> tuple[str, str]:
    subject = f"What are you building with {persona.name}?"
    base = f"{settings.app_base_url}/intent/{intent_token}"
    line = (
        f"Pick one and we'll run your first {persona.name} search for you, with the results and the code."
        if persona.runs_searches else f"Pick one and we'll show you the best way to start with {persona.name}."
    )
    options = [
        {"label": s.label, "hint": s.hint, "url": f"{base}?choice={s.id}"} for s in persona.segments if s.button
    ]
    html = env.get_template("email/t6_ask_intent.html").render(
        vendor=persona.name, sample=persona.sample, line=line, options=options
    )
    return subject, html


def render_student(settings: Settings, intent_token: str) -> tuple[str, str]:
    subject = "Start with Crustdata's free endpoints"
    html = env.get_template("email/student.html").render(
        identify_curl=snippets.curl("/company/identify", {"domains": ["stripe.com"]}),
        autocomplete_curl=snippets.curl(
            "/person/search/autocomplete",
            {"field": "experience.employment_details.current.title", "query": "Data Sci", "limit": 5},
        ),
        intent_url=f"{settings.app_base_url}/intent/{intent_token}",
    )
    return subject, html


def render_internal() -> tuple[str, str]:
    subject = "You work here"
    return subject, env.get_template("email/internal.html").render()
