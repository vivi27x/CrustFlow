"""HTTP surface: the dashboard and its API, the sandbox, and the intent page."""

from __future__ import annotations

import asyncio
import json
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import parse_qs

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response, StreamingResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from app import db, events, kpis
from app.config import get_settings
from app.delivery import snippets
from app.delivery.slack import blocks as slack_blocks
from app.models import Status
from app.pipeline import highlight, run, watcher
from app.services import Services, build_services

BASE = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=BASE / "templates")

SIM_DEMO = ["priya@hirefast.in|Priya Sharma", "rohan.codes@gmail.com|Rohan Kulkarni", "priya.sharma.dev@gmail.com|Priya Sharma"]
SIM_MATRIX = [
    "priya@hirefast.in|Priya Sharma", "priya.sharma.dev@gmail.com|Priya Sharma", "rohan.codes@gmail.com|Rohan Kulkarni",
    "prof.rao@iitk.ac.in",
    "jane@crustdata.com", "throwaway@mailinator.com", "not-an-email", "founder@brand-new-startup.xyz",
]
STAGGER_S = 0.4
OWN_SEARCH = "own_search"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    db.connect()
    app.state.services = build_services(settings)
    # Not spawned: drain() waits for every spawned task, and this one never ends.
    poller = asyncio.create_task(watcher.poll_forever(app.state.services)) if settings.watch_poll_s > 0 else None
    yield
    if poller:
        poller.cancel()
    await app.state.services.drain()
    await app.state.services.client.close()
    db.close()


