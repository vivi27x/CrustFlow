"""End-to-end runs of the agent against the simulator: the test matrix and the spec's criteria."""

import asyncio
import re

import pytest

from app import db
from app.models import Status
from app.pipeline.run import process_intent, process_signup, run_pill


NAMES = {
    "priya.sharma.dev@gmail.com": "Priya Sharma",
    "rohan.codes@gmail.com": "Rohan Kulkarni",
    "meera.ops@gmail.com": "Meera Joshi",
    "private.person@gmail.com": "Private Person",
}


async def run(services, raw, name=None):
    signup = db.create_signup(raw, name=NAMES.get(raw, "") if name is None else name)
    await process_signup(services, signup["id"])
    return db.get_signup(signup["id"])


def credits(signup):
    return db.credits_for(signup["id"])


async def test_work_email_full_flow(services):
    signup = await run(services, "priya@hirefast.in")
    assert signup["status"] == Status.DELIVERED
    assert signup["playbook"] == "recruiting"
    assert len(signup["result_rows"]) == 10
    assert credits(signup) <= services.settings.budget_standard
    assert signup["sales_alert"] == "sent"
    assert "brief" in signup and signup["brief"]["credits_per_month"] > 0
    assert signup["brief"]["reasons"], "the sales card must carry cited reasons"
    for reason in signup["decision"]["reasons"]:
        assert reason["citations"], "every reason needs a citation"
    assert "curl -X POST" in signup["email_html"] and "import requests" in signup["email_html"]
    assert signup["sandbox_token"] in signup["email_html"]
    assert 2 <= len(signup["pills"]) <= 3


async def test_relax_is_logged_when_strict_query_is_thin(services):
    signup = await run(services, "priya@hirefast.in")
    notes = [c["note"] for c in db.list_calls(signup["id"])]
    assert signup["relaxed"], "Senior Backend Engineer in Bengaluru is thin in the simulator"
    assert any("Relaxing to" in n for n in notes)
    assert len(signup["attempts"]) <= 3, "at most 2 retries"


async def test_snap_is_logged(services):
    signup = await run(services, "priya@hirefast.in")
    calls = db.list_calls(signup["id"])
    assert any(c["endpoint"].endswith("/autocomplete") for c in calls)


async def test_emailed_code_reproduces_emailed_rows(services):
    """The delivered request, sent again, must give the same people."""
    signup = await run(services, "priya@hirefast.in")
    request = signup["request"]
    from app.crustdata.client import Ledger, RunContext
    from app.playbooks.base import person_rows

    ctx = RunContext(None, Ledger(cap=100))
    response = await services.client.post(request["endpoint"], request["body"], ctx)
    assert [r["name"] for r in person_rows(response.data)] == [r["a"] for r in signup["result_rows"]]


async def test_repeat_identity_second_email_goes_to_human(services):
    first = await run(services, "priya@hirefast.in")
    second = await run(services, "priya.sharma.dev@gmail.com")
    assert first["status"] == Status.DELIVERED
    assert second["status"] == Status.HUMAN_QUEUE
    assert second["route"] == "Repeat identity"
    assert "9001" in second["route_detail"]
    assert credits(second) <= 1, "no proof of concept for a repeat"
    assert "email_html" not in second


async def test_simultaneous_signups_for_one_person_let_exactly_one_through(services):
    """Both finish identify before either is stored, which is what a plain lookup gets wrong."""
    a = db.create_signup("priya@hirefast.in", name="Priya Sharma")
    b = db.create_signup("priya.sharma.dev@gmail.com", name="Priya Sharma")
    await asyncio.gather(process_signup(services, a["id"]), process_signup(services, b["id"]))
    statuses = sorted(db.get_signup(x["id"])["status"] for x in (a, b))
    assert statuses == ["delivered", "human_queue"]


async def test_alias_of_earlier_signup_is_caught_for_free(services):
    await run(services, "priya@hirefast.in")
    alias = await run(services, "Priya+promo@HireFast.in")
    assert alias["status"] == Status.HUMAN_QUEUE
    assert alias["route"] == "Repeat signup"
    assert credits(alias) == 0
    assert db.list_calls(alias["id"]) == []


async def test_student_gmail_gets_guide_and_no_sales(services):
    signup = await run(services, "rohan.codes@gmail.com")
    assert signup["status"] == Status.NURTURE
    assert signup["playbook"] == "student"
    assert signup["route"] == "Self-serve nurture, 0 rep hours"
    assert signup["sales_alert"] == "suppressed"
    assert credits(signup) <= 1
    assert "/company/enrich" not in [c["endpoint"] for c in db.list_calls(signup["id"])]


async def test_student_on_academic_domain(services):
    signup = await run(services, "rohan.verma@iitk.ac.in")
    assert signup["status"] == Status.NURTURE
    assert credits(signup) <= 1


async def test_faculty_is_never_treated_as_student(services):
    signup = await run(services, "prof.rao@iitk.ac.in")
    assert signup["status"] == Status.DELIVERED
    assert signup["playbook"] == "academic"
    assert signup["result_rows"]
    assert credits(signup) <= services.settings.budget_standard


async def test_unknown_academic_address_is_asked_not_labelled_student(services):
    signup = await run(services, "nobody.here@iitk.ac.in")
    assert signup["status"] == Status.AWAITING_INTENT
    assert signup["playbook"] == "unknown"


async def test_internal_domain(services):
    signup = await run(services, "jane@crustdata.com")
    assert signup["status"] == Status.INTERNAL
    assert credits(signup) == 0
    assert db.list_calls(signup["id"]) == [] or all(c["kind"] == "delivery" for c in db.list_calls(signup["id"]))


