"""A clear name match gets its email in seconds. The agent is for the people we can't place."""

import asyncio
import dataclasses

import pytest

from app import db
from app.crustdata.client import CrustdataError, Ledger, RunContext
from app.models import Company, Decision, Person, Playbook, QueryOutcome, Slots, Status
from app.personas import load_personas
from app.pipeline import profile_doc, profiles, route
from app.pipeline.run import process_signup
from tests.test_report import EMAIL, SAMPLE, Loop, use_loop

SUPABASE = load_personas()["supabase"]


async def signup_for(services, email="priya@hirefast.in", name="Priya Sharma"):
    signup = db.create_signup(email, name=name)
    await process_signup(services, signup["id"])
    return db.get_signup(signup["id"])


def endpoints(signup):
    return [c["endpoint"] for c in db.list_calls(signup["id"]) if c["kind"] == "crustdata"]


async def test_a_clear_match_skips_the_agent(monkeypatch, services):
    loop = use_loop(monkeypatch, services, Loop())
    monkeypatch.setattr(services.settings, "fast_path", True)
    signup = await signup_for(services)
    assert loop.calls == 0
    assert signup["status"] == Status.DELIVERED
    assert not signup.get("fallback_reason"), "Skipping the agent on purpose isn't a fallback"
    called = endpoints(signup)
    assert {"/person/enrich", "/company/enrich", "/job/search"} <= set(called)


async def test_nobody_found_still_gets_the_agent(monkeypatch, services):
    loop = use_loop(monkeypatch, services, Loop())
    monkeypatch.setattr(services.settings, "fast_path", True)
    await signup_for(services, EMAIL, "Somebody Unlisted")
    assert loop.calls == 1


async def test_a_saved_report_is_still_used_on_the_fast_path(monkeypatch, services):
    """It's free and richer, and it lets a demo use a report built ahead of time."""
    loop = use_loop(monkeypatch, services, Loop())
    monkeypatch.setattr(services.settings, "fast_path", True)
    db.cache_set(f"report:{EMAIL}", SAMPLE)
    signup = await signup_for(services, EMAIL, "Noor Khan")
    assert loop.calls == 0
    assert signup.get("profile_report")


async def test_the_research_runs_at_the_same_time(monkeypatch, services):
    """Three waits in a row is most of what made signups slow."""
    running, most = 0, 0

    async def post(path, body, ctx, **kwargs):
        nonlocal running, most
        running += 1
        most = max(most, running)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        running -= 1
        raise CrustdataError(500, "nope", path)

    monkeypatch.setattr(services.client, "post", post)
    person = Person(status="matched", profile_url="https://example.com/in/x")
    company = Company(matched=True, name="Hirefast", domain="hirefast.in")
    await profiles.deepen(services.client, RunContext(None, Ledger(cap=15)), person, company, SUPABASE)
    assert most == 3, "Person, company and jobs should overlap"


async def test_one_failed_lookup_doesnt_lose_the_others(services):
    services.client.faults.add("/person/enrich", 500, 5)
    signup = await signup_for(services)
    assert signup["status"] in (Status.DELIVERED, Status.AWAITING_INTENT)
    assert "/company/enrich" in endpoints(signup)


# ---- A report with blanks ----


def blank_report(**front):
    fields = {"crustdata_person_id": "", "linkedin_url": "", "headline": "", "current_title": "", "company_domain": "", "location": ""}
    text = SAMPLE
    for line in text.split("---\n")[1].splitlines():
        key = line.split(":")[0]
        if key in fields:
            text = text.replace(line, f'{key}: "{front.get(key, "")}"')
    return profile_doc.parse(text, EMAIL)


def precheck():
    return Person(
        status="matched", confidence=0.9, person_id=9010, name="Noor Ahmed", headline="Staff Engineer", title="Staff Engineer",
        city="Pune", country="India", profile_url="https://example.com/in/noor",
    )


