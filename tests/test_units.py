import json
import shlex

import pytest

from app.crustdata.client import Ledger
from app.delivery import snippets
from app.delivery.estimate import estimate_credits
from app.models import Company, EvidenceTable, Person, Playbook
from app.pipeline import gates
from app.pipeline.classify import is_researcher, is_student, research_field_from
from app.models import dig
from app.pipeline.query import looks_like
from app.pipeline.route import freshest_signal
from app.playbooks import base, recruiting


@pytest.mark.parametrize("raw,lookup,dedupe", [
    ("  Priya+test@HireFast.IN ", "priya@hirefast.in", "priya@hirefast.in"),
    ("Priya Sharma <priya@hirefast.in>", "priya@hirefast.in", "priya@hirefast.in"),
    ("mailto:p.riya@gmail.com", "p.riya@gmail.com", "priya@gmail.com"),
    ("p.r.i.y.a+1@googlemail.com", "p.r.i.y.a@gmail.com", "priya@gmail.com"),
    ("a.b@hirefast.in", "a.b@hirefast.in", "a.b@hirefast.in"),
])
def test_normalise(raw, lookup, dedupe):
    email = gates.normalise(raw)
    assert (email.lookup, email.dedupe_key) == (lookup, dedupe)


@pytest.mark.parametrize("raw", ["", " ", "x", "a@b", "@x.com", "+x@gmail.com", "a@@b.com", "a b@c.com", None])
def test_normalise_rejects(raw):
    assert gates.normalise(raw) is None


def test_domain_lists_match_subdomains_only_upwards():
    assert gates.domain_in("mail.mailinator.com", gates.disposable_domains())
    assert not gates.domain_in("mailinator.com.evil.io", gates.disposable_domains())
    assert not gates.domain_in("hirefast.in", gates.disposable_domains())


@pytest.mark.parametrize("domain,expected", [
    ("iitk.ac.in", True), ("mit.edu", True), ("cs.stanford.edu", True), ("ox.ac.uk", True),
    ("unimelb.edu.au", True), ("edu.com", False), ("hirefast.in", False), ("ac.in.example.com", False),
])
def test_academic_domains(domain, expected):
    assert gates.is_academic(domain) is expected


@pytest.mark.parametrize("headline,student", [
    ("B.Tech student at IIT Kanpur", True),
    ("CS undergrad", True),
    ("Final year BTech", True),
    ("Professor of Computer Science", False),
    ("PhD student in NLP", False),
    ("Student success manager", True),
    ("Head of Talent", False),
])
def test_student_detection(headline, student):
    assert is_student(Person(headline=headline)) is student


@pytest.mark.parametrize("headline,department,field", [
    ("Professor of Computer Science, natural language processing", "", "natural language processing"),
    ("Associate Professor of Physics at IIT Kanpur", "Physics", "Physics"),
    ("PhD candidate in computational biology", "", "computational biology"),
    ("Assistant Professor, Department of Civil Engineering, IIT Kanpur", "Civil Engineering", "Civil Engineering"),
    ("Researcher", "Chemistry", "Chemistry"),
    ("", "", ""),
])
def test_research_field_keeps_words_whole(headline, department, field):
    assert research_field_from(Person(headline=headline, department=department)) == field


def test_researcher_detection():
    assert is_researcher(Person(headline="Associate Professor, IIT Kanpur"))
    assert is_researcher(Person(headline="PhD candidate"))
    assert not is_researcher(Person(headline="Head of Talent"))


def test_ledger():
    ledger = Ledger(cap=1)
    assert ledger.can_spend(1)
    ledger.charge(0.4)
    assert not ledger.can_spend(0.7)
    ledger.raise_cap(8)
    assert ledger.can_spend(7.6)


def test_person_cost_matches_the_pricing_notes():
    body = base.person_body([base.cond(base.TITLE, "(.)", "x"), base.cond(base.COUNTRY, "=", "India")])
    assert base.person_cost_per_result(body) == 0.23
    no_premium_filter = base.person_body([base.cond(base.COUNTRY, "=", "India")])
    assert base.person_cost_per_result(no_premium_filter) == 0.13


