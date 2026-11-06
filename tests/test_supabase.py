"""The demo: a company's services and a person's profile become one personal email."""

import asyncio
import json
import sys
import types
from pathlib import Path

import httpx
import pytest

from app import db
from app.delivery.mail import Mailer
from app.llm import LLMOutput, output_schema, system_prompt
from app.models import Status
from app.personas import load_personas
from app.pipeline import react_profiles
from app.pipeline.run import process_intent, process_signup
from tests.conftest import make_settings

SUPABASE = load_personas()["supabase"]
OPENING = "Your team ships AI search, so the quickest wins sit close to your data."


def output(segment="ai_builders", picks=None, subject="Vectors next to your data", opening=OPENING, confidence=0.9):
    return LLMOutput.model_validate({
        "segment": segment, "confidence": confidence, "subject": subject,
        "opening": {"claim": opening, "evidence_ids": ["E1"]},
        "reasons": [{"claim": "You build AI features.", "evidence_ids": ["E1"]}],
        "picks": [{"service": "vector", "why": "You work on retrieval, so embeddings can sit beside your data.", "evidence_ids": ["E1"]}] if picks is None else picks,
        "slots": {k: "" for k in ("target_role", "target_city", "target_country", "icp_industry", "buyer_title", "research_field")},
    })


class StubLLM:
    available = True
    fail_next = 0

    def __init__(self, result):
        self.result = result

    async def classify(self, signup_id, table, domain, persona):
        return self.result


async def run(services, raw="priya@hirefast.in", name="Priya Sharma"):
    signup = db.create_signup(raw, name=name, persona_id="supabase")
    await process_signup(services, signup["id"])
    return db.get_signup(signup["id"])


# ---- The company's information ----


def test_every_service_has_a_real_looking_link_on_their_own_site():
    assert len(SUPABASE.services) >= 10
    assert all(s.url.startswith("https://supabase.com/") for s in SUPABASE.services)


def test_claude_is_told_everything_the_company_offers():
    prompt = system_prompt(SUPABASE)
    assert all(s.name in prompt and s.what in prompt for s in SUPABASE.services)
    assert "Nothing has been done or set up" in prompt


def test_claude_can_only_pick_real_services():
    schema = output_schema(SUPABASE)
    assert schema["$defs"]["LLMPick"]["properties"]["service"]["enum"] == [s.id for s in SUPABASE.services]


def test_a_vendor_without_services_is_told_to_leave_picks_empty():
    assert "Leave picks empty" in system_prompt(load_personas()["crustdata"])


# ---- The email ----


async def test_the_email_carries_the_opening_the_picks_and_their_sources(services):
    services.llm = StubLLM(output())
    signup = await run(services)
    html = signup["email_html"]
    assert signup["status"] == Status.DELIVERED and signup["playbook"] == "message"
    assert signup["email_subject"] == "Vectors next to your data"
    assert OPENING in html
    assert "Vector and AI toolkit" in html and "https://supabase.com/docs/guides/ai" in html
    assert "You work on retrieval" in html
    assert "Tailored from:" in html
    assert "Start your project" in html
    assert "This is a sample for a demo" in html and "for Supabase" in html


async def test_different_people_get_different_services(services):
    services.llm = StubLLM(output())
    first = await run(services)
    services.llm = StubLLM(output("larger_companies", picks=[
        {"service": "compliance", "why": "Your team will need a security review.", "evidence_ids": ["E1"]},
    ]))
    second = await run(services, "alex@northwindai.io", "Alex Mercer")
    assert "Vector and AI toolkit" in first["email_html"] and "Vector and AI toolkit" not in second["email_html"]
    assert "SOC 2 and HIPAA" in second["email_html"]
    assert second["sales_alert"] == "sent", "larger companies go to sales"


