"""The founder watch: who gets watched, what a change becomes, and when it's worth an email."""

import dataclasses

import pytest

from app import db
from app.llm import LLMFailure, LLMPick, LLMReason, LLMSignal
from app.models import Company, Person
from app.personas import load_personas
from app.pipeline import founders, profile_doc, signals, watcher
from app.pipeline.run import process_signup
from tests.conftest import make_settings

SUPABASE = load_personas()["supabase"]


async def run(services, email, name, persona="crustdata"):
    signup = db.create_signup(email, name=name, persona_id=persona)
    await process_signup(services, signup["id"])
    return db.get_signup(signup["id"])


def founder(**overrides):
    values = dict(
        status="matched", confidence=0.95, person_id=1, name="Ada Park", title="Co-Founder & CEO",
        profile_url="https://example.com/profiles/ada", match_method="name_and_company",
    )
    return Person(**{**values, **overrides})


def startup(**overrides):
    values = dict(matched=True, company_id=7, name="Tiny", domain="tiny.io", headcount=12, company_type="Privately Held")
    return Company(**{**values, **overrides})


def watches(signup):
    return {w["dataset"]: w for w in db.list_watches(signup_id=signup["id"])}


def stub_judge(services, **answer):
    """Claude's answer, without Claude. Classification already ran, so only the judgement uses it."""
    calls = []

    async def judge_signal(signup_id, persona, profile, signal):
        calls.append(signal)
        if answer.get("fail"):
            raise LLMFailure("down")
        values = dict(
            strength="strong", relevant=True, confidence=0.9,
            why=LLMReason(claim="A new round means more users soon.", evidence_ids=["S1"]),
            subject="Ready for the next wave of users",
            opening=LLMReason(claim="With a new round in, here's how to scale your database.", evidence_ids=["S1"]),
            picks=[LLMPick(service="database", why="Room to grow without a migration.", evidence_ids=["S1"])],
        )
        return LLMSignal(**{**values, **answer})

    services.llm.client = object()  # `available` is all judge() checks.
    services.llm.judge_signal = judge_signal
    return calls


# ---- Who is high value ----


def test_a_startup_founder_is_high_value(services):
    found = founders.identify(services.settings, founder(), startup())
    assert found and found.role == "Co-Founder & CEO" and found.watch_company
    assert all(r.evidence for r in found.reasons), "every reason is cited"


@pytest.mark.parametrize("title, headline", [
    ("Founder's Office Associate", ""),
    ("Product Owner", ""),
    ("Chief of Staff to the CEO", ""),
    ("", "Advisor to founders | Angel investor"),
    ("Founding Engineer", ""),
])
def test_roles_that_only_mention_founders_are_not(services, title, headline):
    assert founders.identify(services.settings, founder(title=title, headline=headline), startup()) is None


def test_founder_in_a_headline_counts(services):
    person = founder(title="", headline="Building Tiny | Co-founder")
    assert founders.identify(services.settings, person, startup()).role == "Co-founder"


@pytest.mark.parametrize("company", [
    startup(headcount=5000), startup(company_type="Public Company"), startup(funding_round="series_e"),
])
def test_big_companies_are_not_startups(services, company):
    assert founders.identify(services.settings, founder(), company) is None


@pytest.mark.parametrize("person", [
    founder(confidence=0.8), founder(status="not_found"), founder(profile_url=""), founder(profile_url="javascript:x"),
])
def test_never_watch_someone_we_might_have_wrong(services, person):
    assert founders.identify(services.settings, person, startup()) is None


def test_unknown_company_watches_the_founder_only(services):
    found = founders.identify(services.settings, founder(), Company())
    assert found and not found.watch_company


# ---- Enrolment ----


async def test_non_founders_are_not_watched(services):
    signup = await run(services, "priya@hirefast.in", "Priya Sharma")
    assert not db.list_watches(signup_id=signup["id"]) and not signup.get("high_value")


async def test_a_founder_gets_a_person_and_a_company_watch(services):
    signup = await run(services, "alex@northwindai.io", "Alex Mercer")
    assert signup["high_value"]["role"] == "Founder & CEO"
    found = watches(signup)
    assert {w["status"] for w in found.values()} == {"active"}
    assert found["person"]["body"]["entities"]["professional_network_profile_urls"] == [signup["person"]["profile_url"]]
    assert found["company"]["body"]["entities"] == {"crustdata_company_ids": [5002]}
    assert found["person"]["body"]["notifications"] == [], "pull-only: there's no public URL for a webhook"
    assert await watcher.poll(services) == 0, "the baseline run fires on nothing"
    assert not db.list_watch_events(signup_id=signup["id"])


