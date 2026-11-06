"""The agent, start to finish. Whatever goes wrong, a signup ends in a safe, visible state."""

from __future__ import annotations

import asyncio
import dataclasses
import datetime as dt
import importlib
import logging
import secrets
import time
from typing import Any

from app import db, events
from app.crustdata.client import BudgetExceeded, CrustdataError, Ledger, RunContext
from app.models import (
    Company,
    Decision,
    Evidence,
    Person,
    Playbook,
    QueryOutcome,
    Reason,
    Status,
    dig,
)
from app.pipeline import classify as classify_module
from app.personas import Segment, VendorPersona
from app.pipeline import deliver, gates, profile_doc, profiles, query, route, starter, watcher
from app.pipeline.classify import default_slots, is_researcher, is_student
from app.pipeline.context import add_context, build_evidence
from app.playbooks.defaults import FALLBACK_COUNTRY
from app.services import Services

log = logging.getLogger("crustflow")

MAX_INTENT_RUNS = 2
FALLBACK_INDUSTRY = "Software Development"


def persona_of(s: Services, signup: dict[str, Any]) -> VendorPersona:
    return s.personas.get(signup.get("persona_id") or "") or s.personas[s.settings.vendor_persona]


def agent_note(ctx: RunContext, endpoint: str, note: str) -> None:
    agent_note_for(ctx.signup_id, endpoint, note)


def agent_note_for(signup_id: str, endpoint: str, note: str) -> None:
    events.publish("call", db.log_call(signup_id=signup_id, kind="agent", endpoint=endpoint, note=note))


def charge_for_report(ctx: RunContext, report: profile_doc.Report, spent_before: float) -> None:
    """The loop may call Crustdata itself. Whatever our client didn't see still counts."""
    unseen = round(report.credits - (ctx.ledger.spent - spent_before), 4)
    if unseen > 0:
        ctx.ledger.charge(unseen)
        events.publish("call", db.log_call(
            signup_id=ctx.signup_id, kind="crustdata", endpoint="profile report", status=200, credits=unseen,
            note=f"credits the profile agent spent: {', '.join(report.tools) or 'tools not listed'}",
        ))


def mark_fallback(ctx: RunContext, why: str) -> None:
    """Falling back is safe but thinner, so people need to see it happened, and why."""
    db.update_signup(ctx.signup_id, fallback_reason=why)
    signup = db.get_signup(ctx.signup_id) if ctx.signup_id else None
    if signup:
        events.publish("signup", summary(signup))
    events.publish("call", db.log_call(
        signup_id=ctx.signup_id, kind="fallback", endpoint="fallback to basic profile", note=f"{why}, so the basic builder took over"[:160],
    ))


def why_no_report(exc: Exception, limit: float) -> str:
    if isinstance(exc, TimeoutError):
        return f"the profile agent ran out of time ({limit:.0f} s)"
    if isinstance(exc, ImportError):
        return "the profile agent isn't available"
    if isinstance(exc, profile_doc.ReportError):
        return f"its report couldn't be used: {exc}"
    return f"the profile agent failed: {type(exc).__name__}: {exc}"


async def run_agent(
    s: Services, ctx: RunContext, email: gates.Email, name: str, persona: VendorPersona, key: str
) -> profile_doc.Report:
    ahead = s.reports_in_progress - 1
    if ahead:
        agent_note(ctx, "profile report", f"waiting for the profile agent, {ahead} ahead")
    async with s.agent_turn:
        spent_before = ctx.ledger.spent
        agent_note(ctx, "profile report", "profile agent started")
        builder = importlib.import_module("app.pipeline.react_profiles")
        async with asyncio.timeout(s.settings.profile_timeout_s):  # Starts here, so queuing costs nothing.
            raw = await builder.build_report(s.client, ctx, email.lookup, name, persona)
    report = profile_doc.parse(raw, email.lookup)
    charge_for_report(ctx, report, spent_before)
    db.cache_set(key, raw)
    agent_note(ctx, "profile report", f"report {'resolved' if report.resolved else 'unresolved'}: {report.matched_on or 'no match line'}"[:160])
    return report