@pytest.mark.parametrize("pick", [
    {"service": "made_up_feature", "why": "It fits.", "evidence_ids": ["E1"]},
    {"service": "vector", "why": "It fits.", "evidence_ids": ["E99"]},
    {"service": "vector", "why": "It fits.", "evidence_ids": []},
    {"service": "vector", "why": "Write to priya@hirefast.in about it.", "evidence_ids": ["E1"]},
], ids=["invented service", "invented source", "no source", "contact detail"])
async def test_a_bad_pick_never_reaches_the_email(services, pick):
    services.llm = StubLLM(output(picks=[pick]))
    signup = await run(services)
    assert signup["decision"]["picks"] == []
    assert signup["status"] == Status.DELIVERED, "the email still goes, built from the segment"
    assert "priya@hirefast.in" not in signup["email_html"]


async def test_links_come_from_the_company_not_from_claude(services):
    services.llm = StubLLM(output(picks=[
        {"service": "vector", "why": "See https://evil.example/phish for more.", "evidence_ids": ["E1"]},
    ]))
    signup = await run(services)
    assert 'href="https://evil.example' not in signup["email_html"]


async def test_a_repeated_pick_counts_once(services):
    pick = {"service": "vector", "why": "It fits your work.", "evidence_ids": ["E1"]}
    services.llm = StubLLM(output(picks=[pick, pick, pick]))
    assert len((await run(services))["decision"]["picks"]) == 1


async def test_a_subject_with_contact_details_is_replaced(services):
    services.llm = StubLLM(output(subject="Hi priya@hirefast.in"))
    assert (await run(services))["email_subject"] == "Your best first step with Supabase"


async def test_the_ask_email_uses_the_new_design_and_the_companys_choices(services):
    signup = await run(services, "someone@gmail.com", "")
    html = signup["email_html"]
    assert "Building with AI" in html and "Embeddings, RAG, agents" in html
    assert "Nothing runs until you choose" in html
    await process_intent(services, signup["id"], "ai_builders")
    updated = db.get_signup(signup["id"])
    assert updated["status"] == Status.DELIVERED
    assert SUPABASE.segment("ai_builders").first_value in updated["email_html"]


# ---- Sending to one inbox ----


def mailer(tmp_path, seen, **settings):
    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"id": "abc"})

    values = {"crustdata_mode": "live", "email_live": True, "email_api_key": "k", "email_from": "a@b.co", **settings}
    return Mailer(make_settings(tmp_path, **values), transport=httpx.MockTransport(handler))


async def test_redirect_sends_to_the_test_inbox_and_says_who_it_was_for(tmp_path):
    seen = []
    mail = mailer(tmp_path, seen, email_redirect_to="me@team.co", email_allowlist="me@team.co")
    result = await mail.send("real.person@company.com", "Hello", "<p>x</p>")
    assert seen[0]["to"] == ["me@team.co"], "the real person must never be addressed"
    assert seen[0]["subject"] == "[Test: real.person@company.com] Hello"
    assert result.status == "sent" and "redirected to me@team.co" in result.detail


async def test_a_redirect_address_off_the_allowlist_sends_nothing(tmp_path):
    seen = []
    mail = mailer(tmp_path, seen, email_redirect_to="me@team.co", email_allowlist="other@team.co")
    result = await mail.send("real.person@company.com", "Hello", "<p>x</p>")
    assert seen == [] and result.status == "rendered"


async def test_with_no_redirect_the_allowlist_still_blocks_real_people(tmp_path):
    seen = []
    result = await mailer(tmp_path, seen, email_allowlist="me@team.co").send("real.person@company.com", "Hello", "<p>x</p>")
    assert seen == [] and result.status == "rendered"


# ---- The bridge to the agent ----


class FakeBudget:
    spent = 0.0


class FakeAgent:
    behaviour = "ok"
    running = 0
    most_at_once = 0

    def __init__(self, settings, email, name):
        self.email, self.name, self.log, self.budget, self.saved = email, name, [], FakeBudget(), None

    async def run(self):
        FakeAgent.running += 1
        FakeAgent.most_at_once = max(FakeAgent.most_at_once, FakeAgent.running)
        self.log.append({"tool": "crustdata__execute", "crustdata_tools": ["person_search"], "allowed": True})
        await asyncio.sleep(0.03)
        FakeAgent.running -= 1
        if self.behaviour == "slow to close":
            self.saved = Path(FakeAgent.folder) / "user.md"
            self.saved.write_text("the finished report")
            self.budget.spent = 7.03
            await asyncio.sleep(30)
        if self.behaviour == "crash":
            self.budget.spent = 3.0
            raise RuntimeError("agent blew up")
        if self.behaviour == "no report":
            return {"user_md": None, "error": "gave up", "crustdata_credits": 1.5}
        path = Path(FakeAgent.folder) / "user.md"
        path.write_text("the report")
        return {"user_md": str(path), "crustdata_credits": 9.83, "seconds": 90, "claude_usd": 0.3}


