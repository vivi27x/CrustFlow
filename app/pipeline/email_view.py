"""Turns a decision into the data Sonali's email templates expect, plus the styles they share."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from app.models import Decision, Evidence, Pick
from app.personas import VendorPersona
from app.pipeline import highlight

ACCENT = "#5546e8"
INK = "#0f1020"
MUTED = "#6b6e7b"
BUTTON = "display:inline-block;margin:0 8px 8px 0;padding:11px 18px;border-radius:8px;font-size:14px;font-weight:600;text-decoration:none;"

# Inline styles, because email clients drop stylesheets. `_ui.html` reads these as S.<name>.
S = {
    "accent": ACCENT,
    "h1": f"margin:0 0 8px;font-size:24px;line-height:1.25;font-weight:700;color:{INK};",
    "h2": f"margin:24px 0 8px;font-size:15px;font-weight:700;color:{INK};",
    "lead": "margin:0 0 20px;font-size:16px;line-height:1.55;color:#33354a;",
    "text": f"margin:0 0 12px;font-size:14px;line-height:1.5;color:{INK};",
    "label": f"margin:0 0 10px;font-size:11px;font-weight:700;letter-spacing:0.6px;text-transform:uppercase;color:{ACCENT};",
    "small": f"font-size:12px;line-height:1.5;color:{MUTED};",
    "muted": f"color:{MUTED};",
    "faint": "color:#8a8d99;",
    "mono": "font-family:SFMono-Regular,Menlo,Consolas,monospace;",
    "code": "margin:0 0 14px;padding:12px;background:#0f1020;color:#f3f2ee;border-radius:8px;font-size:12px;line-height:1.45;white-space:pre-wrap;word-break:break-word;",
    "prompt": f"margin:0 0 12px;padding:12px 14px;background:#f6f5fe;border-radius:8px;font-size:14px;line-height:1.5;color:{INK};",
    "btn": f"{BUTTON}background:{ACCENT};color:#ffffff;",
    "btn_outline": f"{BUTTON}background:#ffffff;color:{ACCENT};border:1px solid #d9d5fb;",
}


# Which part of a starter each service explains.
BLOCK_FOR = {
    "database": "table", "vector": "vector", "auth": "policy", "compliance": "policy", "realtime": "realtime",
    "queues": "queues", "cron": "cron",
}
SECTION_COLOURS = {
    "table": "#a5b4fc", "vector": "#7dd3fc", "policy": "#86efac", "realtime": "#f9a8d4", "queues": "#fcd34d",
    "cron": "#fdba74", "note": "#c4b5fd", "seed": "#8a8fa8",
}


def _service_row(pick: Pick, persona: VendorPersona) -> dict[str, Any] | None:
    """The name and link come from the vendor's own list, so Claude can't invent a feature or a URL."""
    service = persona.service(pick.service)
    if service is None:
        return None
    return {"a": service.name, "b": pick.reason.claim, "url": service.url}


def _plain_source(evidence: Evidence) -> str:
    """A reader wants to know where a claim came from. The dashboard keeps the exact field and date."""
    if evidence.source == "web":
        return urlparse(evidence.field).netloc.removeprefix("www.") or "the web"
    if evidence.source.startswith("company"):
        return "public company data"
    return "your public work profile"


def citations(decision: Decision) -> str:
    """Every source behind the email, once each."""
    sources: list[str] = []
    for reason in [decision.opening, *(pick.reason for pick in decision.picks)]:
        for evidence in reason.evidence if reason else []:
            if _plain_source(evidence) not in sources:
                sources.append(_plain_source(evidence))
    return " · ".join(sources)


def call_to_action(persona: VendorPersona, starter: dict[str, Any] | None, sandbox_url: str) -> list[dict[str, str]]:
    """With a starter, the next step is running it. Otherwise it's the vendor's own way in."""
    if starter and sandbox_url:
        company = starter.get("company")
        label = f"Run {company}'s starter in your browser" if company else "Run your starter in your browser"
        return [{"label": label, "url": sandbox_url, "primary": "1"}]
    return [{"label": persona.cta_label or "Get started", "url": persona.cta_url, "primary": "1"}] if persona.cta_url else []


def _section(block: dict[str, str], title: str, why: str, url: str = "", note: str = "") -> dict[str, Any]:
    return {
        "title": title, "why": why, "url": url, "note": note, "color": SECTION_COLOURS[block["id"]],
        "code": highlight.sql(block["code"]) if block["code"] else "",
    }


def blueprint(decision: Decision, persona: VendorPersona, starter: dict[str, Any]) -> list[dict[str, Any]]:
    """Every pick sits on the lines it adds, so the code explains itself.

    Sections follow the order the SQL runs in, not the order Claude ranked the picks. Names and links
    come from the vendor's list. A pick that adds no SQL still appears, with its reason and no code.
    """
    placed: dict[str, Pick] = {}
    unplaced: list[Pick] = []
    for pick in decision.picks:
        block = BLOCK_FOR.get(pick.service)
        if block and block not in placed:
            placed[block] = pick
        else:
            unplaced.append(pick)

    def explained(block: dict[str, str], title: str, why: str, note: str = "") -> dict[str, Any]:
        pick = placed.get(block["id"])
        service = persona.service(pick.service) if pick else None
        return _section(block, service.name, pick.reason.claim, service.url, note) if pick and service else _section(block, title, why, note=note)

    company = starter.get("company")
    named = f", named for {company}." if company else "."
    needs_project = f"Needs a {persona.name} project, so the browser sandbox skips this part."
    sections = []
    for block in starter["blocks"]:
        if block["id"] == "table":
            sections.append(explained(block, "Your table", f"{starter['purpose'].rstrip('.')}{named}"))
        elif block["id"] == "policy":
            sections.append(explained(block, "Row Level Security", "On every starter, so each user only sees their own rows."))
        elif block["id"] != "seed":
            sections.append(explained(block, block["id"].title(), ""))
    for pick in unplaced:
        service = persona.service(pick.service)
        if service:
            sections.append(_section({"id": "note", "code": ""}, service.name, pick.reason.claim, service.url))
    sections += [explained(block, block["id"].title(), "", needs_project) for block in starter.get("project_blocks", [])]
    seed = next((block for block in starter["blocks"] if block["id"] == "seed"), None)
    if seed:
        sections.append(_section(seed, "Sample rows", "Invented, so you have something to query straight away."))
    return sections


def _starter_context(decision: Decision, persona: VendorPersona, starter: dict[str, Any] | None) -> dict[str, Any] | None:
    if not starter:
        return None
    mcp = persona.service("mcp")
    return {
        "file": f"{starter['table']}.sql",
        "sections": blueprint(decision, persona, starter),
        "prompt": starter["mcp_prompt"],
        "mcp_url": mcp.url if mcp else "",
    }


def feature_match(
    decision: Decision, persona: VendorPersona, intent_url: str,
    starter: dict[str, Any] | None = None, sandbox_url: str = "",
) -> dict[str, Any]:
    """The email for a company with no search to run: what fits this person, and why."""
    segment = persona.segment(decision.segment)
    fallback = segment.first_value if segment else ""
    rows = [row for row in (_service_row(pick, persona) for pick in decision.picks) if row]
    return {
        "vendor": persona.name,
        "sample": persona.sample,
        "hero": {
            "title": decision.subject or f"Your best first step with {persona.name}",
            "line": decision.opening.claim if decision.opening else fallback,
        },
        "card": {
            "title": "Where we'd start",
            "sub": f"Chosen for your work from everything {persona.name} offers.",
            "rows": rows,
            "footer": f"See everything {persona.name} offers",
            "footer_url": persona.cta_url,
        } if rows else None,
        "cite": citations(decision),
        "cta": call_to_action(persona, starter, sandbox_url),
        "starter": _starter_context(decision, persona, starter),
        "sandbox_url": sandbox_url,
        "intent_url": intent_url,
    }