async def test_watch_creation_failing_never_touches_the_signup(services):
    services.client.faults.add("/watch/company", 500, 4)
    signup = await run(services, "alex@northwindai.io", "Alex Mercer")
    assert signup["status"] == "delivered"
    found = watches(signup)
    assert found["company"]["status"] == "failed" and found["person"]["status"] == "active"


async def test_one_company_is_watched_once(services):
    first = await run(services, "alex@northwindai.io", "Alex Mercer")
    db.update_signup(first["id"], status="delivered")
    second = db.create_signup("other@northwindai.io")
    db.update_signup(second["id"], status="delivered", person=dataclasses.asdict(founder(profile_url="https://example.com/x")),
                     company=first["company"])
    await watcher.enroll(services, second["id"])
    assert set(watches(db.get_signup(second["id"]))) == {"person"}


async def test_watches_are_planned_not_created_on_the_real_api_without_watch_live(tmp_path):
    from app.services import build_services

    db.close()
    settings = make_settings(tmp_path, crustdata_mode="live", watch_live=False)
    db.connect(settings.db_path)
    s = build_services(settings)
    try:
        signup = db.create_signup("ada@tiny.io")
        db.update_signup(signup["id"], status="delivered", person=dataclasses.asdict(founder()), company=dataclasses.asdict(startup()))
        await watcher.enroll(s, signup["id"])
        assert {w["status"] for w in db.list_watches(signup_id=signup["id"])} == {"planned"}
        assert not [c for c in db.list_calls(signup["id"]) if c["kind"] == "crustdata"], "nothing was sent to Crustdata"
    finally:
        await s.client.close()
        db.close()


# ---- Activity into user.md ----


async def test_activity_is_added_to_user_md_with_its_source(services):
    signup = await run(services, "alex@northwindai.io", "Alex Mercer")
    assert await watcher.simulate(services, watches(signup)["company"]["id"], "funding") == 1
    signup = db.get_signup(signup["id"])
    report = profile_doc.parse(signup["user_md"], "alex@northwindai.io")  # Still a valid user-profile/v1.
    activity = [f.text for f in report.sections["Recent activity"]]
    assert "Northwind AI raised a Series B round (the last was Series A)" in activity
    assert "[watch_company · funding.last_round_type ·" in signup["user_md"]
    assert "Nothing found" not in signup["user_md"].split("## Recent activity")[1]


def test_add_activity_keeps_the_rest_of_the_report():
    text = open("tests/fixtures/profile_report.md").read()
    fact = profile_doc.Fact("Tiny raised a Seed round", "watch_company", "funding.last_round_type", "2026-11-06")
    updated = signals.add_activity(text, [fact])
    assert signals.add_activity(updated, [fact]) == updated, "the same fact is added once"
    before, after = profile_doc.parse(text, "noor_khan@hirefast.in"), profile_doc.parse(updated, "noor_khan@hirefast.in")
    assert after.sections.pop("Recent activity")[0].text == fact.text
    before.sections.pop("Recent activity", None)
    assert after.sections == before.sections and after.billed == before.billed and after.meta == before.meta


def test_news_cites_the_article():
    changes = [{"field": "news", "type": "added", "new_elements": [
        {"title": "Tiny ships v2", "article_url": "https://news.example/tiny", "published_at": "2026-11-01"},
    ]}]
    [fact] = signals.facts("company", changes, {"basic_info": {"name": "Tiny"}}, "2026-11-06")
    assert (fact.source, fact.field, fact.date) == ("web", "https://news.example/tiny", "2026-11-01")


def test_contact_details_in_a_change_are_masked():
    changes = [{"field": "basic_profile.headline", "type": "changed", "from": "x", "to": "CEO, mail ada@tiny.io"}]
    [fact] = signals.facts("person", changes, {"basic_profile": {"name": "Ada"}}, "2026-11-06")
    assert "@" not in fact.text


async def test_a_rerun_never_acts_twice(services):
    signup = await run(services, "alex@northwindai.io", "Alex Mercer")
    await watcher.simulate(services, watches(signup)["company"]["id"], "funding")
    db.update_watch(watches(signup)["company"]["id"], last_run_id=0)  # As if we lost our place.
    assert await watcher.poll(services) == 0
    assert len(db.list_watch_events(signup_id=signup["id"])) == 1


# ---- Judging and the email ----


async def test_strong_relevant_signal_is_emailed_with_citations(services):
    signup = await run(services, "alex@northwindai.io", "Alex Mercer", "supabase")
    calls = stub_judge(services)
    await watcher.simulate(services, watches(signup)["company"]["id"], "funding")
    [event] = db.list_watch_events(signup_id=signup["id"])
    assert calls and calls[0][0]["id"] == "S1", "Claude reads the change as S rows"
    assert event["status"] == "done" and event["email_status"] == "rendered"
    html = event["email_html"]
    assert "https://supabase.com/docs/guides/database/overview" in html, "links come from the persona"
    assert "Tailored from: public company data" in html
    assert f"/watch/stop/{db.get_signup(signup['id'])['stop_token']}" in html
    assert "watch" not in event["judgement"]["opening"]["claim"].lower()