async def read_report(
    s: Services, ctx: RunContext, email: gates.Email, name: str, persona: VendorPersona, *, saved_only: bool = False
) -> profile_doc.Report | None:
    """Gets the loop's report. Any failure is flagged and returns None, and the basic builder carries on.

    Reports are cached, so a rerun is instant and never pays twice. That also lets a demo use a
    report that was built ahead of time. `saved_only` means we know who this is, so a saved report
    is welcome but it isn't worth minutes of agent time.
    """
    key = f"report:{email.dedupe_key}"  # The research doesn't change per vendor.
    cached = db.cache_get(key)
    if cached is not None:
        agent_note(ctx, "profile report", "used a saved report, 0 credits")
        return profile_doc.parse(cached, email.lookup)
    if saved_only:
        agent_note(ctx, "profile report", "fast path: the name check found them, so the agent isn't needed")
        return None
    if s.reports_in_progress > s.settings.report_queue_max:
        mark_fallback(ctx, f"{s.reports_in_progress} signups were already waiting for the profile agent")
        return None
    s.reports_in_progress += 1
    try:
        return await run_agent(s, ctx, email, name, persona, key)
    except Exception as exc:  # noqa: BLE001 - a bad report must not cost the signup their email.
        mark_fallback(ctx, why_no_report(exc, s.settings.profile_timeout_s))
        return None
    finally:
        s.reports_in_progress -= 1


async def stop_on_identity(s: Services, signup_id: str, email: gates.Email, person: Person, persona: VendorPersona) -> bool:
    """Repeats go to a person and students get the guide. Runs after every new answer on who this is."""
    if person.person_id is not None:
        holder = profiles.remember_person(signup_id, person.person_id, persona)
        if holder != signup_id:
            first = db.get_signup(holder) or {}
            to_human(
                signup_id,
                "Repeat identity",
                f"Same crustdata_person_id ({person.person_id}) as {first.get('email_raw', 'an earlier signup')}. "
                "Possible free-credit farming, so no new proof of concept.",
            )
            return True
    if person.matched and persona.has("student") and is_student(person):
        await nurture_student(s, signup_id, email.lookup)
        return True
    return False


def summary(signup: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": signup["id"],
        "email": signup["email_raw"],
        "status": signup["status"],
        "playbook": signup.get("playbook"),
        "route": signup.get("route", ""),
        "credits": round(db.credits_for(signup["id"]), 2),
        "sandbox_credits": round(db.credits_for(signup["id"], kind="sandbox"), 2),
        "ttfr_ms": signup.get("ttfr_ms"),
        "fallback": signup.get("fallback_reason", ""),
        "founder": bool(signup.get("high_value")),
        "created_at": signup["created_at"],
    }


def set_state(signup_id: str, status: Status | None = None, **fields: Any) -> None:
    if status is not None:
        fields["status"] = status.value
    db.update_signup(signup_id, **fields)
    signup = db.get_signup(signup_id)
    if signup:
        events.publish("signup", summary(signup))


def delivery_note(signup_id: str, channel: str, status: str, detail: str) -> None:
    events.publish(
        "call",
        db.log_call(
            signup_id=signup_id, kind="delivery", endpoint=channel,
            status=200 if status == "sent" else (500 if status == "failed" else None),
            note=f"{status}: {detail}" if detail else status,
        ),
    )


async def send_email(s: Services, signup_id: str, to: str, subject: str, html: str) -> str:
    result = await s.mailer.send(to, subject, html)
    delivery_note(signup_id, "email", result.status, result.detail)
    db.update_signup(
        signup_id, email_subject=subject, email_html=html, email_to=to,
        email_status=result.status, email_detail=result.detail,
    )
    return result.status


def intent_token_for(signup_id: str) -> str:
    signup = db.get_signup(signup_id) or {}
    token = signup.get("intent_token") or secrets.token_urlsafe(16)
    db.update_signup(signup_id, intent_token=token)
    return token


