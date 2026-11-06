"""CrustFlow works for a vendor, and the vendor's persona shapes everything Claude decides."""

import json

import pytest
from pydantic import ValidationError

from app import db
from app.llm import LLM, LLMFailure, LLMOutput, output_schema, system_prompt
from app.models import EvidenceTable, Status
from app.personas import VendorPersona, load_personas
from app.pipeline.run import process_intent, process_signup
from app.playbooks.gtm import ICP_INDUSTRIES
from app.services import build_services
from tests.conftest import make_settings

PERSONAS = load_personas()
CRUSTDATA, SUPABASE = PERSONAS["crustdata"], PERSONAS["supabase"]


def output(segment, *, opening="Here is where to start.", opening_ids=("E1",), confidence=0.9, subject="", picks=None, **slots):
    return LLMOutput.model_validate({
        "segment": segment, "confidence": confidence,
        "opening": {"claim": opening, "evidence_ids": list(opening_ids)},
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
        self.result = result
        self.personas = []

    async def classify(self, signup_id, table, domain, persona):
        self.personas.append(persona.id)
        return self.result


async def run(services, raw, name="", persona=""):
    signup = db.create_signup(raw, name=name, persona_id=persona)
    await process_signup(services, signup["id"])
    return db.get_signup(signup["id"])


def endpoints(signup):
    return [c["endpoint"] for c in db.list_calls(signup["id"])]


# ---- The persona itself ----


def test_shipped_personas_load():
    assert {"crustdata", "supabase"} <= set(PERSONAS)


def test_a_repeated_segment_is_refused():
    data = json.loads(SUPABASE.model_dump_json())
    data["segments"].append(data["segments"][0])
    with pytest.raises(ValidationError):
        VendorPersona.model_validate(data)


def test_unknown_is_a_reserved_segment():
    data = json.loads(SUPABASE.model_dump_json())
    data["segments"][0]["id"] = "unknown"
    with pytest.raises(ValidationError):
        VendorPersona.model_validate(data)


def test_a_broken_persona_file_stops_startup(tmp_path):
    (tmp_path / "bad.json").write_text('{"id": "bad"}')
    with pytest.raises(ValidationError):
        load_personas(tmp_path)


def test_an_unknown_vendor_stops_startup(tmp_path):
    with pytest.raises(ValueError, match="VENDOR_PERSONA"):
        build_services(make_settings(tmp_path, vendor_persona="nobody"))


# ---- What Claude is told ----


def test_the_prompt_describes_the_vendor():
    prompt = system_prompt(SUPABASE)
    assert SUPABASE.product in prompt
    assert all(s.look_for in prompt for s in SUPABASE.segments)
    assert "Crustdata" not in prompt


def test_the_schema_allows_only_this_vendors_segments():
    assert output_schema(SUPABASE)["properties"]["segment"]["enum"] == [*(s.id for s in SUPABASE.segments), "unknown"]
    assert "recruiting" in output_schema(CRUSTDATA)["properties"]["segment"]["enum"]


def test_only_a_gtm_vendor_gets_the_industry_list():
    crustdata = output_schema(CRUSTDATA)["$defs"]["LLMSlots"]["properties"]["icp_industry"]
    assert set(ICP_INDUSTRIES) <= set(crustdata["enum"])
    assert "enum" not in output_schema(SUPABASE)["$defs"]["LLMSlots"]["properties"]["icp_industry"]


async def test_a_segment_from_another_vendor_is_rejected(services):
    class Messages:
        async def create(self, **_):
            class Block:
                type = "text"
                text = output("recruiting").model_dump_json()

            class Response:
                stop_reason = "end_turn"
                content = [Block()]

            return Response()

    class Beta:
        messages = Messages()

    class Client:
        beta = Beta()

    table = EvidenceTable()
    table.add("person/search", "basic_profile.headline", "Engineer", "2026-10-20")
    with pytest.raises(LLMFailure, match="unknown segment"):
        await LLM(services.settings, client=Client()).classify(None, table, "acme.com", SUPABASE)


# ---- What each vendor sends ----


async def test_a_message_only_vendor_runs_no_search(services):
    services.llm = StubLLM(output("product_teams", opening="You can dictate prompts instead of typing them."))
    signup = await run(services, "priya@hirefast.in", "Priya Sharma", "supabase")
    assert services.llm.personas == ["supabase"]
    assert signup["status"] == Status.DELIVERED
    assert signup["playbook"] == "message"
    calls = endpoints(signup)
    assert "/company/search" not in calls and "/person/search/autocomplete" not in calls
    assert not signup.get("sandbox_token") and not signup.get("result_rows")
    html = signup["email_html"]
    assert "You can dictate prompts" in html and "supabase.com" in html and "Supabase" in html
    assert "Crustdata search" not in html


async def test_the_same_signup_gets_a_different_email_per_vendor(services):
    services.llm = StubLLM(output("recruiting", target_role="Senior Backend Engineer"))
    crustdata = await run(services, "priya@hirefast.in", "Priya Sharma", "crustdata")
    services.llm = StubLLM(output("larger_companies"))
    wispr = await run(services, "priya@hirefast.in", "Priya Sharma", "supabase")
    assert crustdata["playbook"] == "recruiting" and crustdata["result_rows"]
    assert wispr["playbook"] == "message"
    assert wispr["status"] == Status.DELIVERED, "a repeat for one vendor isn't a repeat for another"


async def test_a_high_fit_message_segment_alerts_with_no_credit_estimate(services):
    services.llm = StubLLM(output("larger_companies"))
    signup = await run(services, "priya@hirefast.in", "Priya Sharma", "supabase")
    assert signup["sales_alert"] == "sent"
    assert signup["brief"]["credits_per_month"] is None


async def test_claudes_opening_leads_the_results_email(services):
    services.llm = StubLLM(output("recruiting", opening="Here are engineers you could call this week.", target_role="Backend Engineer"))
    signup = await run(services, "priya@hirefast.in", "Priya Sharma")
    assert "Here are engineers you could call this week." in signup["email_html"]
    assert signup["decision"]["opening"]["citations"]


@pytest.mark.parametrize("opening,ids", [
    ("A claim with a made-up source.", ("E99",)),
    ("A claim with no source.", ()),
    ("Reach Priya at priya@hirefast.in today.", ("E1",)),
    ("Call them on +91 98765 43210 now.", ("E1",)),
    ("x" * 500, ("E1",)),
])
async def test_a_bad_opening_is_dropped_and_the_email_still_goes(services, opening, ids):
    services.llm = StubLLM(output("larger_companies", opening=opening, opening_ids=ids))
    signup = await run(services, "priya@hirefast.in", "Priya Sharma", "supabase")
    assert signup["decision"]["opening"] is None
    assert signup["status"] == Status.DELIVERED
    assert SUPABASE.segment("larger_companies").first_value in signup["email_html"]
    assert "98765" not in signup["email_html"] and "made-up" not in signup["email_html"]


async def test_rules_never_pick_a_segment_the_vendor_doesnt_have(services):
    signup = await run(services, "priya@hirefast.in", "Priya Sharma", "supabase")
    assert signup["status"] == Status.AWAITING_INTENT, "Claude is off, and rules only know Crustdata's segments"


async def test_buttons_come_from_the_persona(services):
    signup = await run(services, "someone@gmail.com", "", "supabase")
    html = signup["email_html"]
    assert "Building with AI" in html and "Recruiting" not in html
    assert "What are you building with Supabase?" == signup["email_subject"]
    await process_intent(services, signup["id"], "product_teams")
    updated = db.get_signup(signup["id"])
    assert updated["status"] == Status.DELIVERED and updated["playbook"] == "message"


async def test_gates_follow_the_persona(services):
    own = await run(services, "jane@supabase.com", "", "supabase")
    assert own["status"] == Status.INTERNAL
    crustdata_address = await run(services, "jane@crustdata.com", "", "supabase")
    assert crustdata_address["status"] != Status.INTERNAL
    not_a_rival = await run(services, "sam@zoominfo.com", "", "supabase")
    assert not_a_rival["route"] != "Competitor"


async def test_student_rule_only_applies_to_vendors_with_that_segment(services):
    signup = await run(services, "rohan.codes@gmail.com", "Rohan Kulkarni", "supabase")
    assert signup["status"] != Status.NURTURE


# ---- Relevance for GTM ----


async def test_gtm_searches_the_buyers_title_directly(services):
    """Five companies rarely have the exact title, so the market gets searched first."""
    services.llm = StubLLM(output("gtm", icp_industry="Software Development", buyer_title="Account Executive"))
    signup = await run(services, "alex@northwindai.io", "Alex Mercer")
    assert signup["attempts"][0]["endpoint"] == "/person/search"
    assert "/company/search" not in endpoints(signup), "enough buyers means no second search to pay for"
    assert signup["result_rows"] and "buyers at" not in signup["email_subject"]
    assert not signup.get("companies")
    for pill in signup["pills"]:
        assert pill["id"] != "sales_titles", "a sales filter would fight the buyer title"


async def test_a_rare_buyer_title_stays_in_the_right_market(services):
    """A real run sent a shipping specialist as a "buyer". Seniors in the market is the honest fallback."""
    services.llm = StubLLM(output("gtm", icp_industry="Software Development", buyer_title="Chief Unicorn Officer"))
    signup = await run(services, "alex@northwindai.io", "Alex Mercer")
    assert signup["attempts"][-1]["label"].startswith("Senior decision-makers in Software Development")
    assert "/company/search" not in endpoints(signup)
    assert signup["result_rows"]
    assert "widened" in signup["email_html"] or "wider one" in signup["email_html"]


async def test_gtm_with_no_buyer_title_still_finds_companies_first(services):
    services.llm = StubLLM(output("gtm", icp_industry="Software Development"))
    signup = await run(services, "alex@northwindai.io", "Alex Mercer")
    assert "/company/search" in endpoints(signup)
    assert signup["result_rows"]
    assert db.credits_for(signup["id"]) <= services.settings.budget_standard


async def test_an_email_never_claims_a_market_fit_it_dropped(services):
    services.llm = StubLLM(output("gtm", icp_industry="Wholesale"))
    signup = await run(services, "alex@northwindai.io", "Alex Mercer")
    if any("any industry" in a["label"] for a in signup["attempts"]):
        assert "that fit your market" not in signup["email_subject"]
        assert "any industry" in signup["email_html"]


# ---- What the real run taught us ----


async def test_a_doubtful_student_guess_asks_instead(services):
    """Claude once called an intern a student at 55%, under the bar, and the bar was skipped."""
    services.llm = StubLLM(output("student", confidence=0.55))
    signup = await run(services, "priya@hirefast.in", "Priya Sharma")
    assert signup["status"] == Status.AWAITING_INTENT


def test_an_opening_can_only_claim_what_is_true_when_it_is_written():
    assert "Nothing has been done or set up" in system_prompt(SUPABASE)
    assert "never promise" in system_prompt(CRUSTDATA)
    assert "ran a first search" not in system_prompt(SUPABASE)


def test_a_vendor_with_no_search_is_not_told_about_search_slots():
    assert "target_role" not in system_prompt(SUPABASE)


async def test_a_button_click_never_claims_to_know_their_market(services):
    signup = await run(services, "someone@gmail.com")
    await process_intent(services, signup["id"], "gtm")
    assert "that fit your market" not in db.get_signup(signup["id"])["email_subject"]