def test_blanks_in_a_report_keep_what_the_name_check_found():
    company = Company(matched=True, name="Hirefast", domain="hirefast.in")
    person, kept = profile_doc.apply(blank_report(), precheck(), company, "hirefast.in", False)
    assert (person.person_id, person.title, person.city, person.profile_url) == (9010, "Staff Engineer", "Pune", "https://example.com/in/noor")
    assert person.confidence == profile_doc.BOTH_FOUND_THEM, "Two searches agree, so this must not drop to asking"
    assert kept is company, "A blank employer domain must not erase the company we found"


def test_a_report_about_someone_else_borrows_nothing():
    other = dataclasses.replace(precheck(), person_id=4444, name="Someone Else")
    person, _ = profile_doc.apply(blank_report(crustdata_person_id="5555"), other, Company(), "hirefast.in", False)
    assert person.title == "" and person.profile_url == ""


def test_commit_history_stays_out_of_the_summary():
    text = "Staff Engineer at Acme, building storage; 271 GitHub commits on the core. Bengaluru based."
    assert profile_doc.work_summary(text) == "Staff Engineer at Acme, building storage; Bengaluru based."


@pytest.mark.parametrize("fact", [
    profile_doc.Fact("Posted about their weekend trip", "web", "https://x.com/someone/status/1", "2026-11-01"),
    profile_doc.Fact("Posted about their weekend trip", "web", "https://www.twitter.com/someone/status/1"),
    profile_doc.Fact("Recent tweet about hiring", "web_search", "query"),
    profile_doc.Fact("A post about a conference", "social_post_search", "post.text"),
    profile_doc.Fact("271 commits", "email_lookup", "github"),
], ids=["x", "twitter", "tweet", "post search", "commits"])
def test_their_own_posts_never_reach_claude(fact):
    assert profile_doc._is_personal_trail(fact)


@pytest.mark.parametrize("fact", [
    profile_doc.Fact("Raised a seed round", "web", "https://techcrunch.com/a"),
    profile_doc.Fact("Launched a new product", "web", "https://netflix.com/news"),
    profile_doc.Fact("Staff Engineer", "person_search", "experience.current"),
], ids=["press", "domain ending in x.com", "work fact"])
def test_work_facts_still_do(fact):
    assert not profile_doc._is_personal_trail(fact)


def test_the_real_agent_package_imports():
    """Everything else stubs the agent, so without this a broken file passes every test."""
    from profile_agent.agent import ProfileAgent

    assert ProfileAgent


# ---- The sales brief ----


def decision():
    return Decision(Playbook.MESSAGE, 0.9, [], Slots(), "llm", segment="larger_companies")


def test_a_board_seat_isnt_the_title_sales_leads_with():
    person = Person(name="A Person", title="Council Member", headline="CTO at Northwind | Regional Tech Council Member")
    company = Company(name="Northwind", headcount=900, headcount_growth_6m=-1.34567)
    brief = route.build_brief(person, company, decision(), QueryOutcome(Playbook.MESSAGE, None, []), "a@northwind.example")
    assert brief["who"] == "A Person, CTO, Northwind (900 people, -1.3% in 6 months)"


def test_a_real_title_is_left_alone():
    assert route.job_title_for_sales(Person(title="Head of Platform", headline="anything"), Company(name="Acme")) == "Head of Platform"


def test_no_size_means_no_brackets():
    brief = route.build_brief(Person(name="A", title="CTO"), Company(name="Acme"), decision(), QueryOutcome(Playbook.MESSAGE, None, []), "a@acme.com")
    assert brief["who"] == "A, CTO, Acme"


def test_a_message_email_still_gets_a_dated_signal():
    jobs = [
        {"title": "Older Role", "location": "", "date_added": "2026-09-01"},
        {"title": "Staff Engineer", "location": "Remote", "date_added": "2026-10-20"},
        {"title": "Undated", "location": "", "date_added": ""},
    ]
    assert route.freshest_signal([], jobs) == "Hiring: Staff Engineer in Remote, posted 2026-10-20 (job/search)"


@pytest.mark.parametrize("jobs", [None, [], [{"title": "Undated", "date_added": ""}]])
def test_no_dates_says_so(jobs):
    assert route.freshest_signal([], jobs) == "No dated signal in these results"