async def ask_intent(s: Services, signup_id: str, to: str, why: str, *, status: Status = Status.AWAITING_INTENT) -> None:
    """Below the confidence bar we ask. Buttons cost nothing and never send an empty result."""
    persona = persona_of(s, db.get_signup(signup_id) or {})
    subject, html = deliver.render_intent(s.settings, intent_token_for(signup_id), persona)
    await send_email(s, signup_id, to, subject, html)
    set_state(signup_id, status, playbook=Playbook.UNKNOWN.value, route=why)


async def nurture_student(s: Services, signup_id: str, to: str) -> None:
    subject, html = deliver.render_student(s.settings, intent_token_for(signup_id))
    await send_email(s, signup_id, to, subject, html)
    set_state(
        signup_id, Status.NURTURE, playbook=Playbook.STUDENT.value,
        route="Self-serve nurture, 0 rep hours", sales_alert="suppressed",
    )


def to_human(signup_id: str, reason: str, detail: str) -> None:
    db.add_human_item(signup_id, reason, detail)
    events.publish("human", {"signup_id": signup_id, "reason": reason, "detail": detail})
    set_state(signup_id, Status.HUMAN_QUEUE, route=reason, route_detail=detail, sales_alert="suppressed")


async def finish_gate(s: Services, signup_id: str, email: gates.Email, gate: gates.GateResult) -> None:
    if gate.human:
        to_human(signup_id, gate.reason, gate.detail)
        return
    subject, html = deliver.render_internal()
    await send_email(s, signup_id, email.lookup, subject, html)
    set_state(signup_id, gate.status, route=gate.reason, route_detail=gate.detail, sales_alert="suppressed")


async def query_and_deliver(
    s: Services, ctx: RunContext, signup_id: str, to: str, person: Person, company: Company, decision: Decision
) -> None:
    persona = persona_of(s, db.get_signup(signup_id) or {})
    if decision.playbook is Playbook.MESSAGE:
        await deliver_message(s, signup_id, to, person, company, decision, persona)
        return

    set_state(signup_id, Status.QUERYING, playbook=decision.playbook.value)
    outcome = await query.run_query(s.client, ctx, decision, company.domain)
    store_query(signup_id, outcome)
    if outcome.empty:
        await ask_intent(s, signup_id, to, "No results after relaxing, so we asked what they're building")
        return

    set_state(signup_id, Status.DELIVERING)
    sandbox_token = secrets.token_urlsafe(16)
    subject, html = deliver.render_results(s.settings, decision, outcome, sandbox_token, intent_token_for(signup_id))
    signup = db.get_signup(signup_id) or {}
    ttfr_ms = int((time.time() - signup.get("created_at", time.time())) * 1000)
    kind = deliver.row_kind(decision)
    db.update_signup(
        signup_id,
        sandbox_token=sandbox_token,
        pills=query.pills_for(decision, outcome),
        request={"endpoint": outcome.primary.endpoint, "body": outcome.primary.body} if outcome.primary else None,
        result_kind=kind,
        result_columns=list(deliver.COLUMNS[kind]),
        result_rows=deliver.table_rows(kind, outcome.rows),
        ttfr_ms=ttfr_ms,
    )
    await send_email(s, signup_id, to, subject, html)
    await route_signup(s, signup_id, to, person, company, decision, outcome, persona)


async def deliver_message(
    s: Services, signup_id: str, to: str, person: Person, company: Company, decision: Decision, persona: VendorPersona
) -> None:
    """For a vendor with no search to run: the tailored message is the first result."""
    set_state(signup_id, Status.DELIVERING, playbook=decision.playbook.value)
    picks = [pick.service for pick in decision.picks]
    built = starter.compile_starter(decision.starter, picks, persona, profiles.short_name(company.name), person.name)
    if built is None and decision.starter is not None and decision.starter.table:
        agent_note_for(signup_id, "starter", "Claude's starter couldn't be made safe, so the email goes without code")
    token = secrets.token_urlsafe(16) if built else ""
    subject, html = deliver.render_message(s.settings, decision, persona, intent_token_for(signup_id), built, token)
    db.update_signup(signup_id, starter=built, sandbox_token=token)
    signup = db.get_signup(signup_id) or {}
    db.update_signup(signup_id, ttfr_ms=int((time.time() - signup.get("created_at", time.time())) * 1000))
    await send_email(s, signup_id, to, subject, html)
    await route_signup(s, signup_id, to, person, company, decision, QueryOutcome(decision.playbook, None, []), persona)


