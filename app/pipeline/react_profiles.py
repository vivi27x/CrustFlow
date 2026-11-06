"""Connects the profile agent to the pipeline.

`profile_agent` is a separate package that runs on Claude Code and writes user.md. This turns one
run of it into the `build_report` that `run.py` calls, and keeps its work visible and accounted for.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from app import db, events
from app.crustdata.client import CrustdataClient, RunContext
from app.personas import VendorPersona

CREDIT_CAP = 8.0  # Leaves room in a signup's 15 for company research and any first search.
POLL_S = 1.0


class AgentFailed(RuntimeError):
    """The agent finished with no report, so the basic builder takes over."""


def _note(ctx: RunContext, note: str) -> None:
    events.publish("call", db.log_call(signup_id=ctx.signup_id, kind="agent", endpoint="profile agent", note=note[:160]))


def _describe(entry: dict[str, Any]) -> str:
    tools = entry.get("crustdata_tools")
    if tools:
        return f"Crustdata: {', '.join(sorted(set(tools)))}"
    name = str(entry.get("tool", "")).replace("profile__", "")
    target = entry.get("input")
    if isinstance(target, dict):
        target = target.get("query") or target.get("url") or ""
    return f"{name}: {target}" if target else name


async def _show_progress(agent: Any, ctx: RunContext) -> None:
    """A report takes about two minutes, so the dashboard shows each step as it happens."""
    shown = 0
    while True:
        for entry in agent.log[shown:]:
            blocked = "" if entry.get("allowed", True) else " (blocked by the guard)"
            _note(ctx, _describe(entry) + blocked)
        shown = len(agent.log)
        await asyncio.sleep(POLL_S)


def _charge(ctx: RunContext, spent: float, note: str) -> None:
    if spent:
        ctx.ledger.charge(spent)
        events.publish("call", db.log_call(
            signup_id=ctx.signup_id, kind="crustdata", endpoint="profile report", status=200, credits=spent,
            note=note,
        ))


async def build_report(
    client: CrustdataClient, ctx: RunContext, email: str, name: str, persona: VendorPersona
) -> str:
    if client.settings.uses_simulator:
        raise AgentFailed("the agent uses real services, so it doesn't run on simulated data")

    from profile_agent.agent import ProfileAgent
    from profile_agent.config import settings as agent_settings

    try:
        config = agent_settings(max_credits=CREDIT_CAP)
    except SystemExit as exc:  # It exits on a missing key, which would otherwise stop the whole server.
        raise AgentFailed(str(exc)) from exc

    agent = ProfileAgent(config, email, name or None)
    progress = asyncio.create_task(_show_progress(agent, ctx))
    try:
        run = await agent.run()
    except (Exception, asyncio.CancelledError):
        _charge(ctx, agent.budget.spent, "credits the profile agent spent before it stopped")
        if agent.saved:  # It saved the report, then ran out of time closing down. Don't waste it.
            _note(ctx, "report saved just before the time limit, so we kept it")
            return agent.saved.read_text()
        raise
    finally:
        progress.cancel()
    _charge(ctx, run.get("crustdata_credits") or 0, "credits the profile agent spent")
    if not run.get("user_md"):
        raise AgentFailed(str(run.get("error") or "no report saved"))
    _note(ctx, f"report written in {run.get('seconds')} s, ${run.get('claude_usd') or 0:.2f} of Claude")
    return Path(run["user_md"]).read_text()
