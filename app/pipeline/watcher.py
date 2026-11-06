"""The founder watch. High-value signups get a Crustdata watch, and what changes gets read and acted on.

1. **Enroll**, after a signup finishes: a founder or CEO of a startup, matched with confidence
   (`founders.identify`), gets an entity watch on their profile and one on their company. Watches
   are pull-only, because we have no public URL for a webhook, so we read their run history.
2. **Poll** (`poll`): new runs since the last one we read, and each notification in them. A
   notification is stored once by its uid, so re-reading a run never acts twice.
3. **Handle** each event, in steps that are saved as they finish, so a crash resumes where it stopped:
   `new` -> facts added to their user.md -> `recorded` -> Claude judges it -> `judged` -> at most
   one email -> `done`.

On the real API a watch commits the account to 5 credits per change, so nothing is created
until WATCH_LIVE is on. The bodies are stored either way, so the dashboard shows what would run.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import secrets
import time
from typing import Any

from app import db, events
from app.crustdata.client import BudgetExceeded, CrustdataError, Ledger, RunContext
from app.models import Company, Person, Pick, Reason, Status, dig
from app.personas import VendorPersona
from app.pipeline import deliver, founders, profile_doc, signals
from app.services import Services

log = logging.getLogger("crustflow")

WATCHABLE = {Status.DELIVERED.value, Status.ACTIVATED.value, Status.AWAITING_INTENT.value}
UNFINISHED = ("new", "recorded", "judged")
STILL_RUNNING = {"RUNNING", "PENDING", "QUEUED"}
RUNS_PER_READ = 50

# What fires a watch. A new role or a changed headline for the founder; for the company, money
# and news. Headcount and open roles move all the time and each move is a paid notification.
PERSON_TRACK = {"op": "or", "conditions": [
    {"field": "experience.employment_details.current", "type": "added", "keys": ["crustdata_company_id", "name"]},
    {"field": "basic_profile.headline", "type": "changed"},
]}
COMPANY_TRACK = {"op": "or", "conditions": [
    {"field": "funding.last_round_type", "type": "changed"},
    {"field": "funding.total_investment_usd", "type": "changed"},
    {"field": "funding.investors", "type": "added"},
    {"field": "funding.acquisitions", "type": "added"},
    {"field": "news", "type": "added", "keys": ["article_url"]},
]}
PERSON_FIELDS = ["basic_profile", "experience"]
COMPANY_FIELDS = ["basic_info", "headcount", "funding", "news"]


def _ctx(signup_id: str | None) -> RunContext:
    return RunContext(signup_id, Ledger(cap=0.0))  # Reading and creating watches is free.


def note(signup_id: str | None, text: str) -> None:
    events.publish("call", db.log_call(signup_id=signup_id, kind="watch", endpoint="founder watch", note=text[:160]))


def changed(signup_id: str) -> None:
    events.publish("watch", {"signup_id": signup_id})


def persona_for(s: Services, signup: dict[str, Any]) -> VendorPersona:
    return s.personas.get(signup.get("persona_id") or "") or s.personas[s.settings.vendor_persona]


def creates_watches(s: Services) -> bool:
    return s.settings.uses_simulator or s.settings.watch_live


# ---- Enroll ----


def watch_bodies(s: Services, person: Person, company: Company, founder: founders.Founder) -> list[tuple[str, str, dict[str, Any]]]:
    config = {
        "trigger": {"type": "interval", "every_hours": s.settings.watch_every_hours},
        "max_results_per_run": 25,
        "refresh_frequency_days": 30,  # The standard rate. Fresher data costs more per change.
        "payload_delivery_type": "inline",
    }
    bodies = [("person", person.profile_url, {
        "entities": {"professional_network_profile_urls": [person.profile_url]},
        "track": PERSON_TRACK, "fields": PERSON_FIELDS, "config": config, "notifications": [],
    })]
    if founder.watch_company:
        entities = {"crustdata_company_ids": [company.company_id]} if company.company_id else {"domains": [company.domain]}
        subject = str(company.company_id or company.domain)
        bodies.append(("company", subject, {
            "entities": entities, "track": COMPANY_TRACK, "fields": COMPANY_FIELDS, "config": config, "notifications": [],
        }))
    return bodies


async def enroll(s: Services, signup_id: str) -> None:
    """Never raises: a signup's email has already gone, and the watch is extra."""
    try:
        await _enroll(s, signup_id)
    except Exception:  # noqa: BLE001 - see above.
        log.exception("watch enroll failed for %s", signup_id)
        note(signup_id, "couldn't set up the founder watch, so this signup isn't watched")