async def route_signup(
    s: Services, signup_id: str, to: str, person: Person, company: Company, decision: Decision,
    outcome: QueryOutcome, persona: VendorPersona,
) -> None:
    if route.is_high_fit(s.settings, decision, company, persona):
        fallback = (db.get_signup(signup_id) or {}).get("fallback_reason", "")
        brief = route.build_brief(person, company, decision, outcome, to, fallback)
        slack_status, detail = await s.slack.post(brief)
        delivery_note(signup_id, "slack", slack_status, detail)
        set_state(
            signup_id, Status.DELIVERED, brief=brief, slack_status=slack_status, slack_detail=detail,
            route="High fit: sales brief", sales_alert="sent",
        )
    else:
        set_state(signup_id, Status.DELIVERED, route="Self-serve nurture, 0 rep hours", sales_alert="suppressed")


def store_identity(signup_id: str, person: Person, company: Company) -> None:
    db.update_signup(signup_id, person=dataclasses.asdict(person), company=dataclasses.asdict(company))


def store_decision(signup_id: str, decision: Decision, evidence: list[Evidence]) -> None:
    db.update_signup(
        signup_id,
        decision={
            "playbook": decision.playbook.value,
            "confidence": decision.confidence,
            "decided_by": decision.decided_by,
            "segment": decision.segment,
            "reasons": [r.to_dict() for r in decision.reasons],
            "opening": decision.opening.to_dict() if decision.opening else None,
            "subject": decision.subject,
            "picks": [{"service": p.service, **p.reason.to_dict()} for p in decision.picks],
            "sales_angle": decision.sales_angle.to_dict() if decision.sales_angle else None,
            "slots": decision.slots.model_dump(),
        },
        evidence=[dataclasses.asdict(e) for e in evidence],
    )


def store_query(signup_id: str, outcome: QueryOutcome) -> None:
    db.update_signup(
        signup_id,
        attempts=[dataclasses.asdict(a) for a in outcome.attempts],
        snaps=outcome.snaps,
        relaxed=outcome.relaxed,
        companies=outcome.supporting_rows,
    )