async def test_one_signal_email_per_cooldown(services):
    signup = await run(services, "alex@northwindai.io", "Alex Mercer", "supabase")
    stub_judge(services)
    await watcher.simulate(services, watches(signup)["company"]["id"], "funding")
    await watcher.simulate(services, watches(signup)["person"]["id"], "role")
    outcomes = [e["outcome"] for e in db.list_watch_events(signup_id=signup["id"])]
    assert outcomes[0].startswith("email written") and "in the last 7 days" in outcomes[1]


@pytest.mark.parametrize("answer, reason", [
    ({"strength": "weak"}, "a weak signal"),
    ({"relevant": False}, "nothing Supabase offers fits it"),
    ({"confidence": 0.3}, "under the bar"),
    ({"opening": LLMReason(claim="Here's how to scale.", evidence_ids=["E1"])}, "no opening that cites the news"),
    ({"opening": LLMReason(claim="Here's how to scale.", evidence_ids=["S99"])}, "no opening that cites the news"),
])
async def test_no_email_unless_strong_relevant_and_cited(services, answer, reason):
    signup = await run(services, "alex@northwindai.io", "Alex Mercer", "supabase")
    stub_judge(services, **answer)
    await watcher.simulate(services, watches(signup)["company"]["id"], "funding")
    [event] = db.list_watch_events(signup_id=signup["id"])
    assert reason in event["outcome"] and not event.get("email_status")


async def test_without_claude_rules_never_invent_an_offer(services):
    signup = await run(services, "alex@northwindai.io", "Alex Mercer", "supabase")
    stub_judge(services, fail=True)
    await watcher.simulate(services, watches(signup)["company"]["id"], "funding")
    [event] = db.list_watch_events(signup_id=signup["id"])
    assert event["judgement"]["decided_by"] == "rules" and event["judgement"]["strength"] == "strong"
    assert "no services were matched to them earlier" in event["outcome"], "no earlier picks, so nothing to offer"


async def test_a_crash_after_claiming_the_email_never_sends_it_twice(services):
    signup = await run(services, "alex@northwindai.io", "Alex Mercer", "supabase")
    stub_judge(services)
    sends = []
    original = services.mailer.send

    async def counting_send(*args):
        sends.append(args)
        return await original(*args)

    services.mailer.send = counting_send
    await watcher.simulate(services, watches(signup)["company"]["id"], "funding")
    [event] = db.list_watch_events(signup_id=signup["id"])
    db.update_watch_event(event["uid"], status="judged")  # As if we crashed after sending.
    await watcher.poll(services)
    assert len(sends) == 1
    assert "already sent" in db.get_watch_event(event["uid"])["outcome"]


async def test_a_failed_step_is_flagged_and_the_rest_carry_on(services, monkeypatch):
    signup = await run(services, "alex@northwindai.io", "Alex Mercer", "supabase")
    stub_judge(services)

    def broken(*_):
        raise RuntimeError("disk full")

    monkeypatch.setattr(watcher, "record_activity", broken)
    await watcher.simulate(services, watches(signup)["company"]["id"], "funding")
    [event] = db.list_watch_events(signup_id=signup["id"])
    assert event["status"] == "failed" and "RuntimeError" in event["outcome"]


# ---- Stopping ----


async def test_stop_cancels_the_watches_and_the_emails(services):
    signup = await run(services, "alex@northwindai.io", "Alex Mercer", "supabase")
    company = watches(signup)["company"]
    assert await watcher.stop(services, signup["id"]) == 2
    assert {w["status"] for w in db.list_watches(signup_id=signup["id"])} == {"cancelled"}
    assert services.client.transport.watches.items[company["crustdata_id"]]["status"] == "cancelled"
    await watcher.enroll(services, signup["id"])
    assert {w["status"] for w in db.list_watches(signup_id=signup["id"])} == {"cancelled"}, "stopping is final"


def test_a_founder_with_a_starter_is_sent_back_to_it():
    judgement = signals.Judgement("strong", True, 0.9, None, "llm", "Next step")
    context = signals.email_context(judgement, SUPABASE, "http://x/stop", {"company": "Acme"}, "http://x/sandbox/tok")
    assert context["cta"] == [{"label": "Run Acme's starter in your browser", "url": "http://x/sandbox/tok", "primary": "1"}]


def test_a_founder_without_one_keeps_the_vendors_own_link():
    judgement = signals.Judgement("strong", True, 0.9, None, "llm", "Next step")
    assert signals.email_context(judgement, SUPABASE, "http://x/stop")["cta"][0]["url"] == SUPABASE.cta_url
