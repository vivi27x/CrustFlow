"""Claude proposes a table and our code writes the SQL, so a bad proposal can't reach anyone."""

import json
import re
from html import unescape

import pytest
from fastapi.testclient import TestClient

from app import db
from app.config import get_settings
from app.llm import LLMOutput, output_schema, system_prompt
from app.main import app
from app.models import Decision, Evidence, Pick, Playbook, Reason, Slots, StarterSpec, Status
from app.personas import load_personas
from app.pipeline import email_view, highlight, starter
from app.pipeline.run import process_signup, run_pill  # noqa: F401
from tests.test_supabase import StubLLM, output

SUPABASE = load_personas()["supabase"]
CRUSTDATA = load_personas()["crustdata"]


def plain(html):
    """What a reader sees and copies: the code without its colour tags."""
    return unescape(re.sub(r"<[^>]+>", "", str(html)))


def spec(**changes):
    values = {
        "table": "acme_tickets",
        "purpose": "Support tickets with a short summary",
        "columns": [{"name": "title", "type": "text"}, {"name": "priority", "type": "integer"}, {"name": "open", "type": "boolean"}],
        "rows": [
            ["Login page times out", "1", "true"],
            ["Export fails on large files", "2", "true"],
            ["Typo on the billing page", "3", "false"],
            ["Search misses new tickets", "1", "true"],
            ["Dark mode colours are off", "3", "false"],
        ],
        "search": "login page",
        "other_search": "billing typo",
    }
    return StarterSpec.model_validate({**values, **changes})


def build(picks=("vector",), company="Acme", person="Priya Sharma", **changes):
    return starter.compile_starter(spec(**changes), list(picks), SUPABASE, company, person)


def test_a_good_spec_gives_sql_named_for_them():
    built = build()
    assert "create table acme_tickets (" in built["sql"]
    assert "Supabase starter for Acme" in built["sql"]
    assert "'Login page times out', 1, true" in built["sql"]


def test_every_starter_is_protected_by_row_level_security():
    """A public table without it is readable with the anon key, so this isn't optional."""
    sql = build(picks=())["sql"]
    assert "enable row level security" in sql and "owner_id = auth.uid()" in sql


@pytest.mark.parametrize("pick,present", [
    ("vector", "using hnsw (embedding vector_cosine_ops)"),
    ("vector", "create function match_acme_tickets"),
    ("realtime", "alter publication supabase_realtime add table acme_tickets;"),
])
def test_each_pick_adds_its_lines(pick, present):
    assert present in build(picks=(pick,))["sql"]
    assert present not in build(picks=())["sql"]


@pytest.mark.parametrize("pick,present", [("queues", "pgmq.create('acme_tickets_jobs')"), ("cron", "'acme_tickets_nightly', '0 3 * * *'")])
def test_lines_that_need_a_real_project_stay_out_of_the_sandbox_sql(pick, present):
    built = build(picks=(pick,))
    assert present in built["project_only_sql"]
    assert present not in built["sql"]


def test_a_starter_is_only_for_a_vendor_that_offers_one():
    assert starter.compile_starter(spec(), ["vector"], CRUSTDATA, "Acme", "") is None


@pytest.mark.parametrize("table", ["", "Tickets", "1tickets", "a b", "tickets; drop table x", "order", "x" * 60, 'acme"--'])
def test_a_bad_table_name_drops_the_starter(table):
    assert build(table=table) is None


@pytest.mark.parametrize("columns", [
    [{"name": "title", "type": "text"}],
    [{"name": f"c{i}x", "type": "text"} for i in range(7)],
    [{"name": "title", "type": "text"}, {"name": "title", "type": "text"}],
    [{"name": "title", "type": "text"}, {"name": "order", "type": "text"}],
    [{"name": "title", "type": "text"}, {"name": "id", "type": "text"}],
    [{"name": "title", "type": "text"}, {"name": "x y", "type": "text"}],
    [{"name": "title", "type": "text"}, {"name": "note", "type": "text; drop table x"}],
    [{"name": "total", "type": "integer"}, {"name": "price", "type": "numeric"}],
], ids=["too few", "too many", "repeated", "reserved", "ours", "spaces", "bad type", "no text"])
def test_bad_columns_drop_the_starter(columns):
    assert build(columns=columns, rows=[["a", "b"]] * 5) is None