async def run_signup(s: Services, signup_id: str) -> None:
    settings = s.settings
    signup = db.get_signup(signup_id)
    if signup is None:
        return
    set_state(signup_id, Status.GATING)

    email = gates.normalise(signup["email_raw"])
    if email is None:
        set_state(
            signup_id, Status.REJECTED, route="That doesn't look like an email address",
            route_detail="Rejected before any call. The signup form asks them to retype it.",
            sales_alert="suppressed",
        )
        return
    set_state(signup_id, email_norm=email.dedupe_key, domain=email.domain, lookup_email=email.lookup)

    persona = persona_of(s, signup)
    gate = gates.free_gates(email, signup_id, persona)
    if gate is not None:
        await finish_gate(s, signup_id, email, gate)
        return

    ctx = RunContext(signup_id, Ledger(cap=settings.budget_initial))
    set_state(signup_id, Status.IDENTIFYING)
    name = signup.get("name", "")
    person, company = await profiles.find(s.client, ctx, email, name, persona)
    store_identity(signup_id, person, company)
    if await stop_on_identity(s, signup_id, email, person, persona):
        return

    report = None
    has_something_to_go_on = bool(name) or person.matched or company.matched
    if settings.profile_builder == "react" and has_something_to_go_on:
        ctx.ledger.raise_cap(settings.budget_standard)
        set_state(signup_id, Status.RESEARCHING)
        known = settings.fast_path and person.matched and person.confidence >= settings.match_confidence_min
        report = await read_report(s, ctx, email, name, persona, saved_only=known)
    if report is not None and not report.resolved:
        mark_fallback(ctx, "the profile agent couldn't identify them")
    if report is not None:
        person, company = profile_doc.apply(report, person, company, email.domain, gates.is_freemail(email.domain))
        db.update_signup(
            signup_id, profile_report=report.raw,
            profile_meta={
                "status": "resolved" if report.resolved else "unresolved", "matched_on": report.matched_on,
                "credits": report.credits, "tools": report.tools,
            },
        )
        store_identity(signup_id, person, company)
        if await stop_on_identity(s, signup_id, email, person, persona):
            return

    academic_domain = gates.is_academic(email.domain)
    if person.matched and person.confidence < settings.match_confidence_min:
        await ask_intent(s, signup_id, email.lookup, f"Identity match too weak ({person.confidence:.2f}), so we asked")
        return
    if not person.matched and (not company.matched or academic_domain):
        await ask_intent(s, signup_id, email.lookup, "Couldn't identify them, so we asked what they're building")
        return

    ctx.ledger.raise_cap(settings.budget_standard)
    is_faculty = persona.has("academic") and academic_domain and person.matched and is_researcher(person)
    forced = Playbook.ACADEMIC if is_faculty else None
    if forced is None:
        set_state(signup_id, Status.RESEARCHING)
        if report is not None and report.resolved:
            await profiles.research_company(s.client, ctx, person, company)
        else:
            await profiles.deepen(s.client, ctx, person, company, persona)
        store_identity(signup_id, person, company)
        if person.status == "redacted":
            set_state(
                signup_id, Status.WITHDRAWN, route="Data removal on file, so we stopped",
                route_detail="This person asked for their data to be removed. Nothing further was processed.",
                sales_alert="suppressed",
            )
            return

    set_state(signup_id, Status.CLASSIFYING)
    table = build_evidence(person, company)
    decision = await classify_module.classify(
        s.llm, settings, signup_id, person, company, table, email.domain, persona, forced=forced
    )
    store_decision(signup_id, decision, table.items)

    if (
        decision.playbook is Playbook.UNKNOWN
        or decision.confidence < settings.classify_confidence_min
        or not decision.reasons
    ):
        await ask_intent(s, signup_id, email.lookup, "Not confident enough to guess, so we asked")
        return
    if decision.playbook is Playbook.STUDENT:
        await nurture_student(s, signup_id, email.lookup)
        return

    await query_and_deliver(s, ctx, signup_id, email.lookup, person, company, decision)


async def safe_exit(s: Services, signup_id: str, why: str) -> None:
    """The last line of defence. If even the fallback email fails, the status still tells the truth."""
    events.publish("call", db.log_call(signup_id=signup_id, kind="agent", endpoint="safe exit", note=why))
    signup = db.get_signup(signup_id) or {}
    to = signup.get("lookup_email")
    try:
        if to and not signup.get("email_html"):
            await ask_intent(s, signup_id, to, f"{why}. Fell back to asking", status=Status.FAILED_SAFE)
        else:
            set_state(signup_id, Status.FAILED_SAFE, route=why)
    except Exception:  # noqa: BLE001 - nothing may escape the last line of defence.
        log.exception("safe exit failed for %s", signup_id)
        set_state(signup_id, Status.FAILED_SAFE, route=why)


async def _guarded(s: Services, signup_id: str, work: Any) -> None:
    try:
        async with asyncio.timeout(s.settings.pipeline_timeout_s):
            await work
    except TimeoutError:
        await safe_exit(s, signup_id, f"Timed out after {s.settings.pipeline_timeout_s:.0f} s")
    except Exception as exc:  # noqa: BLE001 - a demo must never crash on one bad signup.
        log.exception("pipeline failed for %s", signup_id)
        await safe_exit(s, signup_id, f"Unexpected {type(exc).__name__}")


async def process_signup(s: Services, signup_id: str) -> None:
    await _guarded(s, signup_id, run_signup(s, signup_id))
    await watcher.enroll(s, signup_id)  # After their email: a founder's watch never delays it.


