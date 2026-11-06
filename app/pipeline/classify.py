"""Chooses a playbook. Rules go first where a wrong answer would be costly."""

from __future__ import annotations

import re

from app import db, events
from app.config import Settings
from app.llm import LLM, LLMFailure, LLMOutput, LLMReason
from app.models import Company, Decision, Evidence, EvidenceTable, Person, Pick, Playbook, Reason, Slots
from app.personas import VendorPersona

STUDENT = re.compile(r"\b(student|undergrad|undergraduate|b\.?\s?tech|m\.?\s?tech|fresher|sophomore|freshman)\b", re.I)
ACADEMIC = re.compile(
    r"\b(professor|prof\.?|faculty|lecturer|researcher|research (scientist|fellow|scholar|associate)|postdoc|"
    r"post-doctoral|phd|ph\.d|doctoral|scientist)\b",
    re.I,
)
RECRUITING = re.compile(r"\b(talent|recruit\w*|sourcer|sourcing|staffing|human resources|people ops|people operations|hr)\b", re.I)
GTM = re.compile(
    r"\b(founder|co-?founder|ceo|growth|gtm|go-to-market|revops|revenue|sales|sdr|bdr|marketing|demand gen|"
    r"business development|partnerships)\b",
    re.I,
)
CONTACT_DETAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+|\+?\d[\d\s().-]{7,}\d")
MAX_OPENING = 400
MAX_REASON = 280
MAX_SUBJECT = 80
MAX_PICKS = 3
RECRUITER_ROLE = re.compile(r"\b(recruit\w*|talent|sourcer|hr|human resources)\b", re.I)


def is_student(person: Person) -> bool:
    text = f"{person.headline} {person.title}"
    # "PhD student" stays with the researchers: only true students get the zero-credit guide.
    return bool(STUDENT.search(text)) and not ACADEMIC.search(text)


def is_researcher(person: Person) -> bool:
    return bool(ACADEMIC.search(f"{person.headline} {person.title}"))


def first_evidence(table: EvidenceTable, field: str) -> Evidence | None:
    return next((e for e in table.items if e.field == field), None)


def _location_parts(location: str) -> tuple[str, str]:
    """Splits 'Bengaluru, Karnataka, India' into city and country. A lone 'Bengaluru' is a city.

    The country is only a guess ('San Francisco, CA' gives 'CA'). Snapping drops it if it isn't one.
    """
    parts = [p.strip() for p in location.split(",") if p.strip()]
    if not parts:
        return "", ""
    city = "" if parts[0].lower() in ("remote", "hybrid") else parts[0]
    return city, parts[-1] if len(parts) >= 2 else ""


def default_slots(person: Person, company: Company) -> Slots:
    role, city, country = "", person.city, person.country
    for job in company.jobs:
        if job["title"] and not RECRUITER_ROLE.search(job["title"]):
            role = job["title"]
            job_city, job_country = _location_parts(job["location"])
            city, country = job_city or city, job_country or country
            break
    industry = company.industries[0] if company.industries else ""
    return Slots(
        target_role=role,
        target_city=city,
        target_country=country or company.country,
        icp_industry=industry,
        research_field=research_field_from(person),
    )


ROLE_WORDS = re.compile(
    r"\b(associate|assistant|adjunct|visiting|professor|prof|faculty|lecturer|researcher|research|scientist|fellow|"
    r"scholar|postdoc|phd|ph\.d|candidate|student|head|dean|director)\b\.?",
    re.I,
)
PLACE_WORDS = re.compile(r"\b(university|institute|college|school|iit|nit|iisc|iiit|mit|department|dept|lab)\b", re.I)


def research_field_from(person: Person) -> str:
    """Pulls a field from a headline, keeping its words whole. A clipped field gives a worse first search."""
    candidates = []
    for segment in re.split(r"[,|;/]| - ", person.headline):
        cleaned = ROLE_WORDS.sub(" ", segment)
        cleaned = re.sub(r"^\W*(of|in|on|at)\b", " ", cleaned.strip(), flags=re.I)
        cleaned = re.sub(r"^\W*(of|in|on|at)\b", " ", cleaned.strip(), flags=re.I)
        cleaned = " ".join(cleaned.split())
        if cleaned and not PLACE_WORDS.search(cleaned) and " at " not in f" {cleaned.lower()} ":
            candidates.append(cleaned)
    # Researchers tend to list their department first and their specific field after it.
    return candidates[-1] if candidates else person.department


def decide_by_rules(person: Person, company: Company, table: EvidenceTable, *, decided_by: str) -> Decision:
    """Used when Claude is unavailable, so it only answers on clear signals."""
    title_evidence = first_evidence(table, "basic_profile.current_title")
    headline_evidence = first_evidence(table, "basic_profile.headline")
    profile = title_evidence or headline_evidence
    profile_text = f"{person.title} {person.headline}"
    industry_evidence = next((e for e in table.items if e.field.endswith("industries")), None)
    jobs = [e for e in table.items if e.source == "job/search"]
    recruiter_jobs = [e for e in jobs if RECRUITER_ROLE.search(e.value)]
    slots = default_slots(person, company)

    if profile and RECRUITING.search(profile_text):
        reasons = [Reason("Your work centres on hiring.", [profile])]
        if recruiter_jobs:
            reasons.append(Reason(f"You have {len(recruiter_jobs)} recruiting roles open, so sourcing is a live need.", recruiter_jobs[:3]))
        open_role = next((e for e in jobs if slots.target_role and e.value.startswith(slots.target_role)), None)
        if open_role:
            reasons.append(Reason(f"You are hiring for {slots.target_role} right now.", [open_role]))
        return Decision(Playbook.RECRUITING, 0.75, reasons, slots, decided_by)

    if len(recruiter_jobs) >= 2:
        return Decision(
            Playbook.RECRUITING, 0.65,
            [Reason(f"You have {len(recruiter_jobs)} recruiting roles open.", recruiter_jobs[:3])], slots, decided_by,
        )

    if industry_evidence and RECRUITING.search(industry_evidence.value):
        return Decision(Playbook.RECRUITING, 0.65, [Reason("Your company works in recruiting.", [industry_evidence])], slots, decided_by)

    if profile and GTM.search(profile_text):
        reasons = [Reason("You own revenue, so finding buyers matters most.", [profile])]
        if industry_evidence:
            reasons.append(Reason(f"You sell into {slots.icp_industry}.", [industry_evidence]))
        return Decision(Playbook.GTM, 0.7, reasons, slots, decided_by)

    return Decision(Playbook.UNKNOWN, 0.3, [], slots, decided_by)