def test_quotes_cant_escape_a_value():
    rows = [["x'); drop table acme_tickets; --", "1", "true"], *spec().model_dump()["rows"][1:]]
    sql = build(rows=rows)["sql"]
    assert "'x''); drop table acme_tickets; --', 1, true" in sql


def test_a_comment_cant_break_out_onto_a_new_line():
    assert "\ndrop" not in build(purpose="Tickets\ndrop table users")["sql"]


@pytest.mark.parametrize("value", ["1; drop table x", "one", "1.5", ""])
def test_a_number_must_be_a_number(value):
    rows = [["A title", value, "true"], *spec().model_dump()["rows"][1:]]
    assert build(rows=rows) is None


def test_a_boolean_must_be_a_boolean():
    rows = [["A title", "1", "maybe"], *spec().model_dump()["rows"][1:]]
    assert build(rows=rows) is None


def test_a_date_must_be_a_date():
    columns = [{"name": "title", "type": "text"}, {"name": "due", "type": "timestamptz"}]
    assert build(columns=columns, rows=[["a", "someday"]] * 5) is None
    assert "'2026-11-01'" in build(columns=columns, rows=[["a", "2026-11-01"]] * 5)["sql"]


@pytest.mark.parametrize("text", ["ask jane@acme.com", "call +91 98765 43210", "Priya will review", "from sharma", "null\x00byte"])
def test_no_contact_details_or_their_own_name_in_sample_rows(text):
    rows = [[text, "1", "true"], *spec().model_dump()["rows"][1:]]
    assert build(rows=rows) is None


def test_too_few_rows_drops_the_starter():
    assert build(rows=spec().model_dump()["rows"][:2]) is None


def test_a_wrong_length_row_drops_the_starter():
    rows = [["only one"], *spec().model_dump()["rows"][1:]]
    assert build(rows=rows) is None


def test_the_last_row_is_held_back_for_the_add_a_row_button():
    built = build()
    assert "Dark mode" not in built["sql"]
    pill = next(p for p in built["pills"] if p["id"] == "add_row")
    assert "Dark mode" in pill["sql"]


def test_sample_owners_stay_out_of_the_code_people_read():
    built = build()
    assert starter.FIRST_USER not in built["sql"] and starter.OTHER_USER not in built["sql"]
    assert "owner_id uuid default auth.uid()" in built["sql"], "Sample rows have no owner, so it can't be required"


def test_the_sandbox_gives_sample_rows_two_owners_so_the_policy_shows_something():
    update = build()["assign_owners"]
    assert starter.FIRST_USER in update and starter.OTHER_USER in update


@pytest.mark.parametrize("picks", [(), ("vector",), ("vector", "realtime", "queues", "cron")], ids=["plain", "vector", "everything"])
def test_every_line_fits_the_code_block(picks):
    table = "a_rather_long_company_name_tickets"
    built = build(picks=picks, table=table)
    for line in f"{built['sql']}\n{built['project_only_sql']}".split("\n"):
        assert len(line) <= starter.MAX_LINE, line


def test_a_long_row_breaks_between_values_and_stays_valid():
    columns = [{"name": f"note_{word}", "type": "text"} for word in ("one", "two", "three", "four")]
    value = "A sample value here"
    rows = [[f"{value} {i}" for i in range(4)] for _ in range(5)]
    sql = build(columns=columns, rows=rows)["sql"]
    assert f"'{value} 0', '{value} 1',\n   '{value} 2'" in sql
    assert all(len(line) <= starter.MAX_LINE for line in sql.split("\n"))


def test_a_long_purpose_wraps_as_comments():
    purpose = "Customer accounts with their deployment details and renewal dates across every region we serve"
    lines = build(purpose=purpose)["sql"].split("\n")
    assert all(line.startswith("--") for line in lines[:4])


def test_three_pills_with_vector():
    assert [p["id"] for p in build()["pills"]] == ["search", "other_user", "add_row"]


def test_three_pills_without_vector():
    assert [p["id"] for p in build(picks=())["pills"]] == ["other_user", "add_row", "filter"]


def test_no_second_search_means_another_pill_fills_in():
    assert [p["id"] for p in build(other_search="")["pills"]] == ["other_user", "add_row", "filter"]


