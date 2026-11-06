"""The ReAct loop's report is the profile. These cover reading it and running on it."""

import asyncio
import sys
import types
from pathlib import Path

import httpx
import pytest

from app import db
from app.crustdata.client import Ledger, RunContext
from app.llm import LLMOutput, system_prompt
from app.models import Company, EvidenceTable, Person, Status
from app.personas import load_personas
from app.pipeline import gates, profile_doc
from app.pipeline.context import build_evidence
from app.pipeline.run import process_signup, read_report, run_pill

SAMPLE = (Path(__file__).parent / "fixtures" / "profile_report.md").read_text()
EMAIL = "noor_khan@hirefast.in"
CRUSTDATA = load_personas()["crustdata"]


def report_for(email=EMAIL, **replacements):
    text = SAMPLE.replace(EMAIL, email)
    for old, new in replacements.items():
        text = text.replace(old, new)
    return text


class Loop:
    """Stands in for the teammate's module."""

    def __init__(self, text=SAMPLE, error=None, delay=0, spend=0.0):
        self.text, self.error, self.delay, self.spend, self.calls = text, error, delay, spend, 0

    async def build_report(self, client, ctx, email, name, persona):
        self.calls += 1
        ctx.ledger.charge(self.spend)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        return self.text


def use_loop(monkeypatch, services, loop):
    module = types.ModuleType("app.pipeline.react_profiles")
    module.build_report = loop.build_report
    monkeypatch.setitem(sys.modules, "app.pipeline.react_profiles", module)
    monkeypatch.setattr(services.settings, "profile_builder", "react")
    monkeypatch.setattr(services.settings, "fast_path", False)  # A clear match would skip the loop.
    return loop


def output(segment, confidence=0.9, subject="", picks=None, **slots):
    return LLMOutput.model_validate({
        "segment": segment, "confidence": confidence,
        "opening": {"claim": "We ran a first search for you.", "evidence_ids": ["E1"]},
        "reasons": [{"claim": "Your work fits this.", "evidence_ids": ["E1"]}],
        "subject": subject, "picks": picks or [],
        "slots": {
            "target_role": "", "target_city": "", "target_country": "", "icp_industry": "", "buyer_title": "",
            "research_field": "", **slots,
        },
    })


class StubLLM:
    available = True
    fail_next = 0

    def __init__(self, result):
        self.result, self.rows = result, []

    async def classify(self, signup_id, table, domain, persona):
        self.rows = table.as_prompt_rows()
        return self.result


async def run(services, raw=EMAIL, name="Noor Khan"):
    signup = db.create_signup(raw, name=name)
    await process_signup(services, signup["id"])
    return db.get_signup(signup["id"])


def notes(signup):
    return " | ".join(c["note"] or "" for c in db.list_calls(signup["id"]))


# ---- Reading it ----


def test_the_sample_format_parses():
    report = profile_doc.parse(SAMPLE, EMAIL)
    assert report.resolved and report.person_name == "Noor Ahmed"
    assert report.credits == 7.42, "from the agent's table, because its front matter has no credits"
    assert report.tools == ["/person/search"]
    assert report.matched_on.startswith("Crustdata person 9010")


def test_the_older_sample_format_still_parses():
    text = SAMPLE.replace('name: "Noor Ahmed"', 'resolved_name: "Noor Ahmed"\nstatus: "resolved"\nconfidence: "confirmed"\ncrustdata_credits: 3.5')
    report = profile_doc.parse(text, EMAIL)
    assert report.person_name == "Noor Ahmed" and report.credits == 3.5


def test_a_citation_with_brackets_inside_it_stays_whole():
    fact = profile_doc.parse(SAMPLE, EMAIL).sections["Current role"][0]
    assert fact.source == "person_search"
    assert fact.field == "experience.employment_details.current[0]"
    assert fact.text.endswith("leading the matching team.")