def _clean(value: str) -> str:
    """For short filter values. Reasons get `_sentence`, or they stop mid-sentence."""
    return " ".join(value.split())[:80]


def _sentence(value: str) -> str:
    return " ".join(value.split())[:MAX_REASON]


def _backed(line: LLMReason, table: EvidenceTable) -> Reason | None:
    """An opening or sales angle goes out under our name, so it needs real rows and no contact details."""
    text = " ".join(line.claim.split())
    cited = [item for item in (table.get(i) for i in line.evidence_ids) if item is not None]
    if not text or len(text) > MAX_OPENING or CONTACT_DETAIL.search(text):
        return None
    return Reason(text, cited) if cited and len(cited) == len(line.evidence_ids) else None


def _picks(output: LLMOutput, table: EvidenceTable, persona: VendorPersona) -> list[Pick]:
    """Keeps services the vendor really offers, each backed by real rows."""
    picks: list[Pick] = []
    for pick in output.picks:
        text = _sentence(pick.why)
        cited = [item for item in (table.get(i) for i in pick.evidence_ids) if item is not None]
        known = persona.service(pick.service) is not None
        repeated = any(p.service == pick.service for p in picks)
        if known and not repeated and text and cited and len(cited) == len(pick.evidence_ids) and not CONTACT_DETAIL.search(text):
            picks.append(Pick(pick.service, Reason(text, cited)))
    return picks[:MAX_PICKS]


def _subject(value: str) -> str:
    text = " ".join(value.split())
    return text if len(text) <= MAX_SUBJECT and not CONTACT_DETAIL.search(text) else ""


def _from_llm(
    output: LLMOutput, table: EvidenceTable, person: Person, company: Company, persona: VendorPersona
) -> Decision | None:
    """Keeps only reasons backed by real evidence. No reasons left means no decision."""
    reasons: list[Reason] = []
    for reason in output.reasons:
        cited = [item for item in (table.get(i) for i in reason.evidence_ids) if item is not None]
        if cited and len(cited) == len(reason.evidence_ids):
            reasons.append(Reason(_sentence(reason.claim), cited))
    segment = persona.segment(output.segment)
    playbook = segment.playbook if segment else Playbook.UNKNOWN
    if not reasons and playbook is not Playbook.UNKNOWN:
        return None

    fallback = default_slots(person, company)
    slots = Slots(
        target_role=_clean(output.slots.target_role) or fallback.target_role,
        target_city=_clean(output.slots.target_city) or fallback.target_city,
        target_country=_clean(output.slots.target_country) or fallback.target_country,
        icp_industry=_clean(output.slots.icp_industry) or fallback.icp_industry,
        research_field=_clean(output.slots.research_field) or fallback.research_field,
        buyer_title=_clean(output.slots.buyer_title),
    )
    if RECRUITER_ROLE.search(slots.target_role):
        slots.target_role = fallback.target_role
    confidence = min(1.0, max(0.0, output.confidence))
    return Decision(
        playbook, confidence, reasons, slots, "llm", segment=output.segment, opening=_backed(output.opening, table),
        subject=_subject(output.subject), picks=_picks(output, table, persona), starter=output.starter,
        sales_angle=_backed(output.sales_angle, table),
    )


async def classify(
    llm: LLM,
    settings: Settings,
    signup_id: str,
    person: Person,
    company: Company,
    table: EvidenceTable,
    domain: str,
    persona: VendorPersona,
    *,
    forced: Playbook | None = None,
) -> Decision:
    """`forced` means rules already chose: the model only fills slots and writes reasons."""
    decision: Decision | None = None
    if llm.available:
        try:
            output = await llm.classify(signup_id, table, domain, persona)
            decision = _from_llm(output, table, person, company, persona)
            if decision is None:
                events.publish("call", db.log_call(
                    signup_id=signup_id, kind="agent", endpoint="citations",
                    note="Model reasons cited no real evidence, so they were dropped",
                ))
        except LLMFailure:
            decision = None

    if decision is None:
        decided_by = "rules-fallback" if llm.available else "rules"
        if forced is None:
            decision = decide_by_rules(person, company, table, decided_by=decided_by)
            if persona.has(decision.playbook.value):
                decision.segment = decision.playbook.value
            else:  # Our rules only know Crustdata's segments, so for anyone else we ask.
                decision = Decision(Playbook.UNKNOWN, 0.3, [], decision.slots, decided_by)
        else:
            decision = Decision(forced, 0.75, [], default_slots(person, company), decided_by)
    if forced is not None:
        decision.playbook = forced
        decision.segment = forced.value
        decision.confidence = max(decision.confidence, settings.classify_confidence_min)
        if not decision.reasons:
            profile = first_evidence(table, "basic_profile.headline") or first_evidence(table, "basic_profile.current_title")
            if profile:
                decision.reasons = [Reason("You do research, so papers in your field are the most useful first result.", [profile])]
    return decision