def test_ladder_has_at_most_two_retries_and_gets_wider():
    rungs = recruiting.ladder("Senior Backend Engineer", "Bengaluru", "India")
    assert len(rungs) == 3
    assert [len(base.conditions_of(r.body)) for r in rungs] == [3, 2, 2]
    assert base.conditions_of(rungs[2].body)[0]["value"] == "Backend Engineer"


def test_pills_change_exactly_one_filter():
    body = recruiting.ladder("Senior Backend Engineer", "Bengaluru", "India")[0].body
    original = base.conditions_of(body)
    for pill in recruiting.pills(body, "Senior Backend Engineer"):
        changed = base.conditions_of(pill.body)
        differing = [c for c in changed if c not in original] + [c for c in original if c not in changed]
        assert 1 <= len(differing) <= 2, pill.id  # One added or removed, or one swapped.


def test_pills_do_not_mutate_the_delivered_request():
    body = recruiting.ladder("Backend Engineer", "", "India")[0].body
    before = json.dumps(body)
    recruiting.pills(body, "Backend Engineer")
    assert json.dumps(body) == before


def test_snippets_round_trip_to_the_exact_body():
    body = {"filters": {"field": "basic_profile.name", "type": "(.)", "value": "O'Brien & Sons"}, "limit": 10}
    tokens = shlex.split(snippets.curl("/person/search", body))
    assert json.loads(tokens[tokens.index("-d") + 1]) == body
    code = snippets.python("/person/search", body)
    namespace: dict = {}
    exec(code.split("response = ")[0].split("import requests")[1], namespace)
    assert namespace["body"] == body


def test_python_snippet_survives_awkward_values():
    body = {
        "a": True, "b": None, "c": 1.5, "d": [], "e": {}, "f": ["x", {"y": [1, 2]}],
        "g": 'He said "hi" and it\'s \\ fine', "h": "Bengaluru café", "i": ["z" * 80],
    }
    namespace: dict = {}
    exec(snippets.python("/x", body).split("response = ")[0].split("import requests")[1], namespace)
    assert namespace["body"] == body


def test_python_snippet_is_readable():
    body = {"filters": {"op": "and", "conditions": [{"field": "x", "type": "in", "value": ["CXO", "Director"]}]}}
    code = snippets.python("/x", body)
    assert '"value": ["CXO", "Director"],' in code
    assert max(len(line) for line in code.splitlines()) <= 90


def test_snippets_use_an_environment_variable_for_the_key():
    assert "$CRUSTDATA_API_KEY" in snippets.curl("/x", {})
    assert "os.environ" in snippets.python("/x", {})


def test_python_snippet_handles_booleans_as_python():
    code = snippets.python("/person/search", {"filters": {"field": "x", "type": "=", "value": True}})
    assert "True" in code and "true" not in code


def test_estimate_prints_its_inputs_and_is_deterministic():
    company = Company(matched=True, openings=5, headcount=42)
    first = estimate_credits(Playbook.RECRUITING, company)
    assert first == estimate_credits(Playbook.RECRUITING, company)
    assert first.credits_per_month == 700
    assert any("5 open roles" in a for a in first.assumptions)


def test_estimate_copes_with_missing_company_numbers():
    assert estimate_credits(Playbook.GTM, Company(matched=True)).credits_per_month > 0
    assert estimate_credits(Playbook.RECRUITING, Company(matched=True)).credits_per_month > 0


def test_freshest_signal_picks_the_newest_date():
    rows = [
        {"name": "A", "title": "T", "company": "C", "start_date": "2024-01-01", "updated_at": ""},
        {"name": "B", "title": "T", "company": "C", "start_date": "2026-09-01", "updated_at": "2026-08-01T00:00:00Z"},
    ]
    assert "B started" in freshest_signal(rows)
    assert freshest_signal([]) == "No dated signal in these results"