def test_a_date_in_a_citation_is_kept():
    fact = profile_doc.parse(SAMPLE, EMAIL).sections["Recent activity"][0]
    assert (fact.source, fact.field, fact.date) == ("web", "https://example.com/summit", "2026-10-01")


def test_an_empty_section_gives_no_facts():
    assert len(profile_doc.parse(SAMPLE, EMAIL).sections["Recent activity"]) == 1, "'Nothing found' isn't a fact"


@pytest.mark.parametrize("text", [
    "", "no front matter here", "---\nschema: [unclosed\n---\nbody", "---\nschema: \"other/v9\"\nstatus: x\nemail: a@b.co\n---\n",
])
def test_an_unreadable_report_is_refused(text):
    with pytest.raises(profile_doc.ReportError):
        profile_doc.parse(text, EMAIL)


def test_a_report_about_someone_else_is_refused():
    with pytest.raises(profile_doc.ReportError, match="different email"):
        profile_doc.parse(SAMPLE, "someone.else@hirefast.in")


@pytest.mark.parametrize("found_id,domain,personal,confidence", [
    (9010, "hirefast.in", False, profile_doc.BOTH_FOUND_THEM),
    (None, "hirefast.in", False, profile_doc.DOMAIN_MATCHES),
    (None, "eng.hirefast.in", False, profile_doc.DOMAIN_MATCHES),
    (None, "gmail.com", True, profile_doc.NAME_ONLY),
    (None, "somewhere-else.com", False, profile_doc.DOMAIN_DIFFERS),
], ids=["both found them", "same domain", "subdomain", "personal email", "different employer"])
def test_we_decide_identity_ourselves(found_id, domain, personal, confidence):
    """The agent can settle on a namesake and has no way to say it isn't sure."""
    report = profile_doc.parse(SAMPLE, EMAIL)
    person, _ = profile_doc.apply(report, Person(person_id=found_id), Company(), domain, personal)
    assert person.confidence == confidence


async def test_a_report_about_another_company_asks_instead_of_trusting_it(monkeypatch, services):
    other = "noor@somewhere-else.xyz"
    use_loop(monkeypatch, services, Loop(report_for(other)))
    services.llm = StubLLM(output("recruiting", target_role="Backend Engineer"))
    signup = await run(services, other, "Noor Ahmed")
    assert signup["status"] == Status.AWAITING_INTENT
    assert "too weak" in signup["route"]


def test_other_people_and_identity_proof_never_become_evidence():
    rows = profile_doc.evidence_rows(profile_doc.parse(SAMPLE, EMAIL))
    text = " ".join(r["value"] for r in rows)
    report = profile_doc.parse(SAMPLE.replace("## Current role\n", "## Namesakes ruled out\n\n- Noor Ahmed, accountant at Acme Ledger. [person_search · basic_profile.name]\n\n## Current role\n"), EMAIL)
    assert "Acme Ledger" not in " ".join(r["value"] for r in profile_doc.evidence_rows(report)), "namesakes are other people"
    assert "commits" not in text and "github" not in text.lower()
    assert "Gravatar" not in text and "Nothing found" not in text
    assert any(r["value"].startswith("Caveat:") for r in rows)


def test_contact_details_are_masked_but_dates_survive():
    rows = profile_doc.evidence_rows(profile_doc.parse(SAMPLE, EMAIL))
    text = " ".join(r["value"] for r in rows)
    assert "hello@hirefast.in" not in text and "98765" not in text
    assert "[email]" in text and "[phone]" in text
    assert profile_doc.mask("From 2026-03-30 to 2026-06-08") == "From 2026-03-30 to 2026-06-08"
    assert profile_doc.mask("id 188782683") == "id 188782683"


def test_an_unresolved_report_leaves_the_pre_check_alone():
    text = SAMPLE.replace('name: "Noor Ahmed"', 'name: ""')
    person, company = Person(status="matched", person_id=1), Company()
    assert profile_doc.apply(profile_doc.parse(text, EMAIL), person, company, "hirefast.in", False) == (person, company)


