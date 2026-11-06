"""The sales card. It is always stored for the dashboard; it posts only when SLACK_LIVE is on."""

from __future__ import annotations

from typing import Any

import httpx

from app.config import Settings


HEADER_LIMIT = 150  # Slack rejects the whole message past these, so trim instead.
SECTION_LIMIT = 3000


def _section(text: str) -> dict[str, Any]:
    return {"type": "section", "text": {"type": "mrkdwn", "text": text[:SECTION_LIMIT]}}


def blocks(brief: dict[str, Any]) -> list[dict[str, Any]]:
    reasons = "\n".join(
        f"• {r['claim']} _({'; '.join(c['citation'] for c in r['citations'])})_" for r in brief["reasons"]
    )
    assumptions = "\n".join(f"• {a}" for a in brief["assumptions"])
    sections = [
        {"type": "header", "text": {"type": "plain_text", "text": f"High fit: {brief['who']}"[:HEADER_LIMIT]}},
        _section(f"*Why*\n{reasons}"),
    ]
    angle = brief.get("angle")
    if angle:
        cited = "; ".join(c["citation"] for c in angle["citations"])
        sections.append(_section(f"*How to open*\n{angle['claim']} _({cited})_"))
    if brief.get("fallback"):
        sections.append(_section(f"*Basic profile only*\n{brief['fallback']}. Check the details before acting on this."))
    if brief.get("match_note"):
        sections.append(_section(f"*Identity*\n{brief['match_note']}"))
    if brief.get("credits_per_month") is not None:
        sections.append(_section(f"*Estimated {brief['credits_per_month']:,} credits per month*\n{assumptions}"))
    sections.append(_section(f"*Freshest signal*\n{brief['freshest']}"))
    return sections


class SlackNotifier:
    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.settings = settings
        self.transport = transport
        self.fail_next = 0

    async def post(self, brief: dict[str, Any]) -> tuple[str, str]:
        settings = self.settings
        if settings.uses_simulator:
            return "rendered", "Simulated data is never posted to a real channel"
        if not settings.slack_live or not settings.slack_webhook_url:
            return "rendered", "SLACK_LIVE is off, so this was rendered but not posted"
        if self.fail_next > 0:
            self.fail_next -= 1
            return "failed", "Forced failure"
        try:
            async with httpx.AsyncClient(timeout=6, transport=self.transport) as client:
                response = await client.post(settings.slack_webhook_url, json={"blocks": blocks(brief)})
        except httpx.HTTPError as exc:
            return "failed", type(exc).__name__
        if response.status_code >= 300:
            return "failed", f"Slack {response.status_code}: {response.text[:120]}"
        return "sent", ""
