"""Checks that cost nothing. They run before any credit is spent."""

from __future__ import annotations

import re
from dataclasses import dataclass
from email.utils import parseaddr
from functools import lru_cache
from pathlib import Path

from email_validator import EmailNotValidError, validate_email

from app import db
from app.models import Status
from app.personas import VendorPersona

DATA = Path(__file__).resolve().parent.parent / "data"
ACADEMIC = re.compile(r"(\.edu|\.edu\.[a-z]{2}|\.ac\.[a-z]{2})$")


@dataclass
class Email:
    raw: str
    lookup: str  # Alias removed. This is the address we send to, so Gmail dots stay.
    dedupe_key: str  # Also has Gmail dots removed, so variants of one inbox collapse.
    local: str
    domain: str
    alias_removed: bool = False


@dataclass
class GateResult:
    status: Status
    reason: str
    detail: str = ""
    human: bool = False


def _load(name: str) -> frozenset[str]:
    lines = (DATA / name).read_text().splitlines()
    return frozenset(l.strip().lower() for l in lines if l.strip() and not l.startswith("#"))


@lru_cache
def disposable_domains() -> frozenset[str]:
    return _load("disposable_domains.txt")


@lru_cache
def freemail_domains() -> frozenset[str]:
    return _load("freemail_domains.txt")


def domain_in(domain: str, domains: frozenset[str] | set[str]) -> bool:
    """Matches the domain or any parent, so mail.mailinator.com counts as mailinator.com."""
    parts = domain.split(".")
    return any(".".join(parts[i:]) in domains for i in range(len(parts) - 1))


def is_freemail(domain: str) -> bool:
    return domain in freemail_domains()


def is_academic(domain: str) -> bool:
    return bool(ACADEMIC.search(domain))


def normalise(raw: str) -> Email | None:
    """Returns None for anything that isn't a usable address, including an empty string."""
    text = (raw or "").strip().strip("<>\"'`,;").strip()
    if text.lower().startswith("mailto:"):
        text = text[len("mailto:"):]
    if "<" in text:  # "Priya Sharma <priya@hirefast.in>"
        text = parseaddr(text)[1] or text
    try:
        parsed = validate_email(text, check_deliverability=False)
    except EmailNotValidError:
        return None

    local, domain = parsed.local_part.lower(), parsed.domain.lower()
    if domain == "googlemail.com":
        domain = "gmail.com"
    base, plus, _ = local.partition("+")
    if not base:
        return None
    dedupe_local = base.replace(".", "") if domain == "gmail.com" else base
    return Email(
        raw=raw,
        lookup=f"{base}@{domain}",
        dedupe_key=f"{dedupe_local}@{domain}",
        local=base,
        domain=domain,
        alias_removed=bool(plus),
    )


def free_gates(email: Email, signup_id: str, persona: VendorPersona) -> GateResult | None:
    """The first gate that stops this signup, or None to carry on."""
    holder = db.claim_identity(f"{persona.id}:email:{email.dedupe_key}", signup_id)
    if holder != signup_id:
        return GateResult(
            Status.HUMAN_QUEUE,
            "Repeat signup",
            f"Same inbox as an earlier signup ({email.dedupe_key}). No new proof of concept.",
            human=True,
        )
    if domain_in(email.domain, disposable_domains()):
        return GateResult(
            Status.HUMAN_QUEUE,
            "Disposable email domain",
            f"{email.domain} is on the disposable-domain list. No credits spent.",
            human=True,
        )
    if domain_in(email.domain, set(persona.own_domains)):
        return GateResult(Status.INTERNAL, f"{persona.name} team member", f"{email.domain} is an internal domain.")
    if domain_in(email.domain, set(persona.competitor_domains)):
        return GateResult(
            Status.HUMAN_QUEUE,
            "Competitor",
            f"{email.domain} is on the competitor list. No free proof of concept.",
            human=True,
        )
    return None