def test_citations_name_the_report():
    report = profile_doc.parse(SAMPLE, EMAIL)
    person, company = profile_doc.apply(report, Person(), Company(), "hirefast.in", False)
    table = build_evidence(person, company)
    assert all(not e.source.startswith("person/") for e in table.items)
    assert any(e.source.startswith("profile report") for e in table.items)
    assert any(e.source == "person_search" for e in table.items)


# ---- Running on it ----


async def test_the_report_is_the_profile(monkeypatch, services):
    loop = use_loop(monkeypatch, services, Loop())
    services.llm = StubLLM(output("recruiting", target_role="Backend Engineer"))
    signup = await run(services)
    assert loop.calls == 1
    assert signup["person"]["match_method"] == "report"
    assert signup["person"]["name"] == "Noor Ahmed"
    assert signup["profile_report"] == SAMPLE
    assert signup["status"] == Status.DELIVERED
    assert signup["company"]["headcount"], "routing needs numbers the report only has as prose"


async def test_claude_sees_the_report_but_not_the_namesakes(monkeypatch, services):
    use_loop(monkeypatch, services, Loop())
    services.llm = StubLLM(output("recruiting", target_role="Backend Engineer"))
    await run(services)
    text = " ".join(r["value"] for r in services.llm.rows)
    assert "leading the matching team" in text
    assert "commits" not in text


async def test_the_loops_credits_count_once(monkeypatch, services):
    use_loop(monkeypatch, services, Loop())
    services.llm = StubLLM(output("recruiting", target_role="Backend Engineer"))
    signup = await run(services)
    lines = [c for c in db.list_calls(signup["id"]) if c["endpoint"] == "profile report" and c["credits"]]
    assert [c["credits"] for c in lines] == [7.42]
    assert 7.42 < db.credits_for(signup["id"]) <= services.settings.budget_standard


async def test_a_full_report_still_leaves_room_for_the_search(monkeypatch, services):
    """At the old cap of 8 this ran out of budget and skipped the company."""
    use_loop(monkeypatch, services, Loop())
    services.llm = StubLLM(output("recruiting", target_role="Backend Engineer"))
    signup = await run(services)
    assert len(signup["result_rows"]) == 10


async def test_calls_the_loop_made_through_our_client_arent_charged_twice(monkeypatch, services):
    use_loop(monkeypatch, services, Loop(spend=7.42))
    services.llm = StubLLM(output("recruiting", target_role="Backend Engineer"))
    signup = await run(services)
    assert not [c for c in db.list_calls(signup["id"]) if c["endpoint"] == "profile report" and c["credits"]]


async def test_a_saved_report_is_free_and_skips_the_loop(monkeypatch, services):
    loop = use_loop(monkeypatch, services, Loop())
    email = gates.normalise(EMAIL)
    first = await read_report(services, RunContext("a", Ledger(cap=15)), email, "Noor Khan", CRUSTDATA)
    ctx = RunContext("b", Ledger(cap=15))
    second = await read_report(services, ctx, email, "Noor Khan", CRUSTDATA)
    assert first.raw == second.raw and loop.calls == 1
    assert ctx.ledger.spent == 0


async def test_a_repeat_the_cheap_check_caught_never_runs_the_loop(monkeypatch, services):
    await run(services, "priya@hirefast.in", "Priya Sharma")
    loop = use_loop(monkeypatch, services, Loop())
    signup = await run(services, "priya.sharma.dev@gmail.com", "Priya Sharma")
    assert signup["status"] == Status.HUMAN_QUEUE
    assert loop.calls == 0


async def test_a_clear_student_never_runs_the_loop(monkeypatch, services):
    loop = use_loop(monkeypatch, services, Loop())
    signup = await run(services, "rohan.codes@gmail.com", "Rohan Kulkarni")
    assert signup["status"] == Status.NURTURE
    assert loop.calls == 0
    assert db.credits_for(signup["id"]) <= 1