def test_a_filter_value_is_quoted():
    columns = [{"name": "title", "type": "text"}, {"name": "team", "type": "text"}]
    rows = [[f"A long title for ticket number {word}", "o'brien"] for word in ("one", "two", "three", "four", "five")]
    built = build(picks=(), columns=columns, rows=rows)
    assert "where team = 'o''brien'" in next(p for p in built["pills"] if p["id"] == "filter")["sql"]


def test_a_filter_prefers_a_value_that_repeats():
    columns = [{"name": "title", "type": "text"}, {"name": "team", "type": "text"}]
    rows = [[f"Ticket {word}", "billing"] for word in ("one", "two", "three", "four", "five")]
    pill = next(p for p in build(picks=(), columns=columns, rows=rows)["pills"] if p["id"] == "filter")
    assert "where team = 'billing'" in pill["sql"]


def test_a_missing_search_phrase_uses_the_purpose():
    assert build(search="")["search"] == "Support tickets with a short summary"


def test_the_mcp_prompt_has_no_placeholders():
    prompt = build(picks=("vector", "realtime", "queues", "cron"))["mcp_prompt"]
    assert "acme_tickets" in prompt and "match_acme_tickets" in prompt
    for placeholder in ("<", ">", "{", "YOUR", "your-"):
        assert placeholder not in prompt


# ---- What Claude is told ----


def test_claude_gets_a_spec_to_fill_only_when_the_vendor_offers_one():
    assert "starter: a small Postgres table" in system_prompt(SUPABASE)
    assert "starter: a small Postgres table" not in system_prompt(CRUSTDATA)


def test_claude_is_told_not_to_sell_people_their_own_product():
    assert "never pitch that service as a replacement" in system_prompt(SUPABASE)


def test_column_types_are_a_fixed_list():
    schema = output_schema(SUPABASE)
    assert schema["$defs"]["StarterColumn"]["properties"]["type"]["enum"] == ["", "text", "integer", "numeric", "boolean", "timestamptz"]


def test_the_model_must_answer_the_new_fields():
    schema = output_schema(SUPABASE)
    assert {"starter", "sales_angle"} <= set(schema["required"])
    assert "default" not in schema["properties"]["starter"]


def test_old_recordings_still_load():
    data = json.loads(LLMOutput.model_validate(output().model_dump()).model_dump_json())
    data.pop("starter"), data.pop("sales_angle")
    assert LLMOutput.model_validate(data).starter.table == ""


# ---- Delivered ----


def with_starter(**changes):
    result = output()
    result.starter = spec(**changes)
    return result


async def run(services, llm_output):
    services.settings.vendor_persona = "supabase"
    services.llm = StubLLM(llm_output)
    signup = db.create_signup("priya@hirefast.in", name="Priya Sharma", persona_id="supabase")
    await process_signup(services, signup["id"])
    return db.get_signup(signup["id"])


async def test_the_email_carries_the_starter_and_the_sandbox_link(services):
    signup = await run(services, with_starter())
    html = signup["email_html"]
    assert signup["starter"]["table"] == "acme_tickets"
    assert f"/sandbox/{signup['sandbox_token']}" in html
    assert "create table acme_tickets" in plain(html)
    assert "s starter in your browser" in html
    assert "acme_tickets.sql" in html and "Run it live" in html


async def test_the_email_and_the_sandbox_run_the_same_sql(services):
    signup = await run(services, with_starter())
    built = signup["starter"]
    page = plain(signup["email_html"])
    assert all(block["code"] in page for block in built["blocks"])
    assert built["sql"].split("\n\n", 1)[1] == "\n\n".join(block["code"] for block in built["blocks"])


@pytest.mark.parametrize("picks", [(), ("vector",), ("vector", "realtime", "auth")], ids=["plain", "vector", "everything"])
def test_the_sections_read_in_an_order_that_runs(picks):
    """Someone may copy the email from top to bottom, so the extension must come before the table."""
    code = "\n\n".join(block["code"] for block in build(picks=picks)["blocks"])
    order = [code.find(text) for text in ("create table", "enable row level security", "insert into")]
    assert order == sorted(order) and -1 not in order
    if "vector" in picks:
        assert code.find("create extension") < code.find("create table") < code.find("create index") < code.find("create function")


def pick(service):
    return Pick(service, Reason(f"Why {service} fits.", [Evidence("E1", "company/enrich", "headcount.total", "9")]))