async def _enroll(s: Services, signup_id: str) -> None:
    signup = db.get_signup(signup_id)
    if not s.settings.watch_founders or signup is None or signup.get("status") not in WATCHABLE:
        return
    if signup.get("watch_stopped") or not signup.get("person"):
        return
    person, company = Person(**signup["person"]), Company(**(signup.get("company") or {}))
    founder = founders.identify(s.settings, person, company)
    if founder is None:
        return
    if not signup.get("high_value"):
        db.update_signup(signup_id, high_value=founder.to_dict(), stop_token=secrets.token_urlsafe(16))
        note(signup_id, f"high value: {founder.role}{f' at {company.name}' if company.name else ''}")
    for dataset, subject, body in watch_bodies(s, person, company, founder):
        watch = db.claim_watch(signup_id, dataset, subject, body=body)
        if watch is None:
            holder = db.find_watch(dataset, subject) or {}
            if holder.get("signup_id") != signup_id:
                note(signup_id, f"{dataset} already watched for signup {holder.get('signup_id')}, so no second watch")
            continue
        await create(s, watch)
    changed(signup_id)


async def create(s: Services, watch: dict[str, Any]) -> None:
    signup_id = watch["signup_id"]
    if not creates_watches(s):
        db.update_watch(watch["id"], status="planned", detail="WATCH_LIVE is off, so it wasn't created")
        note(signup_id, f"{watch['dataset']} watch planned, not created: WATCH_LIVE is off")
        return
    try:
        response = await s.client.post(f"/watch/{watch['dataset']}", watch["body"], _ctx(signup_id), note=f"watch the {watch['dataset']}")
    except CrustdataError as exc:
        db.update_watch(watch["id"], status="failed", detail=f"{exc.error_type}: {exc.message}"[:200])
        note(signup_id, f"{watch['dataset']} watch failed: {exc.error_type}")
        return
    crustdata_id = dig(response.data, "id")
    if not isinstance(crustdata_id, int):
        db.update_watch(watch["id"], status="failed", detail="Crustdata returned no watch id")
        note(signup_id, f"{watch['dataset']} watch failed: no id in the response")
        return
    db.update_watch(watch["id"], status="active", crustdata_id=crustdata_id, detail="")
    note(signup_id, f"{watch['dataset']} watch {crustdata_id} active, checked every {s.settings.watch_every_hours} h")


# ---- Poll ----


async def poll(s: Services) -> int:
    """Reads every active watch, then works through anything not finished. Returns new events found."""
    async with s.watch_lock:  # The background loop and the dashboard's button must not overlap.
        found = 0
        for watch in db.list_watches(status="active"):
            try:
                found += await poll_watch(s, watch)
            except (CrustdataError, BudgetExceeded) as exc:
                note(watch["signup_id"], f"couldn't read {watch['dataset']} watch {watch['crustdata_id']}: {exc}"[:160])
        for event in db.list_watch_events(statuses=UNFINISHED):
            await handle(s, event["uid"])
        return found


async def poll_watch(s: Services, watch: dict[str, Any]) -> int:
    ctx = _ctx(watch["signup_id"])
    base = f"/watch/{watch['dataset']}/{watch['crustdata_id']}"
    response = await s.client.get(f"{base}/runs", ctx, params={"limit": RUNS_PER_READ}, note="any new runs?")
    runs = [r for r in dig(response.data, "runs", default=[]) if isinstance(r, dict) and int(r.get("id") or 0) > watch["last_run_id"]]
    found = 0
    for run in sorted(runs, key=lambda r: int(r["id"])):
        status = str(run.get("status", "")).upper()
        if status in STILL_RUNNING:
            break  # Read it next time, when it's finished. Later runs wait too, so none is skipped.
        if status == "SUCCESS" and (run.get("new_records_count") or run.get("records_after_filter")):
            summary = await s.client.get(f"{base}/runs/{run['id']}/summary", ctx, note=f"run {run['id']}")
            found += store_run(watch, run, summary.data or {})
        elif status != "SUCCESS":
            note(watch["signup_id"], f"{watch['dataset']} watch run {run['id']} {status.lower()}: {run.get('failure_reason') or 'no reason given'}")
        if run.get("credits_deducted"):
            events.publish("call", db.log_call(
                signup_id=watch["signup_id"], kind="crustdata", endpoint=f"{base} run", status=200,
                credits=float(run["credits_deducted"]), note=f"run {run['id']}: Crustdata's charge for its notifications",
            ))
        db.update_watch(watch["id"], last_run_id=int(run["id"]), last_checked=time.time())
    return found