@pytest.fixture
def live_client(services, monkeypatch, tmp_path):
    agent_module = types.ModuleType("profile_agent.agent")
    agent_module.ProfileAgent = FakeAgent
    config_module = types.ModuleType("profile_agent.config")
    config_module.settings = lambda **_: object()
    monkeypatch.setitem(sys.modules, "profile_agent.agent", agent_module)
    monkeypatch.setitem(sys.modules, "profile_agent.config", config_module)
    monkeypatch.setattr(FakeAgent, "behaviour", "ok")
    monkeypatch.setattr(FakeAgent, "folder", str(tmp_path), raising=False)
    monkeypatch.setattr(FakeAgent, "most_at_once", 0)
    monkeypatch.setattr(services.client.settings, "crustdata_mode", "live")
    return services.client


def ctx():
    from app.crustdata.client import Ledger, RunContext

    signup = db.create_signup("a@b.co")
    return RunContext(signup["id"], Ledger(cap=15))


async def test_the_bridge_returns_the_report_and_charges_the_true_total(live_client):
    run_ctx = ctx()
    assert await react_profiles.build_report(live_client, run_ctx, "a@b.co", "A Person", SUPABASE) == "the report"
    assert run_ctx.ledger.spent == 9.83, "the balance total, not the agent's incomplete table"
    calls = db.list_calls(run_ctx.signup_id)
    assert any("person_search" in (c["note"] or "") for c in calls), "the wait shouldn't be silent"


async def test_a_crash_still_charges_what_was_spent(live_client, monkeypatch):
    monkeypatch.setattr(FakeAgent, "behaviour", "crash")
    run_ctx = ctx()
    with pytest.raises(RuntimeError):
        await react_profiles.build_report(live_client, run_ctx, "a@b.co", "A Person", SUPABASE)
    assert run_ctx.ledger.spent == 3.0


async def test_a_report_saved_at_the_deadline_is_kept(live_client, monkeypatch):
    """A real run saved at 177 s and hit the limit while closing, wasting 7 credits of research."""
    monkeypatch.setattr(FakeAgent, "behaviour", "slow to close")
    run_ctx = ctx()
    async with asyncio.timeout(0.1):
        report = await react_profiles.build_report(live_client, run_ctx, "a@b.co", "A Person", SUPABASE)
    assert report == "the finished report"
    assert run_ctx.ledger.spent == 7.03


async def test_no_report_raises_so_the_basic_builder_takes_over(live_client, monkeypatch):
    monkeypatch.setattr(FakeAgent, "behaviour", "no report")
    run_ctx = ctx()
    with pytest.raises(react_profiles.AgentFailed):
        await react_profiles.build_report(live_client, run_ctx, "a@b.co", "A Person", SUPABASE)
    assert run_ctx.ledger.spent == 1.5


async def test_a_missing_key_never_stops_the_server(live_client, monkeypatch):
    def missing_key(**_):
        raise SystemExit("CRUSTDATA_API_KEY is missing from .env")

    monkeypatch.setattr(sys.modules["profile_agent.config"], "settings", missing_key)
    with pytest.raises(react_profiles.AgentFailed):
        await react_profiles.build_report(live_client, ctx(), "a@b.co", "A", SUPABASE)


async def test_the_agent_never_runs_on_simulated_data(services):
    with pytest.raises(react_profiles.AgentFailed):
        await react_profiles.build_report(services.client, ctx(), "a@b.co", "A", SUPABASE)


async def test_the_sources_line_reads_plainly(services):
    services.llm = StubLLM(output())
    html = (await run(services))["email_html"]
    assert "Tailored from: your public work profile" in html
    assert "basic_profile" not in html and "identity confirmed" not in html