def sections_for(services_picked, **changes):
    decision = Decision(Playbook.MESSAGE, 0.9, [], Slots(), "llm", picks=[pick(s) for s in services_picked])
    built = starter.compile_starter(spec(**changes), services_picked, SUPABASE, "Acme", "Priya Sharma")
    return email_view.blueprint(decision, SUPABASE, built)


def test_a_pick_sits_on_the_lines_it_adds():
    sections = sections_for(["vector", "auth", "realtime"])
    by_title = {s["title"]: s for s in sections}
    vector = next(s for s in sections if "Why vector fits." == s["why"])
    assert "create function match_acme_tickets" in plain(vector["code"])
    assert "enable row level security" in plain(next(s for s in sections if s["why"] == "Why auth fits.")["code"])
    assert "supabase_realtime" in plain(next(s for s in sections if s["why"] == "Why realtime fits.")["code"])
    assert "Your table" in by_title and "Sample rows" in by_title


def test_sections_use_the_vendors_names_and_links_not_claudes():
    section = next(s for s in sections_for(["vector"]) if s["why"] == "Why vector fits.")
    service = SUPABASE.service("vector")
    assert (section["title"], section["url"]) == (service.name, service.url)


def test_a_pick_with_no_sql_still_appears_with_its_reason():
    section = next(s for s in sections_for(["storage"]) if s["why"] == "Why storage fits.")
    assert section["code"] == "" and section["title"] == SUPABASE.service("storage").name


def test_two_picks_on_one_block_both_appear():
    whys = [s["why"] for s in sections_for(["auth", "compliance"])]
    assert "Why auth fits." in whys and "Why compliance fits." in whys


def test_row_level_security_is_explained_even_when_nobody_picked_it():
    assert any(s["title"] == "Row Level Security" for s in sections_for(["vector"]))


def test_a_section_needing_a_real_project_says_so():
    section = next(s for s in sections_for(["queues"]) if s["why"] == "Why queues fits.")
    assert "sandbox skips this part" in section["note"] and "pgmq.create" in plain(section["code"])


def test_sample_rows_come_last():
    assert sections_for(["vector", "queues"])[-1]["title"] == "Sample rows"


def test_colouring_never_changes_the_code():
    sql = build(picks=("vector", "realtime"))["sql"]
    assert plain(highlight.sql(sql)) == sql


def test_the_code_is_coloured_inline_because_email_clients_strip_stylesheets():
    html = str(highlight.sql("select 1 from t -- note"))
    assert 'style="color: #A5B4FC"' in html and "class=" not in html


def test_a_sample_value_cant_become_html():
    rows = [["<script>alert(1)</script>", "1", "true"], *spec().model_dump()["rows"][1:]]
    html = str(highlight.sql(build(rows=rows)["sql"]))
    assert "<script>" not in html and "&lt;script&gt;" in html


async def test_a_bad_spec_still_sends_the_email_without_code(services):
    signup = await run(services, with_starter(table="drop table x"))
    assert signup["status"] == Status.DELIVERED
    assert not signup["starter"] and not signup["sandbox_token"]
    assert "Start your project" in signup["email_html"]
    assert any("couldn't be made safe" in c["note"] for c in db.list_calls(signup["id"]))


async def test_no_spec_keeps_the_plain_button(services):
    signup = await run(services, output())
    assert "Start your project" in signup["email_html"]
    assert not any("couldn't be made safe" in c["note"] for c in db.list_calls(signup["id"]))


async def test_a_message_signup_counts_as_served(services):
    """Nobody on the Supabase path gets search rows, and the headline numbers must still show."""
    from app import kpis

    signup = await run(services, with_starter())
    numbers = kpis.compute()
    assert numbers["served"] == 1 and numbers["avg_ttfr_s"] is not None
    assert numbers["credits_per_served"] == round(db.credits_for(signup["id"]), 2)
    assert numbers["non_empty_rate"] is None, "No search ran, so there's no rate to show"


async def test_a_sales_angle_reaches_the_slack_card(services):
    from app.delivery.slack import blocks
    from app.llm import LLMReason
    from app.models import Reason, Evidence
    from app.pipeline import route
    from app.models import Company, Decision, Person, Playbook, QueryOutcome, Slots

    cited = Evidence("E1", "company/enrich", "headcount.total", "900", "2026-11-06")
    decision = Decision(Playbook.MESSAGE, 0.9, [Reason("A fit.", [cited])], Slots(), "llm", sales_angle=Reason("Open on the review.", [cited]))
    brief = route.build_brief(Person(name="A"), Company(name="Acme"), decision, QueryOutcome(Playbook.MESSAGE, None, []), "a@acme.io")
    assert "Open on the review." in str(blocks(brief))
    assert LLMReason(claim="", evidence_ids=[]).claim == ""