def store_run(watch: dict[str, Any], run: dict[str, Any], summary: dict[str, Any]) -> int:
    """One event per notified subject that changed. The baseline's sample has no changes, so it's skipped."""
    date = str(run.get("completed_at") or run.get("started_at") or signals.today())[:10]
    stored = 0
    for delivery in summary.get("notifications") or []:
        for index, item in enumerate(dig(delivery, "payload", "notifications", default=[])):
            changes = (item.get("changes") or []) if isinstance(item, dict) else []
            if not changes:
                continue
            record = item.get("record") or {}
            facts = signals.facts(watch["dataset"], changes, record, date)
            if not facts:
                continue
            uid = str(item.get("uid") or f"{watch['dataset']}_{watch['subject']}_{run['id']}_{index}")
            if db.add_watch_event(
                uid, watch["id"], watch["signup_id"], int(run["id"]),
                dataset=watch["dataset"], changes=changes, facts=[dataclasses.asdict(f) for f in facts],
            ):
                stored += 1
                note(watch["signup_id"], f"new activity: {facts[0].text}")
    if summary.get("payload_delivery", {}).get("type") == "link" and not stored:
        note(watch["signup_id"], f"run {run['id']} was delivered as a file, which we don't read")
    if stored:
        changed(watch["signup_id"])
    return stored


# ---- Handle one event ----


async def handle(s: Services, uid: str) -> None:
    """Each step saves its result before the next starts, so a retry picks up where it stopped."""
    event = db.get_watch_event(uid)
    if event is None:
        return
    try:
        signup = db.get_signup(event["signup_id"])
        if signup is None:
            db.update_watch_event(uid, status="done", outcome="the signup was deleted")
            return
        facts = [profile_doc.Fact(**f) for f in event.get("facts", [])]
        if event["status"] == "new":
            record_activity(signup, facts)
            db.update_watch_event(uid, status="recorded")
            event["status"] = "recorded"
        if event["status"] == "recorded":
            persona = persona_for(s, signup)
            judgement = await signals.judge(s.llm, signup, persona, signals.evidence(facts))
            db.update_watch_event(uid, status="judged", judgement=judgement.to_dict())
            event = db.get_watch_event(uid) or event
        if event["status"] == "judged":
            outcome = await maybe_email(s, db.get_signup(event["signup_id"]) or signup, event)
            db.update_watch_event(uid, status="done", outcome=outcome)
            note(signup["id"], f"activity handled: {outcome}")
    except Exception as exc:  # noqa: BLE001 - one bad event must not stop the rest.
        log.exception("watch event %s failed", uid)
        db.update_watch_event(uid, status="failed", outcome=f"failed: {type(exc).__name__}")
        note(event["signup_id"], f"couldn't handle activity {uid}: {type(exc).__name__}")
    changed(event["signup_id"])


def record_activity(signup: dict[str, Any], facts: list[profile_doc.Fact]) -> None:
    """user.md is the living profile. We add to it in the database, the report cache and on disk."""
    email = signup.get("lookup_email") or signup["email_raw"]
    current = signup.get("user_md") or signup.get("profile_report")
    if not current:
        current = signals.starter_user_md(email, Person(**signup["person"]), Company(**(signup.get("company") or {})))
    updated = signals.add_activity(current, facts)
    db.update_signup(signup["id"], user_md=updated)
    key = f"report:{signup.get('email_norm', '')}"
    if db.cache_get(key) is not None:  # A rerun reads the latest, not the day-one report.
        db.cache_set(key, updated)
    path = signals.agent_file(email)
    if path.exists():
        path.write_text(updated)


def last_signal_email(signup_id: str) -> float:
    """A rendered email counts too, so the demo behaves the way a live run would."""
    sent = [e.get("sent_at") or 0 for e in db.list_watch_events(signup_id=signup_id) if e.get("email_status") in ("sent", "rendered")]
    return max(sent, default=0.0)