async def test_disposable_domain_goes_to_human_queue(services):
    signup = await run(services, "throwaway@mailinator.com")
    assert signup["status"] == Status.HUMAN_QUEUE
    assert credits(signup) == 0
    assert any(i["signup_id"] == signup["id"] for i in db.list_human_items())


async def test_competitor_goes_to_human_queue(services):
    signup = await run(services, "sam@zoominfo.com")
    assert signup["status"] == Status.HUMAN_QUEUE
    assert signup["route"] == "Competitor"
    assert credits(signup) == 0


@pytest.mark.parametrize("raw", ["", "   ", "not-an-email", "a@b", "@hirefast.in", "priya@", "a b@c.com"])
async def test_malformed_email_is_rejected_with_no_calls(services, raw):
    signup = await run(services, raw)
    assert signup["status"] == Status.REJECTED
    assert db.list_calls(signup["id"]) == []


async def test_brand_new_domain_is_asked(services):
    signup = await run(services, "founder@brand-new-startup.xyz")
    assert signup["status"] == Status.AWAITING_INTENT
    assert "Recruiting" in signup["email_html"] and "Investing" in signup["email_html"]
    assert credits(signup) <= 1


async def test_unmatched_gmail_is_asked(services):
    signup = await run(services, "random.person.123@gmail.com")
    assert signup["status"] == Status.AWAITING_INTENT
    assert credits(signup) <= 1


async def test_thin_evidence_is_asked_not_guessed(services):
    signup = await run(services, "dana@quietledger.io")
    assert signup["status"] == Status.AWAITING_INTENT


async def test_redacted_person_stops_everything(services):
    signup = await run(services, "private.person@gmail.com")
    assert signup["status"] == Status.WITHDRAWN
    assert "email_html" not in signup


async def test_matched_gmail_finds_employer_and_is_served(services):
    signup = await run(services, "meera.ops@gmail.com")
    assert signup["status"] == Status.DELIVERED
    assert signup["playbook"] == "gtm"
    assert signup["company"]["name"] == "Cedarbyte"
    assert credits(signup) <= services.settings.budget_standard


async def test_gtm_flow_has_companies_and_buyers(services):
    signup = await run(services, "alex@northwindai.io")
    assert signup["status"] == Status.DELIVERED
    assert signup["playbook"] == "gtm"
    assert 1 <= len(signup["companies"]) <= 5
    assert signup["result_rows"]
    assert credits(signup) <= services.settings.budget_standard
    assert "northwindai.io" not in [c["domain"] for c in signup["companies"]], "never sell them their own company"


async def test_pill_activates_and_a_second_click_is_free(services):
    signup = await run(services, "priya@hirefast.in")
    pill = signup["pills"][0]
    result = await run_pill(services, signup, pill)
    assert result["ok"] and result["credits"] > 0
    updated = db.get_signup(signup["id"])
    assert updated["status"] == Status.ACTIVATED
    again = await run_pill(services, updated, pill)
    assert again["cached"] and again["credits"] == 0
    assert credits(signup) <= 8, "pills are tracked apart from the agent's own cap"


async def test_every_generated_pill_is_a_valid_request(services):
    """Pills are built ahead of time, so a bad one would only show up on a live click."""
    for email in ("priya@hirefast.in", "alex@northwindai.io", "prof.rao@iitk.ac.in"):
        signup = await run(services, email)
        for pill in signup["pills"]:
            result = await run_pill(services, signup, pill)
            assert result["ok"], (email, pill["id"], result)


async def test_pills_are_limited_per_sandbox(services):
    signup = await run(services, "priya@hirefast.in")
    for pill in signup["pills"]:
        await run_pill(services, signup, pill)
    extra = dict(signup["pills"][0], id="extra")
    result = await run_pill(services, signup, extra)
    assert result["ok"] is False


async def test_intent_button_runs_a_real_query(services):
    signup = await run(services, "random.person.123@gmail.com")
    await process_intent(services, signup["id"], "recruiting")
    updated = db.get_signup(signup["id"])
    assert updated["status"] == Status.DELIVERED
    assert updated["playbook"] == "recruiting"
    assert updated["result_rows"]
    assert updated["decision"]["decided_by"] == "intent"
    assert updated["decision"]["reasons"][0]["citations"]


@pytest.mark.parametrize("choice", ["gtm", "investor"])
async def test_other_intent_buttons_work_without_a_company(services, choice):
    signup = await run(services, "founder@brand-new-startup.xyz")
    await process_intent(services, signup["id"], choice)
    updated = db.get_signup(signup["id"])
    assert updated["status"] == Status.DELIVERED, updated.get("route")
    assert updated["result_rows"]


async def test_wrong_guess_can_change_the_playbook(services):
    signup = await run(services, "priya@hirefast.in")
    await process_intent(services, signup["id"], "gtm")
    updated = db.get_signup(signup["id"])
    assert updated["playbook"] == "gtm"
    assert updated["result_rows"], "a correction must never lose the signup their results"
    assert credits(updated) <= services.settings.budget_standard


async def test_contact_details_never_appear_in_results(services):
    signup = await run(services, "priya@hirefast.in")
    table = signup["email_html"].split("Why we picked")[0]
    assert not re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", table)
    assert not re.search(r"\+?\d[\d\s-]{8,}\d", table)
    for row in signup["result_rows"]:
        assert set(row) == {"a", "b", "c", "url"}


async def test_our_key_never_appears_in_anything_rendered(services):
    signup = await run(services, "priya@hirefast.in")
    secret = services.settings.crustdata_api_key
    assert secret not in signup["email_html"]
    assert "$CRUSTDATA_API_KEY" in signup["email_html"]
    assert secret not in str(signup)
