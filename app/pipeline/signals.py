"""What a watched founder's new activity means, and what we say about it.

A watch notification is a list of `changes` plus the subject's current record. Code turns each
change into a fact with a citation, adds it to their user.md, then asks Claude whether it's a
strong, relevant signal. Facts are always written by code, so Claude can't invent the news it
writes about: it can only cite rows we built from Crustdata's diff.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from app.llm import STRENGTHS, LLMFailure, LLMSignal
from app.models import Company, Evidence, Person, Pick, Reason
from app.pipeline import profile_doc
from app.pipeline.email_view import SECTION_COLOURS, call_to_action
from app.personas import VendorPersona

ACTIVITY = "Recent activity"
NOTHING = "- Nothing found"
OUTPUT_DIR = Path(__file__).resolve().parents[2] / "profile_agent" / "output"

# Fields where a change, on its own, says what the company needs now. Used when Claude can't judge.
STRONG_FIELDS = {
    "funding.last_round_type", "funding.total_investment_usd", "funding.investors", "funding.acquisitions",
    "funding.milestones", "experience.employment_details.current",
}
ROUND_NAMES = {"pre_seed": "Pre-seed", "seed": "Seed", "angel": "Angel", "ipo": "IPO"}
MAX_PICKS = 3
MAX_OPENING = 400
MAX_SUBJECT = 80


@dataclass
class Judgement:
    strength: str  # strong | weak | noise
    relevant: bool
    confidence: float
    why: Reason | None
    decided_by: str  # llm | rules
    subject: str = ""
    opening: Reason | None = None
    picks: list[Pick] = field(default_factory=list)

    @property
    def worth_an_email(self) -> bool:
        return self.strength == "strong" and self.relevant and self.opening is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "strength": self.strength, "relevant": self.relevant, "confidence": self.confidence,
            "decided_by": self.decided_by, "subject": self.subject,
            "why": self.why.to_dict() if self.why else None,
            "opening": self.opening.to_dict() if self.opening else None,
            "picks": [{"service": p.service, **p.reason.to_dict()} for p in self.picks],
        }


# ---- Changes into facts ----


def _round(value: Any) -> str:
    text = str(value or "").strip()
    if text in ROUND_NAMES:
        return ROUND_NAMES[text]
    return text.replace("_", " ").title() if text else "an unnamed round"


def _money(value: Any) -> str:
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return ""
    if amount >= 1e9:
        return f"${amount / 1e9:.3g}B"
    if amount >= 1e6:
        return f"${amount / 1e6:.3g}M"
    return f"${amount:,.0f}"


def _subject_name(dataset: str, record: dict[str, Any]) -> str:
    if dataset == "company":
        return str((record.get("basic_info") or {}).get("name") or "Their company")
    return str((record.get("basic_profile") or {}).get("name") or "They")


def _clean(text: str) -> str:
    """Values come from public profiles, so they're masked and trimmed like any other fact."""
    return profile_doc.mask(re.sub(r"\s+", " ", text)).strip()[: profile_doc.MAX_FACT]


def _element(change: dict[str, Any], element: Any, who: str, source: str, date: str) -> profile_doc.Fact | None:
    name = change.get("field", "")
    if name == "news" and isinstance(element, dict):
        title = element.get("title") or element.get("headline") or ""
        url = str(element.get("article_url") or element.get("url") or "")
        when = str(element.get("published_at") or element.get("date") or "")[:10] or date
        if not title:
            return None
        if url.startswith(("https://", "http://")):
            return profile_doc.Fact(_clean(f"In the news: {title}"), "web", url, when)
        return profile_doc.Fact(_clean(f"In the news: {title}"), source, name, when)
    if name == "experience.employment_details.current" and isinstance(element, dict):
        title = element.get("title") or "a new role"
        employer = element.get("name") or element.get("company_name") or ""
        start = str(element.get("start_date") or "")[:10]
        text = f"{who} started as {title}" + (f" at {employer}" if employer else "") + (f" ({start})" if start else "")
        return profile_doc.Fact(_clean(text), source, name, date)
    if name == "funding.investors":
        return profile_doc.Fact(_clean(f"New investor: {element}"), source, name, date)
    if name == "funding.acquisitions" and isinstance(element, dict):
        target = element.get("acquired_company_name") or element.get("name") or "a company"
        return profile_doc.Fact(_clean(f"{who} acquired {target}"), source, name, date)
    label = name.rsplit(".", 1)[-1].replace("_", " ")
    return profile_doc.Fact(_clean(f"New {label}: {element}"), source, name, date)