async def maybe_email(s: Services, signup: dict[str, Any], event: dict[str, Any]) -> str:
    """Returns what happened, in words, for the dashboard."""
    judgement = event.get("judgement") or {}
    persona = persona_for(s, signup)
    if judgement.get("strength") != "strong":
        return f"recorded, no email: a {judgement.get('strength', 'unjudged')} signal"
    if not judgement.get("relevant") and judgement.get("decided_by") == "rules":
        return "recorded, no email: Claude couldn't judge it, and no services were matched to them earlier"
    if not judgement.get("relevant"):
        return f"recorded, no email: nothing {persona.name} offers fits it"
    if not judgement.get("opening"):
        return "recorded, no email: no opening that cites the news"
    if float(judgement.get("confidence") or 0) < s.settings.signal_confidence_min:
        return f"recorded, no email: confidence {float(judgement.get('confidence') or 0):.2f} is under the bar"
    if signup.get("watch_stopped"):
        return "recorded, no email: they asked us to stop"
    if event.get("email_status"):  # A retry after a crash: whatever happened then stands.
        return "recorded, no email: already sent"
    to = signup.get("lookup_email")
    if not to:
        return "recorded, no email: no address"
    cooldown = s.settings.signal_email_cooldown_days * 86400
    if time.time() - last_signal_email(signup["id"]) < cooldown:
        return f"recorded, no email: they had one in the last {s.settings.signal_email_cooldown_days:g} days"
    if not db.claim_event_email(event["uid"]):
        return "recorded, no email: already sent"

    restored = signals.Judgement(
        judgement["strength"], judgement["relevant"], judgement["confidence"], None, judgement["decided_by"],
        judgement.get("subject", ""), _reason(judgement["opening"]),
        [Pick(p["service"], _reason(p)) for p in judgement.get("picks", [])],
    )
    base = s.settings.app_base_url
    token = signup.get("sandbox_token")  # If they have a starter, it's still their best next step.
    sandbox_url = f"{base}/sandbox/{token}" if token and signup.get("starter") else ""
    context = signals.email_context(
        restored, persona, f"{base}/watch/stop/{signup.get('stop_token', '')}", signup.get("starter"), sandbox_url
    )
    subject = context["hero"]["title"]
    html = deliver.env.get_template("email/t8_signal.html").render(**context)
    result = await s.mailer.send(to, subject, html)
    events.publish("call", db.log_call(
        signup_id=signup["id"], kind="delivery", endpoint="signal email",
        status=200 if result.status == "sent" else (500 if result.status == "failed" else None),
        note=f"{result.status}: {result.detail}" if result.detail else result.status,
    ))
    db.update_watch_event(
        event["uid"], email_status=result.status, email_detail=result.detail, email_subject=subject,
        email_html=html, email_to=to, sent_at=time.time(),
    )
    return {"sent": "emailed", "rendered": "email written, shown here only", "failed": "email failed"}[result.status]


def _reason(item: dict[str, Any]) -> Reason:
    return Reason(item["claim"], [signals.evidence_from(c) for c in item.get("citations", [])])



# ---- Stop, simulate, and the loop ----


async def stop(s: Services, signup_id: str) -> int:
    """Their choice, so it's final: watches are cancelled, not paused. Returns how many were cancelled."""
    db.update_signup(signup_id, watch_stopped=True)
    cancelled = 0
    for watch in db.list_watches(signup_id=signup_id):
        if watch["status"] == "active" and watch["crustdata_id"]:
            try:
                await s.client.delete(f"/watch/{watch['dataset']}/{watch['crustdata_id']}", _ctx(signup_id), note="they asked us to stop")
            except CrustdataError as exc:
                note(signup_id, f"couldn't cancel {watch['dataset']} watch {watch['crustdata_id']}: {exc.error_type}. Cancel it by hand")
                continue
        if watch["status"] in ("active", "planned"):
            db.update_watch(watch["id"], status="cancelled")
            cancelled += 1
    note(signup_id, f"they asked us to stop: {cancelled} watch(es) cancelled")
    changed(signup_id)
    return cancelled


async def simulate(s: Services, watch_id: int, kind: str) -> int:
    """Simulator only: makes something happen to a watched subject, then polls for it."""
    watch = db.get_watch(watch_id)
    simulator = getattr(s.client.transport, "watches", None)
    if watch is None or simulator is None or not watch.get("crustdata_id"):
        raise ValueError("only an active watch on simulated data can be simulated")
    simulator.simulate(watch["crustdata_id"], kind)
    note(watch["signup_id"], f"simulated: {kind} on the {watch['dataset']}")
    return await poll(s)


async def poll_forever(s: Services) -> None:
    while True:
        await asyncio.sleep(s.settings.watch_poll_s)
        try:
            await poll(s)
        except Exception:  # noqa: BLE001 - the loop must outlive one bad poll.
            log.exception("watch poll failed")


def summary(signup_id: str) -> dict[str, Any]:
    """Everything the dashboard's Watch tab shows."""
    signup = db.get_signup(signup_id) or {}
    return {
        "high_value": signup.get("high_value"),
        "stopped": bool(signup.get("watch_stopped")),
        "watches": [
            {k: w.get(k) for k in ("id", "dataset", "subject", "status", "crustdata_id", "last_run_id", "detail", "last_checked")}
            for w in db.list_watches(signup_id=signup_id)
        ],
        "events": [
            {k: e.get(k) for k in (
                "uid", "dataset", "status", "created_at", "facts", "judgement", "outcome", "email_status",
                "email_subject", "email_detail",
            )}
            for e in reversed(db.list_watch_events(signup_id=signup_id))
        ],
        "has_user_md": bool(signup.get("user_md") or signup.get("profile_report")),
    }