async def test_a_personal_email_with_no_name_gives_the_loop_nothing_to_go_on(monkeypatch, services):
    loop = use_loop(monkeypatch, services, Loop())
    signup = await run(services, "someone@gmail.com", "")
    assert signup["status"] == Status.AWAITING_INTENT
    assert loop.calls == 0


async def test_a_repeat_only_the_report_can_see_goes_to_a_person(monkeypatch, services):
    use_loop(monkeypatch, services, Loop())
    services.llm = StubLLM(output("recruiting", target_role="Backend Engineer"))
    await run(services)
    other = "unknown.person@brand-new-startup.xyz"
    use_loop(monkeypatch, services, Loop(report_for(other)))
    signup = await run(services, other, "Someone New")
    assert signup["status"] == Status.HUMAN_QUEUE
    assert signup["route"] == "Repeat identity"


async def test_a_student_only_the_report_can_see_gets_the_guide(monkeypatch, services):
    text = SAMPLE.replace('headline: "Engineering Manager at HireFast"', 'headline: "B.Tech student at IIT Kanpur"')
    use_loop(monkeypatch, services, Loop(text))
    services.llm = StubLLM(output("recruiting"))
    signup = await run(services)
    assert signup["status"] == Status.NURTURE
    assert signup.get("sales_alert") == "suppressed"


@pytest.mark.parametrize("loop", [
    Loop(error=RuntimeError("loop blew up")),
    Loop(text="this isn't a report"),
    Loop(text=report_for("someone.else@hirefast.in")),
], ids=["crash", "garbage", "wrong person"])
async def test_a_bad_report_falls_back_to_the_basic_builder(monkeypatch, services, loop):
    use_loop(monkeypatch, services, loop)
    signup = await run(services)
    assert signup["person"]["match_method"] == "name_and_company"
    assert "the basic builder took over" in notes(signup)
    assert signup["status"] not in (Status.FAILED_SAFE,)


async def test_a_late_report_falls_back_to_the_basic_builder(monkeypatch, services):
    use_loop(monkeypatch, services, Loop(delay=30))
    monkeypatch.setattr(services.settings, "profile_timeout_s", 0.05)
    signup = await run(services)
    assert "ran out of time" in notes(signup)
    assert signup["person"]["match_method"] == "name_and_company"


async def test_a_missing_loop_falls_back_to_the_basic_builder(monkeypatch, services):
    monkeypatch.setattr(services.settings, "profile_builder", "react")
    monkeypatch.setattr(services.settings, "fast_path", False)
    signup = await run(services)
    assert "the basic builder took over" in notes(signup)
    assert signup["person"]["enriched"] is True


async def test_an_unresolved_report_asks_when_nobody_else_knows_them_either(monkeypatch, services):
    other = "founder@brand-new-startup.xyz"
    text = report_for(other).replace('name: "Noor Ahmed"', 'name: ""')
    use_loop(monkeypatch, services, Loop(text))
    signup = await run(services, other, "Some Founder")
    assert signup["status"] == Status.AWAITING_INTENT


async def test_the_basic_builder_is_untouched_when_the_loop_is_off(services):
    signup = await run(services)
    assert signup["person"]["match_method"] == "name_and_company"
    assert "profile agent" not in notes(signup)


# ---- Engineers ----


async def test_an_engineer_gets_companies_in_their_market(services):
    services.llm = StubLLM(output("builder", icp_industry="Software Development", target_country="United States"))
    signup = await run(services, "alex@northwindai.io", "Alex Mercer")
    assert signup["status"] == Status.DELIVERED
    assert signup["playbook"] == "builder"
    assert signup["result_columns"] == ["Company", "Size", "Country"]
    assert signup["result_rows"] and all(r["url"].startswith("https://") for r in signup["result_rows"])
    assert signup["request"]["endpoint"] == "/company/search"
    assert "curl -X POST" in signup["email_html"]
    assert signup["sales_alert"] == "suppressed"


