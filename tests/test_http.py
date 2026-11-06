"""The HTTP surface, through the real FastAPI app."""

import httpx
import pytest
from fastapi.testclient import TestClient

from app import db
from app.delivery.mail import Mailer
from app.delivery.slack import SlackNotifier, blocks
from app.main import app
from app.models import Decision, Playbook, QueryOutcome, Reason, Slots, Attempt
from app.pipeline import deliver
from tests.conftest import make_settings


@pytest.fixture
def client(tmp_path, monkeypatch):
    db.close()
    settings = make_settings(tmp_path)
    db.connect(settings.db_path)  # Connected first, so the app's lifespan reuses this test database.
    monkeypatch.setattr("app.main.get_settings", lambda: settings)
    with TestClient(app) as test_client:
        yield test_client
    db.close()


def wait_for_finish(client):
    # TestClient runs the lifespan's loop; pipeline tasks finish on their own, so poll.
    import time

    for _ in range(200):
        signups = client.get("/api/state").json()["signups"]
        if signups and all(s["status"] not in ("received", "gating", "identifying", "researching", "classifying", "querying", "delivering") for s in signups):
            return signups
        time.sleep(0.02)
    raise AssertionError("signups did not finish")


def test_signup_through_to_activation(client):
    client.post("/api/signups", json={"email": "priya@hirefast.in"})
    signup = wait_for_finish(client)[0]
    detail = client.get(f"/api/signups/{signup['id']}").json()
    assert detail["has_email"] is True and "email_html" not in detail
    assert all("body" not in pill for pill in detail["pills"]), "pill bodies stay on the server"

    page = client.get(f"/sandbox/{detail['sandbox_token']}")
    assert page.status_code == 200 and "Senior Backend Engineer" in page.text

    result = client.post(f"/sandbox/{detail['sandbox_token']}/pills/{detail['pills'][0]['id']}").json()
    assert result["ok"] is True and "curl" in result
    assert client.get(f"/api/signups/{signup['id']}").json()["status"] == "activated"
    assert client.get("/api/kpis").json()["kpis"]["activations"] == 1


def test_a_pill_id_cannot_smuggle_a_custom_query(client):
    client.post("/api/signups", json={"email": "priya@hirefast.in"})
    signup = wait_for_finish(client)[0]
    token = client.get(f"/api/signups/{signup['id']}").json()["sandbox_token"]
    assert client.post(f"/sandbox/{token}/pills/anything-else").status_code == 404


def test_unknown_links_get_a_friendly_page(client):
    for path in ("/sandbox/nope", "/intent/nope"):
        response = client.get(path)
        assert response.status_code == 404 and "expired" in response.text


def test_opening_an_intent_link_spends_nothing(client):
    """Mail scanners prefetch links, so a GET must never run a query."""
    client.post("/api/signups", json={"email": "founder@brand-new-startup.xyz"})
    signup = wait_for_finish(client)[0]
    html = client.get(f"/api/signups/{signup['id']}/email").text
    token = html.split("/intent/")[1].split("?")[0]
    before = client.get("/api/kpis").json()["kpis"]["total_credits"]
    page = client.get(f"/intent/{token}?choice=recruiting")
    assert page.status_code == 200
    assert client.get("/api/kpis").json()["kpis"]["total_credits"] == before
    assert client.get(f"/api/signups/{signup['id']}").json()["status"] == "awaiting_intent"


def test_intent_button_click_runs_and_lands_in_the_sandbox(client):
    client.post("/api/signups", json={"email": "founder@brand-new-startup.xyz"})
    signup = wait_for_finish(client)[0]
    token = client.get(f"/api/signups/{signup['id']}/email").text.split("/intent/")[1].split("?")[0]
    response = client.post(
        f"/intent/{token}", content="choice=gtm",
        headers={"content-type": "application/x-www-form-urlencoded"}, follow_redirects=False,
    )
    assert response.status_code == 303
    wait_for_finish(client)
    landed = client.get(f"/intent/{token}?running=1", follow_redirects=False)
    assert landed.status_code == 303 and "/sandbox/" in landed.headers["location"]


def test_unknown_intent_choice_is_rejected(client):
    client.post("/api/signups", json={"email": "founder@brand-new-startup.xyz"})
    signup = wait_for_finish(client)[0]
    token = client.get(f"/api/signups/{signup['id']}/email").text.split("/intent/")[1].split("?")[0]
    response = client.post(f"/intent/{token}", content="choice=hack", headers={"content-type": "application/x-www-form-urlencoded"})
    assert response.status_code == 400


