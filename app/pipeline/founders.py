"""Who is worth watching: founders and CEOs of startups, matched with confidence.

Rules only. A watch commits the account to spend on every change, so a guess must never start one.
Each reason cites the field it came from, like everything else shown to a person.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.config import Settings
from app.models import Company, Evidence, Person, Reason
from app.pipeline.route import HONORARY_ROLE

FOUNDER = re.compile(r"\b(co-?\s?founder|founder|ceo|chief executive(?: officer)?)\b", re.I)
# Roles that mention founders without being one.
NOT_FOUNDER = re.compile(
    r"\b(founder'?s office|founders'? (?:fund|program|programme|office|network)|(?:founder|entrepreneur) in residence|"
    r"for founders|helping founders|chief of staff|(?:ea|assistant|associate) to (?:the )?(?:ceo|founder))\b",
    re.I,
)
NOT_A_STARTUP = {"public company", "educational", "government agency"}
LATE_STAGE_ROUNDS = ("series_d", "series_e", "series_f", "ipo", "post_ipo_equity", "private_equity")


@dataclass
class Founder:
    role: str
    reasons: list[Reason] = field(default_factory=list)
    watch_company: bool = False

    def to_dict(self) -> dict[str, object]:
        return {"role": self.role, "watch_company": self.watch_company, "reasons": [r.to_dict() for r in self.reasons]}


def founder_role(person: Person) -> tuple[str, str]:
    """The title, or the part of the headline, that says they founded or run something. And its field."""
    if person.title and FOUNDER.search(person.title) and not NOT_FOUNDER.search(person.title):
        return person.title, "basic_profile.current_title"
    for part in re.split(r"\s*[|·•,;]\s*|\s+-\s+", person.headline):
        if FOUNDER.search(part) and not NOT_FOUNDER.search(part) and not HONORARY_ROLE.search(part):
            return part.strip(), "basic_profile.headline"
    return "", ""


def startup_reason(settings: Settings, company: Company) -> tuple[bool, Evidence | None]:
    """Is their company a startup? Unknown size is fine: we watch the founder, not the company."""
    if not company.matched:
        return True, None
    if company.company_type.strip().lower() in NOT_A_STARTUP:
        return False, None
    if company.funding_round.lower() in LATE_STAGE_ROUNDS:
        return False, None
    if company.headcount is None:
        return True, None
    if company.headcount > settings.watch_startup_max_headcount:
        return False, None
    return True, Evidence("H2", "company/enrich", "headcount.total", f"{company.headcount} people")


def identify(settings: Settings, person: Person, company: Company) -> Founder | None:
    """None unless we're sure who they are, and they founded or run a startup."""
    if not person.matched or person.confidence < settings.watch_confidence_min:
        return None
    if not person.profile_url.lower().startswith(("https://", "http://")):
        return None  # A person watch needs their profile URL.
    role, role_field = founder_role(person)
    if not role:
        return None
    startup, size = startup_reason(settings, company)
    if not startup:
        return None
    source = "profile report" if person.match_method == "report" else "person/search"
    proof = Evidence("H1", source, role_field, role, (person.updated_at or "")[:10])
    where = f" at {company.name}" if company.name else ""
    reasons = [Reason(f"{role}{where}", [proof])]
    if size is not None:
        reasons.append(Reason(f"{company.name} is a startup-sized company", [size]))
    watch_company = company.matched and bool(company.company_id or company.domain)
    return Founder(role, reasons, watch_company)