def test_a_sales_angle_without_real_evidence_is_dropped():
    from app.llm import LLMReason
    from app.models import EvidenceTable
    from app.pipeline.classify import _backed

    table = EvidenceTable()
    table.add("company/enrich", "headcount.total", "900")
    assert _backed(LLMReason(claim="Open here.", evidence_ids=["E1"]), table) is not None
    assert _backed(LLMReason(claim="Open here.", evidence_ids=["E9"]), table) is None
    assert _backed(LLMReason(claim="", evidence_ids=[]), table) is None


def test_a_legal_suffix_stays_out_of_the_button():
    from app.pipeline.profiles import short_name

    assert short_name("Acme LLC.") == "Acme"
    assert short_name("Acme Pvt. Ltd.") == "Acme"
    assert short_name("Acme") == "Acme"


def test_no_company_still_reads_well():
    label = email_view.call_to_action(SUPABASE, {"company": ""}, "http://x")[0]["label"]
    assert label == "Run your starter in your browser"


# ---- The sandbox page ----


@pytest.fixture
def http(tmp_path, monkeypatch):
    db.close()
    monkeypatch.setenv("DB_PATH", str(tmp_path / "http.sqlite"))
    monkeypatch.setenv("CRUSTDATA_MODE", "sim")
    monkeypatch.setenv("VENDOR_PERSONA", "supabase")
    get_settings.cache_clear()
    with TestClient(app) as client:
        yield client
    get_settings.cache_clear()
    db.close()


def sandbox_signup(**fields):
    signup = db.create_signup("a@acme.io", name="", persona_id="supabase")
    db.update_signup(signup["id"], sandbox_token="tok", starter=build(), status=Status.DELIVERED.value, **fields)
    return signup["id"]


def test_the_sandbox_page_runs_the_emailed_sql(http):
    sandbox_signup()
    page = http.get("/sandbox/tok")
    assert page.status_code == 200
    assert "acme_tickets" in page.text and "sandbox_starter.js" in page.text
    assert "isn't involved" in page.text and "It isn't a Supabase project" in page.text


def test_data_in_the_page_cant_close_the_script_tag(http):
    signup = db.create_signup("a@acme.io", persona_id="supabase")
    built = build(purpose="Tickets </script><script>alert(1)</script>")
    db.update_signup(signup["id"], sandbox_token="tok", starter=built)
    assert "<script>alert(1)" not in http.get("/sandbox/tok").text


def test_opening_the_page_doesnt_activate_anyone(http):
    """Mail scanners open links, so only a real second run counts."""
    signup_id = sandbox_signup()
    http.get("/sandbox/tok")
    assert db.get_signup(signup_id)["status"] == Status.DELIVERED


def test_a_second_run_activates_for_free(http):
    signup_id = sandbox_signup()
    assert http.post("/sandbox/tok/ran", json={"pill": "other_user"}).json() == {"ok": True}
    signup = db.get_signup(signup_id)
    assert signup["status"] == Status.ACTIVATED and signup["activated_pill"] == "View as another user"
    assert db.credits_for(signup_id) == 0


def test_their_own_search_counts_as_a_second_run(http):
    signup_id = sandbox_signup()
    http.post("/sandbox/tok/ran", json={"pill": "own_search"})
    assert db.get_signup(signup_id)["status"] == Status.ACTIVATED


def test_activation_keeps_the_first_run(http):
    signup_id = sandbox_signup()
    http.post("/sandbox/tok/ran", json={"pill": "other_user"})
    http.post("/sandbox/tok/ran", json={"pill": "add_row"})
    assert db.get_signup(signup_id)["activated_pill"] == "View as another user"


def test_an_unknown_pill_or_token_is_refused(http):
    sandbox_signup()
    assert http.post("/sandbox/tok/ran", json={"pill": "nope"}).status_code == 404
    assert http.post("/sandbox/other/ran", json={"pill": "other_user"}).status_code == 404
