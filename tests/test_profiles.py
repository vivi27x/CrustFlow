"""Finding people with no email lookup."""

import pytest

from app import db
from app.models import Status
from app.pipeline import gates, profiles
from app.pipeline.run import process_intent, process_signup


async def run(services, raw, name=""):
    signup = db.create_signup(raw, name=name)
    await process_signup(services, signup["id"])
    return db.get_signup(signup["id"])


def endpoints(signup):
    return [c["endpoint"] for c in db.list_calls(signup["id"])]


@pytest.mark.parametrize("name,local,parts", [
    ("Noor Khan", "noor_khan", ["noor", "khan"]),
    ("", "noor_khan", ["noor", "khan"]),
    ("", "noor.k23", ["noor"]),
    ("", "prof.rao", ["rao"]),
    ("", "hello", []),
    ("", "careers", []),
    ("", "230123", []),
    ("Dr. Asha Menon", "x", ["asha", "menon"]),
])
def test_name_parts(name, local, parts):
    email = gates.normalise(f"{local}@example.com")
    assert profiles.name_parts(name, email) == parts


async def test_an_address_that_differs_from_the_profile_name_is_still_found(services):
    """This failed on real data: the address and the profile used different surnames."""
    signup = await run(services, "noor_khan@hirefast.in", "Noor Khan")
    assert signup["person"]["person_id"] == 9010
    assert signup["person"]["confidence"] == profiles.SOME_PARTS_CONFIDENCE
    assert "part of the name" in signup["person"]["match_note"]


async def test_a_full_name_match_is_more_confident(services):
    signup = await run(services, "sam.iyer@hirefast.in", "Sam Iyer")
    assert signup["person"]["person_id"] == 9011
    assert signup["person"]["confidence"] == profiles.ALL_PARTS_CONFIDENCE


async def test_two_people_who_fit_means_no_guess(services):
    signup = await run(services, "sam@hirefast.in", "Sam")
    assert signup["person"]["status"] == "ambiguous"
    assert signup["person"]["person_id"] is None


async def test_a_role_address_with_no_name_names_nobody(services):
    signup = await run(services, "hello@hirefast.in")
    assert signup["person"]["status"] == "unavailable"
    assert "/person/search" not in [e for e in endpoints(signup)][-3:]


async def test_a_personal_email_with_a_unique_name_is_found_and_says_how(services):
    signup = await run(services, "meera.ops@gmail.com", "Meera Joshi")
    assert signup["person"]["match_method"] == "name_only"
    assert signup["company"]["matched"], "the employer comes from the enriched profile"
    assert signup["status"] == Status.DELIVERED
    cited = [c["citation"] for r in signup["decision"]["reasons"] for c in r["citations"]]
    assert any("name only" in c for c in cited), cited


async def test_a_personal_email_with_a_shared_name_asks(services):
    signup = await run(services, "someone@gmail.com", "Asha Menon")
    assert signup["person"]["status"] == "ambiguous"
    assert signup["status"] == Status.AWAITING_INTENT
    assert db.credits_for(signup["id"]) <= 1


async def test_a_personal_email_with_no_name_spends_nothing(services):
    signup = await run(services, "someone@gmail.com")
    assert signup["status"] == Status.AWAITING_INTENT
    assert db.credits_for(signup["id"]) == 0
    assert not any(e.startswith("/person") for e in endpoints(signup))


async def test_a_student_stops_before_the_paid_enrich(services):
    signup = await run(services, "rohan.codes@gmail.com", "Rohan Kulkarni")
    assert signup["status"] == Status.NURTURE
    assert "/person/enrich" not in endpoints(signup)
    assert db.credits_for(signup["id"]) <= 1


async def test_one_person_under_two_work_addresses_is_one_record(services):
    first = await run(services, "noor_khan@hirefast.in", "Noor Ahmed")
    second = await run(services, "noor.a@hirefast.in", "Noor Ahmed")
    assert first["status"] != Status.HUMAN_QUEUE
    assert second["status"] == Status.HUMAN_QUEUE
    assert second["route"] == "Repeat identity"


async def test_a_redacted_profile_stops_at_enrich(services):
    signup = await run(services, "private.person@gmail.com", "Private Person")
    assert signup["status"] == Status.WITHDRAWN


async def test_a_failed_enrich_keeps_what_the_search_found(services):
    services.client.faults.add("/person/enrich", 500, times=4)
    signup = await run(services, "priya@hirefast.in", "Priya Sharma")
    assert signup["person"]["person_id"] == 9001
    assert signup["person"]["enriched"] is False
    assert signup["status"] == Status.DELIVERED


async def test_enrich_fills_the_profile(services):
    signup = await run(services, "priya@hirefast.in", "Priya Sharma")
    person = signup["person"]
    assert person["enriched"] is True
    assert person["past_roles"] and person["education"] and person["skills"]
    assert any(e["field"] == "skills.professional_network_skills" for e in signup["evidence"])


async def test_identify_is_never_called(services):
    for raw, name in (("priya@hirefast.in", "Priya"), ("meera.ops@gmail.com", "Meera Joshi"), ("a@brand-new.xyz", "")):
        signup = await run(services, raw, name)
        assert "/person/identify" not in endpoints(signup)


async def test_the_learning_button_gives_the_guide_for_nothing(services):
    signup = await run(services, "someone@gmail.com")
    await process_intent(services, signup["id"], "student")
    updated = db.get_signup(signup["id"])
    assert updated["status"] == Status.NURTURE
    assert db.credits_for(signup["id"]) == 0
    assert updated["sales_alert"] == "suppressed"
