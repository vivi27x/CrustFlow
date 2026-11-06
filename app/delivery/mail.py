"""Email goes out only when EMAIL_LIVE is on and the address is allowlisted. The dashboard always shows it."""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from app.config import Settings

RESEND_URL = "https://api.resend.com/emails"


@dataclass
class SendResult:
    status: str  # sent | rendered | failed
    detail: str = ""


class Mailer:
    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.settings = settings
        self.transport = transport
        self.fail_next = 0

    async def send(self, to: str, subject: str, html: str) -> SendResult:
        settings = self.settings
        if settings.uses_simulator:
            return SendResult("rendered", "Simulated data is never sent to a real inbox")
        if not settings.email_live:
            return SendResult("rendered", "EMAIL_LIVE is off, so this was rendered but not sent")
        if not settings.email_api_key or not settings.email_from:
            return SendResult("rendered", "No email key or sender configured")
        recipient = settings.email_redirect_to.strip() or to
        redirected = recipient.lower() != to.lower()
        allowlist = settings.allowlist
        if "*" not in allowlist and recipient.lower() not in allowlist:
            return SendResult("rendered", f"{recipient} is not on EMAIL_ALLOWLIST")
        if redirected:
            subject = f"[Test: {to}] {subject}"
        if self.fail_next > 0:
            self.fail_next -= 1
            return SendResult("failed", "Forced failure")
        try:
            async with httpx.AsyncClient(timeout=8, transport=self.transport) as client:
                response = await client.post(
                    RESEND_URL,
                    headers={"Authorization": f"Bearer {settings.email_api_key}"},
                    json={"from": settings.email_from, "to": [recipient], "subject": subject, "html": html},
                )
        except httpx.HTTPError as exc:
            return SendResult("failed", f"{type(exc).__name__}")
        if response.status_code >= 300:
            return SendResult("failed", f"Resend {response.status_code}: {response.text[:160]}")
        sent_id = response.json().get("id", "")
        return SendResult("sent", f"{sent_id}, redirected to {recipient}" if redirected else sent_id)