# ---- The second chance: an intent button or a "wrong guess?" click ----


def intent_decision(signup: dict[str, Any], segment: Segment) -> tuple[Decision, Person, Company]:
    person = Person(**signup["person"]) if signup.get("person") else Person()
    company = Company(**signup["company"]) if signup.get("company") else Company()
    slots = default_slots(person, company)
    slots.target_country = slots.target_country or FALLBACK_COUNTRY
    slots.icp_industry = slots.icp_industry or FALLBACK_INDUSTRY
    today = dt.date.today().isoformat()
    label = segment.label
    reason = Reason(f"You told us you're working on {label}.", [Evidence("E0", "signup", "intent button", label, today)])
    return Decision(segment.playbook, 1.0, [reason], slots, "intent", segment=segment.id), person, company


async def run_intent(s: Services, signup_id: str, choice: str) -> None:
    signup = db.get_signup(signup_id)
    segment = persona_of(s, signup).segment(choice) if signup else None
    if signup is None or segment is None:
        return
    to = signup.get("lookup_email")
    if not to:
        return
    runs = int(signup.get("intent_runs", 0))
    if runs >= MAX_INTENT_RUNS:
        return
    db.update_signup(signup_id, intent_runs=runs + 1, intent_choice=choice)
    if segment.playbook is Playbook.STUDENT:  # They told us, so no credits and no sales alert.
        await nurture_student(s, signup_id, to)
        return
    decision, person, company = intent_decision(signup, segment)
    store_decision(signup_id, decision, [e for r in decision.reasons for e in r.evidence])
    ledger = Ledger(cap=s.settings.budget_standard, spent=db.credits_for(signup_id))
    await query_and_deliver(s, RunContext(signup_id, ledger), signup_id, to, person, company, decision)


async def process_intent(s: Services, signup_id: str, choice: str) -> None:
    await _guarded(s, signup_id, run_intent(s, signup_id, choice))
    await watcher.enroll(s, signup_id)


# ---- The sandbox pill: the signup's second call ----


def mark_activated(signup: dict[str, Any], label: str) -> None:
    if not signup.get("activated_at"):
        set_state(signup["id"], Status.ACTIVATED, activated_at=time.time(), activated_pill=label)


async def run_pill(s: Services, signup: dict[str, Any], pill: dict[str, Any]) -> dict[str, Any]:
    token = signup["sandbox_token"]
    lock = s.pill_locks.setdefault(token, asyncio.Lock())
    async with lock:  # A double click must not bill twice.
        cached = db.get_pill_run(token, pill["id"])
        if cached:
            return {**cached, "cached": True, "credits": 0}
        if db.count_pill_runs(token) >= s.settings.sandbox_pill_limit:
            return {"ok": False, "message": "That's all the free runs here. Paste the code below to keep going."}

        cost = query.pill_cost(pill)
        ctx = RunContext(signup["id"], Ledger(cap=cost), kind="sandbox")
        started = time.perf_counter()
        try:
            response = await s.client.post(pill["endpoint"], pill["body"], ctx, estimate=cost, note=f"pill: {pill['label']}")
        except BudgetExceeded:
            return {"ok": False, "message": "We've paused live runs for now. Your code below still works."}
        except CrustdataError as exc:
            return {"ok": False, "message": f"That search failed ({exc.error_type}). Nothing was charged. Try another."}

        kind = pill.get("kind", "people")
        rows = query.rows_for_kind(kind, response.data)
        result = {
            "ok": True,
            "pill": pill["id"],
            "label": pill["label"],
            "kind": kind,
            "columns": list(deliver.COLUMNS[kind]),
            "rows": deliver.table_rows(kind, rows),
            "count": len(rows),
            "total": dig(response.data, "total_count"),
            "credits": response.credits,
            "latency_ms": int((time.perf_counter() - started) * 1000),
            "request": {"endpoint": pill["endpoint"], "body": pill["body"]},
        }
        db.save_pill_run(token, pill["id"], result)
        mark_activated(signup, pill["label"])
        return result