def facts(dataset: str, changes: list[dict[str, Any]], record: dict[str, Any], date: str) -> list[profile_doc.Fact]:
    """One fact per change, cited to the watch and the field. Never written by a model."""
    source = f"watch_{dataset}"
    who = _subject_name(dataset, record)
    out: list[profile_doc.Fact] = []
    for change in changes:
        name, kind = str(change.get("field", "")), change.get("type", "")
        if kind == "added":
            for element in change.get("new_elements") or []:
                fact = _element(change, element, who, source, date)
                if fact:
                    out.append(fact)
            continue
        before, after = change.get("from"), change.get("to")
        if name == "funding.last_round_type":
            text = f"{who} raised a {_round(after)} round" + (f" (the last was {_round(before)})" if before else "")
        elif name == "funding.total_investment_usd" and _money(after):
            text = f"{who}'s total funding is now {_money(after)}" + (f", up from {_money(before)}" if _money(before) else "")
        elif name == "basic_profile.headline":
            text = f"{who} changed their headline to “{after}”"
        elif name == "basic_profile.current_title":
            text = f"{who}'s title is now {after}"
        elif name == "headcount.total":
            text = f"{who} now has {after} people" + (f", from {before}" if before is not None else "")
        elif name == "hiring.openings_count":
            text = f"{who} has {after} open roles" + (f", from {before}" if before is not None else "")
        else:
            text = f"{name} changed" + (f" from {before}" if before is not None else "") + f" to {after}"
        out.append(profile_doc.Fact(_clean(text), source, name, date))
    return out


def evidence(facts_: list[profile_doc.Fact]) -> list[Evidence]:
    return [Evidence(f"S{i}", f.source, f.field, f.text, f.date) for i, f in enumerate(facts_, start=1)]


# ---- user.md ----


def bullet(fact: profile_doc.Fact) -> str:
    if fact.source == "web":
        return f"- {fact.text} [{fact.field}]"
    cite = " · ".join(part for part in (fact.source, fact.field, fact.date) if part)
    return f"- {fact.text} [{cite}]"


def starter_user_md(email: str, person: Person, company: Company) -> str:
    """For a signup the profile agent never ran on: the same format, from what the name check found."""
    meta = {
        "schema": profile_doc.SCHEMA, "email": email, "name_given": "", "name": person.name,
        "headline": person.headline, "current_title": person.title, "current_company": company.name or person.employer,
        "company_domain": company.domain or person.employer_domain,
        "location": ", ".join(part for part in (person.city, person.country) if part),
        "linkedin_url": person.profile_url, "crustdata_person_id": str(person.person_id or ""),
        "crustdata_company_id": str(company.company_id or ""), "persona": "",
    }
    front = yaml.safe_dump(meta, sort_keys=False, allow_unicode=True)
    date = (person.updated_at or "")[:10]
    cite = " · ".join(part for part in ("person_search", "basic_profile.current_title", date) if part)
    role = f"- {person.title} at {company.name} [{cite}]" if person.title and company.name else NOTHING
    return (
        f"---\n{front}---\n\n# {person.name or email}\n\n"
        "Built from the signup's name check. The founder watch adds what changes.\n\n"
        f"Matched on: {person.match_note or person.match_method or 'the name check'}\n\n"
        f"## Current role\n{role}\n## {ACTIVITY}\n{NOTHING}\n"
    )


def add_activity(text: str, facts_: list[profile_doc.Fact]) -> str:
    """Adds each fact to Recent activity, once. Everything else in the file is left as it was."""
    lines = text.rstrip("\n").split("\n")
    heading = f"## {ACTIVITY}"
    if heading not in lines:
        later = next((i for i, line in enumerate(lines) if line.startswith("## Conflicts") or line.startswith("## Crustdata APIs")), len(lines))
        lines[later:later] = [heading, NOTHING]
    start = lines.index(heading) + 1
    end = next((i for i in range(start, len(lines)) if lines[i].startswith("## ")), len(lines))
    gap = [""] if end > start and not lines[end - 1].strip() else []  # Keep the blank line before the next heading.
    section = [line for line in lines[start:end] if line.strip() and line.strip().lower() != NOTHING.lower()]
    for fact in facts_:
        line = bullet(fact)
        if line not in section:
            section.insert(0, line)  # Newest first.
    lines[start:end] = section + gap
    return "\n".join(lines) + "\n"


def agent_file(email: str) -> Path:
    return OUTPUT_DIR / re.sub(r"[^a-z0-9@._-]+", "-", email.strip().lower()) / "user.md"


# ---- Judging ----


def evidence_from(citation: dict[str, Any]) -> Evidence:
    """Back from a stored citation: "source, field, date" is how Evidence.citation() wrote it."""
    source, _, rest = str(citation.get("citation", "")).partition(", ")
    field, _, date = rest.rpartition(", ") if rest.count(", ") else (rest, "", "")
    return Evidence(citation["id"], source, field, str(citation.get("value", "")), date)