def test_demo_buttons_and_reset(client):
    assert client.post("/api/demo/matrix").status_code == 200
    signups = wait_for_finish(client)
    assert len(signups) == 8
    assert client.post("/api/demo/nope").status_code == 404
    client.post("/api/reset")
    assert client.get("/api/state").json()["signups"] == []


def test_fault_drill_endpoint(client):
    assert client.post("/api/faults", json={"target": "/person/search", "status": 429, "times": 2}).status_code == 200
    assert client.post("/api/faults", json={"target": "nonsense"}).status_code == 400


def test_dashboard_needs_the_password_when_one_is_set(tmp_path, monkeypatch):
    db.close()
    settings = make_settings(tmp_path, demo_password="letmein")
    db.connect(settings.db_path)
    monkeypatch.setattr("app.main.get_settings", lambda: settings)
    with TestClient(app) as locked:
        assert locked.get("/").status_code == 401
        assert locked.post("/api/signups", json={"email": "a@b.co"}).status_code == 401
        assert locked.get("/", auth=("demo", "wrong")).status_code == 401
        assert locked.get("/", auth=("demo", "letmein")).status_code == 200
        assert locked.get("/healthz").status_code == 200
        assert locked.get("/sandbox/nope").status_code == 404, "sandbox links must stay public"
    db.close()


def _outcome(rows):
    attempt = Attempt("Query", "/person/search", {"limit": 10})
    return QueryOutcome(Playbook.RECRUITING, attempt, rows, attempts=[attempt])


def _decision(claim="Because."):
    return Decision(Playbook.RECRUITING, 0.9, [Reason(claim, [])], Slots(), "rules")


def test_hostile_profile_data_is_neutralised_in_the_email(tmp_path):
    rows = [{
        "name": "<script>alert(1)</script>", "title": "<img src=x onerror=alert(1)>", "company": "Co",
        "url": "javascript:alert(1)",
    }]
    _, html = deliver.render_results(make_settings(tmp_path), _decision(), _outcome(rows), "s", "i")
    assert "<script>alert" not in html and "<img src=x" not in html
    assert "javascript:" not in html


def test_email_code_wraps_instead_of_widening_the_layout(tmp_path):
    rows = [{"name": "A", "title": "T", "company": "C", "url": "https://example.com/a"}]
    _, html = deliver.render_results(make_settings(tmp_path), _decision(), _outcome(rows), "s", "i")
    assert "table-layout:fixed" in html
    assert "white-space:pre-wrap" in html and "white-space:pre;" not in html


def test_slack_blocks_stay_inside_slacks_limits():
    brief = {
        "who": "x" * 500, "reasons": [{"claim": "y" * 4000, "citations": []}],
        "credits_per_month": 1234, "assumptions": ["a"], "freshest": "z",
    }
    rendered = blocks(brief)
    assert len(rendered[0]["text"]["text"]) <= 150
    assert all(len(b["text"]["text"]) <= 3000 for b in rendered)


async def test_real_email_path_calls_resend(tmp_path):
    seen = {}

    def transport(request):
        seen["auth"] = request.headers["authorization"]
        seen["body"] = request.content
        return httpx.Response(200, json={"id": "email_123"})

    settings = make_settings(
        tmp_path, crustdata_mode="live", email_live=True, email_api_key="re_x",
        email_from="hello@example.com", email_allowlist="to@example.com",
    )
    result = await Mailer(settings, httpx.MockTransport(transport)).send("TO@example.com", "s", "<p>h</p>")
    assert result.status == "sent" and result.detail == "email_123"
    assert seen["auth"] == "Bearer re_x"


async def test_resend_rejection_is_reported_not_raised(tmp_path):
    settings = make_settings(
        tmp_path, crustdata_mode="live", email_live=True, email_api_key="x", email_from="a@b.co", email_allowlist="*",
    )
    transport = httpx.MockTransport(lambda r: httpx.Response(403, text="domain not verified"))
    result = await Mailer(settings, transport).send("x@y.co", "s", "h")
    assert result.status == "failed" and "domain not verified" in result.detail


async def test_real_slack_path_posts_blocks(tmp_path):
    settings = make_settings(tmp_path, crustdata_mode="live", slack_live=True, slack_webhook_url="https://hooks.example/x")
    transport = httpx.MockTransport(lambda r: httpx.Response(200, text="ok"))
    brief = {"who": "a", "reasons": [], "credits_per_month": 1, "assumptions": [], "freshest": "f"}
    assert (await SlackNotifier(settings, transport).post(brief))[0] == "sent"