app = FastAPI(title="CrustFlow", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
basic = HTTPBasic(auto_error=False)


def services(request: Request) -> Services:
    return request.app.state.services


def require_demo(credentials: HTTPBasicCredentials | None = Depends(basic)) -> None:
    """Signups spend real credits, so the dashboard is protected. Sandbox links stay public."""
    password = get_settings().demo_password
    if not password:
        return
    if credentials is None or not secrets.compare_digest(credentials.password, password):
        raise HTTPException(401, "Password required", headers={"WWW-Authenticate": "Basic"})


Protected = Depends(require_demo)


class SignupIn(BaseModel):
    email: str
    name: str = ""
    persona: str = ""


class FaultIn(BaseModel):
    target: str
    status: int = 500
    times: int = 1


def mode_info(s: Services) -> dict[str, Any]:
    labels = {
        "sim": "Simulated data",
        "live": "Live Crustdata API",
        "record": "Live, recording",
        "replay": "Replaying a recorded run",
    }
    label = labels.get(s.settings.crustdata_mode, s.settings.crustdata_mode)
    if s.settings.crustdata_mode in ("live", "record") and not s.settings.crustdata_api_key:
        label += " (no key set: calls will fail)"
    return {
        "mode": s.settings.crustdata_mode,
        "label": label,
        "llm": s.settings.llm_model if s.llm.available else "Rules only (no Claude key)",
        "email_live": s.settings.email_live,
        "persona": s.settings.vendor_persona,
        "personas": [{"id": p.id, "name": p.name} for p in s.personas.values()],
        "slack_live": s.settings.slack_live,
        "watch": watch_label(s),
    }


def watch_label(s: Services) -> str:
    if not s.settings.watch_founders:
        return "off"
    if s.settings.uses_simulator:
        return "simulated"
    return "live" if s.settings.watch_live else "planned only (WATCH_LIVE is off)"


def demo_list(value: str, fallback: list[str]) -> list[tuple[str, str]]:
    """Comma-separated `email|Full Name` entries."""
    items = [v.strip() for v in value.split(",") if v.strip()] or fallback
    return [(item.partition("|")[0].strip(), item.partition("|")[2].strip()) for item in items]


async def _start_later(s: Services, signup_id: str, delay: float) -> None:
    await asyncio.sleep(delay)
    await run.process_signup(s, signup_id)


def start_signups(s: Services, signups: list[tuple[str, str]], persona: str = "") -> list[str]:
    ids = []
    for index, (email, name) in enumerate(signups):
        signup = db.create_signup(email, name=name, persona_id=persona if persona in s.personas else "")
        events.publish("signup", run.summary(signup))
        # Staggered so the same person's two emails land in a readable order.
        s.spawn(_start_later(s, signup["id"], index * STAGGER_S))
        ids.append(signup["id"])
    return ids


# ---- Dashboard ----


@app.get("/", response_class=HTMLResponse, dependencies=[Protected])
async def dashboard(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "dashboard.html", {"mode": mode_info(services(request))})


@app.get("/api/state", dependencies=[Protected])
async def state(request: Request) -> dict[str, Any]:
    return {
        "mode": mode_info(services(request)),
        "signups": [run.summary(s) for s in db.list_signups()],
        "kpis": kpis.compute(),
        "human_queue": db.list_human_items(),
        "calls": db.list_calls(limit=200),
    }


@app.get("/api/kpis", dependencies=[Protected])
async def get_kpis() -> dict[str, Any]:
    return {"kpis": kpis.compute(), "human_queue": db.list_human_items()}


@app.get("/api/signups/{signup_id}", dependencies=[Protected])
async def signup_detail(signup_id: str, request: Request) -> dict[str, Any]:
    signup = db.get_signup(signup_id)
    if signup is None:
        raise HTTPException(404, "No such signup")
    signup["has_email"] = bool(signup.pop("email_html", None))
    signup["persona_name"] = run.persona_of(services(request), signup).name
    signup["credits"] = round(db.credits_for(signup_id), 2)
    signup["sandbox_credits"] = round(db.credits_for(signup_id, kind="sandbox"), 2)
    signup["calls"] = db.list_calls(signup_id)
    for pill in signup.get("pills", []):
        pill.pop("body", None)
    return signup


@app.get("/api/signups/{signup_id}/email", response_class=HTMLResponse, dependencies=[Protected])
async def signup_email(signup_id: str) -> HTMLResponse:
    signup = db.get_signup(signup_id)
    if signup is None or not signup.get("email_html"):
        raise HTTPException(404, "No email for this signup")
    return HTMLResponse(signup["email_html"])


@app.post("/api/signups", dependencies=[Protected])
async def create_signup(payload: SignupIn, request: Request) -> dict[str, Any]:
    return {"ids": start_signups(services(request), [(payload.email, payload.name)], payload.persona)}


@app.post("/api/demo/{name}", dependencies=[Protected])
async def run_demo(name: str, request: Request) -> dict[str, Any]:
    s = services(request)
    if name == "three":
        signups = demo_list(s.settings.demo_signups, SIM_DEMO)
    elif name == "matrix":
        signups = demo_list(s.settings.matrix_signups, SIM_MATRIX)
    else:
        raise HTTPException(404, "Unknown demo")
    return {"ids": start_signups(s, signups)}


@app.post("/api/reset", dependencies=[Protected])
async def reset(request: Request) -> dict[str, bool]:
    s = services(request)
    await s.drain()
    s.client.faults.clear()
    s.llm.fail_next = s.mailer.fail_next = s.slack.fail_next = 0
    # Simulated credits cost nothing, and a cold cache shows true per-signup numbers in the demo.
    db.reset_demo(clear_cache=s.settings.uses_simulator)
    events.publish("reset", {})
    return {"ok": True}


@app.post("/api/faults", dependencies=[Protected])
async def add_fault(fault: FaultIn, request: Request) -> dict[str, bool]:
    s = services(request)
    if fault.target == "llm":
        s.llm.fail_next += fault.times
    elif fault.target == "email":
        s.mailer.fail_next += fault.times
    elif fault.target == "slack":
        s.slack.fail_next += fault.times
    elif fault.target.startswith("/"):
        s.client.faults.add(fault.target, fault.status, fault.times)
    else:
        raise HTTPException(400, "Unknown fault target")
    events.publish("call", db.log_call(
        signup_id=None, kind="agent", endpoint="failure drill",
        note=f"Armed: {fault.status if fault.target.startswith('/') else 'failure'} on {fault.target}, {fault.times}x",
    ))
    return {"ok": True}


@app.get("/events", dependencies=[Protected])
async def stream(request: Request) -> StreamingResponse:
    queue = events.subscribe()

    async def generate() -> AsyncIterator[str]:
        try:
            yield ": connected\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    message = await asyncio.wait_for(queue.get(), timeout=15)
                except TimeoutError:
                    yield ": keep-alive\n\n"
                    continue
                yield f"event: {message['event']}\ndata: {json.dumps(message['data'], default=str)}\n\n"
        finally:
            events.unsubscribe(queue)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/healthz")
async def health() -> dict[str, str]:
    return {"status": "ok"}


# ---- Sandbox ----


def not_found_page(request: Request, message: str) -> HTMLResponse:
    return templates.TemplateResponse(request, "message.html", {"message": message}, status_code=404)


@app.get("/sandbox/{token}", response_class=HTMLResponse)
async def sandbox(token: str, request: Request) -> HTMLResponse:
    signup = db.find_signup_by_token("sandbox_token", token)
    if signup is None:
        return not_found_page(request, "This sandbox link has expired. Check your latest email for a new one.")
    if signup.get("starter"):
        persona = run.persona_of(services(request), signup)
        return templates.TemplateResponse(request, "sandbox_starter.html", {
            "token": token, "starter": signup["starter"], "vendor": persona.name, "sample": persona.sample,
            "activated": bool(signup.get("activated_at")),
            "sql_html": highlight.sql(signup["starter"]["sql"]),
            "project_html": highlight.sql(signup["starter"]["project_only_sql"]),
        })
    request_info = signup["request"]
    context = {
        "token": token,
        "columns": signup["result_columns"],
        "rows": signup["result_rows"],
        "pills": [{k: p[k] for k in ("id", "label", "hint")} for p in signup.get("pills", [])],
        "request_json": json.dumps(request_info["body"], indent=2),
        "endpoint": request_info["endpoint"],
        "curl": snippets.curl(request_info["endpoint"], request_info["body"]),
        "python": snippets.python(request_info["endpoint"], request_info["body"]),
        "activated": bool(signup.get("activated_at")),
        "mode": mode_info(services(request)),
    }
    return templates.TemplateResponse(request, "sandbox.html", context)


@app.post("/sandbox/{token}/pills/{pill_id}")
async def sandbox_pill(token: str, pill_id: str, request: Request) -> dict[str, Any]:
    """The browser sends an ID. The request body only ever comes from the server."""
    signup = db.find_signup_by_token("sandbox_token", token)
    if signup is None:
        raise HTTPException(404, "Unknown sandbox")
    pill = next((p for p in signup.get("pills", []) if p["id"] == pill_id), None)
    if pill is None:
        raise HTTPException(404, "Unknown pill")
    result = await run.run_pill(services(request), signup, pill)
    if result.get("ok"):
        request_info = result["request"]
        result["request_json"] = json.dumps(request_info["body"], indent=2)
        result["curl"] = snippets.curl(request_info["endpoint"], request_info["body"])
        result["python"] = snippets.python(request_info["endpoint"], request_info["body"])
    return result


class StarterRun(BaseModel):
    pill: str


@app.post("/sandbox/{token}/ran")
async def starter_ran(token: str, payload: StarterRun) -> dict[str, bool]:
    """The browser did the work, so this costs nothing. It records that they came back for a second run."""
    signup = db.find_signup_by_token("sandbox_token", token)
    starter = signup.get("starter") if signup else None
    if not signup or not starter:
        raise HTTPException(404, "Unknown sandbox")
    labels = {OWN_SEARCH: "Their own search", **{p["id"]: p["label"] for p in starter["pills"]}}
    if payload.pill not in labels:
        raise HTTPException(404, "Unknown pill")
    run.mark_activated(signup, labels[payload.pill])
    return {"ok": True}


# ---- Intent ----


@app.get("/intent/{token}", response_class=HTMLResponse)
async def intent_page(token: str, request: Request, choice: str = "", running: int = 0) -> Response:
    """A GET never runs anything: mail scanners open links, and this page spends credits."""
    signup = db.find_signup_by_token("intent_token", token)
    if signup is None:
        return not_found_page(request, "This link has expired.")
    if running:
        status = Status(signup["status"])
        finished = status in (
            Status.DELIVERED, Status.ACTIVATED, Status.AWAITING_INTENT, Status.FAILED_SAFE, Status.NURTURE,
        )
        if finished and signup.get("sandbox_token"):
            return RedirectResponse(f"/sandbox/{signup['sandbox_token']}", status_code=303)
        if status in (Status.DELIVERED, Status.NURTURE):
            return templates.TemplateResponse(request, "message.html", {"message": "Done. Check your email."})
        if finished and signup.get("intent_runs"):
            return templates.TemplateResponse(request, "message.html", {
                "message": "We couldn't find enough results for that yet. Our team will take a look.",
            })
    labels = run.persona_of(services(request), signup).buttons()
    return templates.TemplateResponse(request, "intent.html", {
        "token": token, "labels": labels, "choice": choice if choice in labels else "", "running": bool(running),
        "exhausted": int(signup.get("intent_runs", 0)) >= run.MAX_INTENT_RUNS,
    })


@app.post("/intent/{token}")
async def intent_submit(token: str, request: Request) -> RedirectResponse:
    signup = db.find_signup_by_token("intent_token", token)
    if signup is None:
        raise HTTPException(404, "Unknown link")
    choice = parse_qs((await request.body()).decode()).get("choice", [""])[0]
    s = services(request)
    if choice not in run.persona_of(s, signup).buttons():
        raise HTTPException(400, "Unknown choice")
    if int(signup.get("intent_runs", 0)) < run.MAX_INTENT_RUNS:
        run.set_state(signup["id"], Status.QUERYING, sandbox_token="")
        s.spawn(run.process_intent(s, signup["id"], choice))
    return RedirectResponse(f"/intent/{token}?running=1", status_code=303)


# ---- Founder watch ----


@app.get("/api/signups/{signup_id}/watch", dependencies=[Protected])
async def signup_watch(signup_id: str, request: Request) -> dict[str, Any]:
    if db.get_signup(signup_id) is None:
        raise HTTPException(404, "No such signup")
    s = services(request)
    return {**watcher.summary(signup_id), "mode": watch_label(s), "can_simulate": s.settings.uses_simulator}


@app.get("/api/signups/{signup_id}/user.md", dependencies=[Protected])
async def signup_user_md(signup_id: str) -> Response:
    signup = db.get_signup(signup_id)
    text = signup and (signup.get("user_md") or signup.get("profile_report"))
    if not text:
        raise HTTPException(404, "No user.md for this signup yet")
    return Response(text, media_type="text/markdown; charset=utf-8")


@app.get("/api/watch/events/{uid}/email", response_class=HTMLResponse, dependencies=[Protected])
async def signal_email(uid: str) -> HTMLResponse:
    event = db.get_watch_event(uid)
    if event is None or not event.get("email_html"):
        raise HTTPException(404, "No email for this activity")
    return HTMLResponse(event["email_html"])


@app.post("/api/watch/poll", dependencies=[Protected])
async def watch_poll(request: Request) -> dict[str, int]:
    return {"found": await watcher.poll(services(request))}


@app.post("/api/watches/{watch_id}/simulate", dependencies=[Protected])
async def watch_simulate(watch_id: int, request: Request, kind: str = "funding") -> dict[str, int]:
    s = services(request)
    if not s.settings.uses_simulator:
        raise HTTPException(400, "Activity can only be simulated on simulated data")
    try:
        return {"found": await watcher.simulate(s, watch_id, kind)}
    except (KeyError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/watch/stop/{token}", response_class=HTMLResponse)
async def watch_stop_page(token: str, request: Request) -> HTMLResponse:
    """A GET never stops anything: mail scanners open links."""
    signup = db.find_signup_by_token("stop_token", token) if token else None
    if signup is None:
        return not_found_page(request, "This link has expired.")
    vendor = run.persona_of(services(request), signup).name
    return templates.TemplateResponse(request, "stop.html", {"vendor": vendor, "stopped": bool(signup.get("watch_stopped"))})


@app.post("/watch/stop/{token}", response_class=HTMLResponse)
async def watch_stop(token: str, request: Request) -> HTMLResponse:
    signup = db.find_signup_by_token("stop_token", token) if token else None
    if signup is None:
        raise HTTPException(404, "Unknown link")
    s = services(request)
    await watcher.stop(s, signup["id"])
    return templates.TemplateResponse(request, "stop.html", {"vendor": run.persona_of(s, signup).name, "stopped": True})


@app.get("/api/signups/{signup_id}/slack", dependencies=[Protected])
async def slack_payload(signup_id: str) -> dict[str, Any]:
    signup = db.get_signup(signup_id)
    if signup is None or not signup.get("brief"):
        raise HTTPException(404, "No brief for this signup")
    return {"blocks": slack_blocks(signup["brief"])}