def test_dig_survives_unexpected_shapes():
    assert dig({"a": [{"b": 1}]}, "a", "b") == 1
    assert dig({"a": {"b": 1}}, "a", "b") == 1
    assert dig({"a": []}, "a", "b", default="x") == "x"
    assert dig("nonsense", "a", default=2) == 2
    assert dig(None, 0, default=3) == 3


def test_evidence_skips_empty_values_and_builds_citations():
    table = EvidenceTable()
    assert table.add("job/search", "job_details.title", "") is None
    evidence_id = table.add("job/search", "job_details.title", "Recruiter", "2026-10-21")
    assert table.get(evidence_id).citation() == "job/search, job_details.title, 2026-10-21"


@pytest.mark.parametrize("value,suggestion,expected", [
    ("Bangalore", "Bengaluru", True),
    ("Senior Backend Engineer", "Backend Engineer", True),
    ("Backend Engineer", "Registered Nurse", False),
])
def test_snap_guard(value, suggestion, expected):
    assert looks_like(value, suggestion) is expected


def test_company_cost_matches_what_the_real_api_charged():
    from app.playbooks.base import company_cost_per_result

    size = {"field": "headcount.total", "type": "=>", "value": 50}
    industry = {"field": "taxonomy.professional_network_industry", "type": "=", "value": "Software Development"}
    country = {"field": "locations.country", "type": "=", "value": "India"}
    assert company_cost_per_result({"filters": country, "fields": ["basic_info.name"]}) == 0.03
    assert company_cost_per_result({"filters": size, "fields": ["basic_info.name"]}) == 0.23
    assert company_cost_per_result({"filters": size, "fields": ["headcount.total"]}) == 0.43
    assert company_cost_per_result({"filters": size, "fields": ["taxonomy.professional_network_industry"]}) == 0.33
    assert company_cost_per_result({"filters": industry, "fields": ["basic_info.name"]}) == 0.13


def test_snapping_prefers_the_exact_value_over_the_first_suggestion():
    from app.pipeline.query import best_suggestion

    # Real autocomplete order from the Crustdata API.
    assert best_suggestion("Bangalore", [{"value": "Bangalore Rural"}, {"value": "Bangalore"}]) == "Bangalore"
    assert best_suggestion("india", [{"value": "India"}, {"value": "British Indian Ocean Territory"}]) == "India"


def test_snapping_does_not_drift_to_a_narrower_title():
    from app.pipeline.query import best_suggestion

    suggestions = [
        {"value": "Engineer", "document_count": 900},
        {"value": "Senior Security Engineer", "document_count": 40},
        {"value": "Security Engineer II", "document_count": 90},
    ]
    assert best_suggestion("Security Engineer", suggestions) == "Security Engineer II"
    assert best_suggestion("Quantum Plumber", suggestions) is None


@pytest.mark.parametrize("raw,clean", [
    ("Security Engineer, Privy", "Security Engineer"),
    ("Account Executive - Enterprise", "Account Executive"),
    ("Senior Backend Engineer (Payments)", "Senior Backend Engineer"),
    ("Site Reliability Engineer | Remote", "Site Reliability Engineer"),
    ("Full-Stack Engineer", "Full-Stack Engineer"),
    ("", ""),
])
def test_job_post_padding_is_removed_from_titles(raw, clean):
    from app.pipeline.query import strip_padding

    assert strip_padding(raw) == clean


@pytest.mark.parametrize("location,parts", [
    ("Bengaluru, Karnataka, India", ("Bengaluru", "India")),
    ("Bengaluru", ("Bengaluru", "")),  # Real Greenhouse jobs look like this.
    ("Remote, India", ("", "India")),
    ("Remote", ("", "")),
    ("", ("", "")),
])
def test_location_parts_treat_a_lone_place_as_a_city(location, parts):
    from app.pipeline.classify import _location_parts

    assert _location_parts(location) == parts