async def test_an_engineers_pills_make_a_real_second_call(services):
    services.llm = StubLLM(output("builder", icp_industry="Software Development"))
    signup = await run(services, "alex@northwindai.io", "Alex Mercer")
    assert 2 <= len(signup["pills"]) <= 3
    result = await run_pill(services, signup, signup["pills"][0])
    assert result["ok"] and result["kind"] == "companies"
    assert db.get_signup(signup["id"])["status"] == Status.ACTIVATED


def test_the_prompt_keeps_emails_about_work():
    prompt = system_prompt(CRUSTDATA)
    assert "Never mention their education" in prompt
    assert 'Rows starting "Caveat:"' in prompt
    assert "Judge by their role" in prompt


# ---- What reading a real email showed ----


async def test_reasons_keep_their_whole_sentence(services):
    claim = "You work hands-on as an engineer on database performance, so you would likely call an API directly rather than buy one."
    result = output("builder", icp_industry="Software Development")
    result.reasons[0].claim = claim
    services.llm = StubLLM(result)
    signup = await run(services, "alex@northwindai.io", "Alex Mercer")
    assert signup["decision"]["reasons"][0]["claim"] == claim
    assert claim in signup["email_html"]


async def test_the_email_says_exactly_what_was_searched(services):
    services.llm = StubLLM(output("builder", icp_industry="Software Development", target_country="United States"))
    signup = await run(services, "alex@northwindai.io", "Alex Mercer")
    label = signup["attempts"][-1]["label"]
    assert f"What we searched: {label}" in signup["email_html"]
    assert "Software Development companies" in signup["attempts"][0]["label"]
    assert "in your market" not in signup["email_subject"], "we filtered on industry, not on their market"


async def test_citations_dont_recite_the_company_back_to_them(monkeypatch, services):
    long_fact = "HireFast runs recruiting software for Indian startups " + "and much more besides " * 12
    use_loop(monkeypatch, services, Loop(SAMPLE.replace("HireFast runs recruiting software for Indian startups.", long_fact)))
    result = output("recruiting", target_role="Backend Engineer")
    services.llm = StubLLM(result)
    signup = await run(services)
    cited = next(r for r in services.llm.rows if r["value"].startswith("HireFast runs"))
    result.opening.evidence_ids = [cited["id"]]
    result.reasons[0].evidence_ids = [cited["id"]]
    again = await run(services, "second.signup@hirefast.in", "Second Person")
    assert "and much more besides and much more besides and much more besides" not in again["email_html"]


def test_company_tables_show_country_names():
    from app.playbooks.base import company_rows

    rows = company_rows({"companies": [{"basic_info": {"name": "Acme"}, "locations": {"country": "IND"}}]})
    assert rows[0]["company"] == "India"


def test_an_opening_cant_describe_more_than_the_filters_do():
    assert "never say what the results build" in system_prompt(CRUSTDATA)


# ---- Falling back, out loud ----


@pytest.mark.parametrize("loop,expected", [
    (Loop(error=RuntimeError("loop blew up")), "failed: RuntimeError: loop blew up"),
    (Loop(text="this isn't a report"), "couldn't be used: no front matter"),
    (Loop(text=report_for("someone.else@hirefast.in")), "about a different email"),
    (Loop(text=SAMPLE.replace('name: "Noor Ahmed"', 'name: ""')), "couldn't identify them"),
], ids=["crash", "garbage", "wrong person", "unresolved"])
async def test_every_fallback_is_flagged_with_its_reason(monkeypatch, services, loop, expected):
    use_loop(monkeypatch, services, loop)
    signup = await run(services)
    assert expected in signup["fallback_reason"]
    row = next(c for c in db.list_calls(signup["id"]) if c["kind"] == "fallback")
    assert expected in row["note"]