def _cited(claim: str, ids: list[str], rows: dict[str, Evidence], limit: int = 280) -> Reason | None:
    cited = [rows[i] for i in dict.fromkeys(ids) if i in rows]
    if not claim.strip() or not cited:
        return None
    return Reason(profile_doc.mask(claim.strip())[:limit], cited)


def from_llm(output: LLMSignal, persona: VendorPersona, profile: list[Evidence], signal: list[Evidence]) -> Judgement:
    """Only what cites real rows survives. An opening that cites no new activity isn't about it."""
    rows = {e.id: e for e in [*profile, *signal]}
    opening = _cited(output.opening.claim, output.opening.evidence_ids, rows, MAX_OPENING)
    if opening is not None and not any(e.id.startswith("S") for e in opening.evidence):
        opening = None
    picks = []
    for pick in output.picks[:MAX_PICKS]:
        reason = _cited(pick.why, pick.evidence_ids, rows)
        if reason and persona.service(pick.service) and all(p.service != pick.service for p in picks):
            picks.append(Pick(pick.service, reason))
    strength = output.strength if output.strength in STRENGTHS else "noise"
    return Judgement(
        strength, output.relevant, max(0.0, min(1.0, output.confidence)),
        _cited(output.why.claim, output.why.evidence_ids, rows), "llm",
        profile_doc.mask(output.subject.strip())[:MAX_SUBJECT], opening, picks,
    )


def by_rules(signup: dict[str, Any], persona: VendorPersona, signal: list[Evidence]) -> Judgement:
    """Without Claude: a funding or role change is strong, and the email repeats only the fact itself.

    It counts as relevant only if we already matched them to services the vendor offers, and it
    reuses those cited picks rather than writing new claims. With nothing to offer, there's no email.
    """
    strong = [e for e in signal if e.field in STRONG_FIELDS]
    decision = signup.get("decision") or {}
    picks = []
    for item in decision.get("picks") or []:
        citations = [evidence_from(c) for c in item.get("citations", [])]
        if persona.service(item.get("service", "")) and citations:
            picks.append(Pick(item["service"], Reason(item["claim"], citations)))
    relevant = bool(picks)
    if not strong:
        why = Reason("Not a funding round or a new role, so it isn't worth an email on its own.", signal[:1])
        return Judgement("weak", relevant, 0.6, why, "rules")
    fact = strong[0]
    opening = Reason(f"{fact.value}. Here's where {persona.name} can help at this stage.", [fact])
    why = Reason("A funding round or a new role changes what their company needs.", [fact])
    return Judgement("strong", relevant, 0.75, why, "rules", f"Your next step with {persona.name}", opening, picks[:MAX_PICKS])


async def judge(
    llm: Any, signup: dict[str, Any], persona: VendorPersona, signal: list[Evidence]
) -> Judgement:
    profile = [Evidence(**row) for row in signup.get("evidence") or []]
    if llm.available:
        try:
            output = await llm.judge_signal(
                signup["id"], persona,
                [{"id": e.id, "source": e.source, "field": e.field, "value": e.value, "date": e.date} for e in profile],
                [{"id": e.id, "source": e.source, "field": e.field, "value": e.value, "date": e.date} for e in signal],
            )
            return from_llm(output, persona, profile, signal)
        except LLMFailure:
            pass  # Logged by the call. The rules below never say more than the fact.
    return by_rules(signup, persona, signal)


# ---- The email ----


def email_context(
    judgement: Judgement, persona: VendorPersona, stop_url: str,
    starter: dict[str, Any] | None = None, sandbox_url: str = "",
) -> dict[str, Any]:
    rows = []
    for pick in judgement.picks:
        service = persona.service(pick.service)
        if service:  # The name and link come from the vendor's list, never from Claude.
            rows.append({"a": service.name, "b": pick.reason.claim, "url": service.url})
    sources: list[str] = []
    for reason in [judgement.opening, *(p.reason for p in judgement.picks)]:
        for item in reason.evidence if reason else []:
            if item.source == "web" or item.field == "news":
                plain = "public news"
            elif item.source.startswith("company") or item.source == "watch_company":
                plain = "public company data"
            else:
                plain = "your public work profile"
            if plain not in sources:
                sources.append(plain)
    return {
        "vendor": persona.name,
        "sample": persona.sample,
        "hero": {
            "title": judgement.subject or f"Your next step with {persona.name}",
            "line": judgement.opening.claim if judgement.opening else "",
        },
        "sections": [{"title": row["a"], "why": row["b"], "url": row["url"], "color": SECTION_COLOURS["note"], "code": ""} for row in rows],
        "cite": " · ".join(sources),
        "cta": call_to_action(persona, starter, sandbox_url),
        "unsubscribe_url": stop_url,
    }


def today() -> str:
    return dt.date.today().isoformat()