async def test_a_good_report_isnt_flagged(monkeypatch, services):
    use_loop(monkeypatch, services, Loop())
    services.llm = StubLLM(output("recruiting", target_role="Backend Engineer"))
    assert "fallback_reason" not in await run(services)


async def test_the_basic_builder_on_purpose_isnt_a_fallback(services):
    assert "fallback_reason" not in await run(services)


async def test_a_fallback_shows_in_the_summary_kpis_and_sales_brief(monkeypatch, services):
    from app import kpis
    from app.delivery.slack import blocks
    from app.pipeline.run import summary

    use_loop(monkeypatch, services, Loop(error=RuntimeError("loop blew up")))
    services.llm = StubLLM(output("recruiting", target_role="Backend Engineer"))
    signup = await run(services)
    assert summary(signup)["fallback"]
    assert kpis.compute()["fallbacks"] == 1
    assert "loop blew up" in signup["brief"]["fallback"]
    assert any("Basic profile only" in str(block) for block in blocks(signup["brief"]))


async def test_a_signups_own_email_never_mentions_a_fallback(monkeypatch, services):
    use_loop(monkeypatch, services, Loop(error=RuntimeError("loop blew up")))
    services.llm = StubLLM(output("recruiting", target_role="Backend Engineer"))
    signup = await run(services)
    assert "loop blew up" not in signup["email_html"] and "fallback" not in signup["email_html"].lower()


# ---- Queuing ----


async def signups_at_once(services, count):
    emails = [f"person{i}@brand-new-startup.xyz" for i in range(count)]
    return await asyncio.gather(*(run(services, email, "Noor Khan") for email in emails))


class CountingLoop(Loop):
    running = 0
    most = 0

    async def build_report(self, client, ctx, email, name, persona):
        CountingLoop.running += 1
        CountingLoop.most = max(CountingLoop.most, CountingLoop.running)
        await asyncio.sleep(0.05)
        CountingLoop.running -= 1
        return report_for(email)


async def test_only_one_agent_runs_at_a_time(monkeypatch, services):
    CountingLoop.most = 0
    use_loop(monkeypatch, services, CountingLoop())
    await signups_at_once(services, 3)
    assert CountingLoop.most == 1, "a shared balance can't tell two runs apart"


async def test_waiting_in_the_queue_doesnt_use_up_the_time_limit(monkeypatch, services):
    """Three signups in 80 seconds lost all three reports, because the queue counted against the limit."""
    use_loop(monkeypatch, services, CountingLoop())
    monkeypatch.setattr(services.settings, "profile_timeout_s", 0.1)  # Longer than one run, shorter than three.
    results = await signups_at_once(services, 3)
    assert not [s for s in results if s.get("fallback_reason")]


async def test_a_full_queue_falls_back_straight_away_and_says_so(monkeypatch, services):
    use_loop(monkeypatch, services, CountingLoop())
    monkeypatch.setattr(services.settings, "report_queue_max", 1)
    results = await signups_at_once(services, 4)
    waited_out = [s for s in results if "already waiting" in s.get("fallback_reason", "")]
    assert len(waited_out) == 2


async def test_the_fallback_picks_the_job_at_the_emails_company(services):
    """A real profile listed a council seat first, and the sales brief called that their job."""
    simulator = services.client.http._transport
    real_enrich = simulator.routes["/person/enrich"]

    def two_roles(body):
        response = real_enrich(body)
        data = response.json()
        data[0]["matches"][0]["person_data"]["experience"]["employment_details"]["current"] = [
            {"name": "Forbes", "title": "Council Member", "company_website_domain": "forbes.com"},
            {"name": "HireFast", "title": "VP Engineering", "company_website_domain": "hirefast.in"},
        ]
        return httpx.Response(200, json=data, headers=dict(response.headers))

    simulator.routes["/person/enrich"] = two_roles
    signup = await run(services, "priya@hirefast.in", "Priya Sharma")
    assert signup["person"]["title"] == "VP Engineering"
    assert signup["person"]["employer"] == "HireFast"
