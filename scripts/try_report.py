#!/usr/bin/env python
"""Runs a profile report through the real pipeline, shows every step, and sends nothing.

    uv run python scripts/try_report.py example_user.md
    uv run python scripts/try_report.py example_user.md --persona crustdata

It stands in for the ReAct loop by handing over the file you give it, so you can check a report
without running the loop. It uses Claude and Crustdata from .env, so it spends real credits: about
2 for company research and 2 to 3 for the first search. Email and Slack are forced off, because
reports are about real people. The email is saved to cache/try_report/.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
import types
from html import unescape
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "cache" / "try_report"


def plain_text(html: str) -> str:
    text = re.sub(r"<(style|script)[^>]*>.*?</\1>", "", html, flags=re.S)
    text = re.sub(r"</(p|h1|h2|li|tr|table|pre)>|<br\s*/?>", "\n", text)
    text = re.sub(r"</t[dh]>", "  |  ", text)
    text = unescape(re.sub(r"<[^>]+>", "", text))
    return re.sub(r"\n\s*\n+", "\n\n", re.sub(r"[ \t]+", " ", text)).strip()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("report", type=Path)
    parser.add_argument("--persona", default=os.environ.get("VENDOR_PERSONA", "supabase"))
    args = parser.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    database = OUT / f"{args.persona}.sqlite"
    for path in OUT.glob(f"{args.persona}.sqlite*"):
        path.unlink()
    os.environ.update({
        "EMAIL_LIVE": "false", "SLACK_LIVE": "false", "PROFILE_BUILDER": "react", "FAST_PATH": "false", "DB_PATH": str(database),
        "APP_BASE_URL": os.environ.get("APP_BASE_URL", "http://localhost:8000"),
    })

    sys.path.insert(0, str(ROOT))
    import yaml

    from app import db
    from app.config import get_settings
    from app.pipeline import run
    from app.services import build_services

    text = args.report.read_text()
    meta = yaml.safe_load(text.split("---\n", 2)[1])

    async def build_report(client, ctx, email, name, persona) -> str:
        return text

    module = types.ModuleType("app.pipeline.react_profiles")
    module.build_report = build_report
    sys.modules["app.pipeline.react_profiles"] = module

    async def go() -> None:
        settings = get_settings()
        db.connect(settings.db_path)
        services = build_services(settings)
        started = time.perf_counter()
        states: list[tuple[float, str]] = []
        set_state = run.set_state

        def traced(signup_id, status=None, **fields):
            if status is not None:
                states.append((time.perf_counter() - started, status.value))
            return set_state(signup_id, status, **fields)

        run.set_state = traced
        seen: dict = {}
        classify = services.llm.classify

        async def traced_classify(signup_id, table, domain, persona):
            seen["rows"] = table.as_prompt_rows()
            output = await classify(signup_id, table, domain, persona)
            seen["output"] = output
            return output

        services.llm.classify = traced_classify
        signup = db.create_signup(meta["email"], name=meta.get("name_given", ""), persona_id=args.persona)
        await run.process_signup(services, signup["id"])
        result = db.get_signup(signup["id"])
        decision = result.get("decision") or {}

        print(f"\n== Result: {result['status']} in {time.perf_counter() - started:.1f} s, "
              f"{db.credits_for(signup['id']):.2f} credits. {result.get('route', '')}")
        print("\n== States")
        for at, state in states:
            print(f"  {at:6.2f} s  {state}")
        print("\n== Every call")
        for call in reversed(db.list_calls(signup["id"])):
            credits = call.get("credits") or 0
            print(f"  {call['kind'][:8]:8} {call['endpoint'][:34]:34} {call.get('status') or '':<4} "
                  f"{call.get('latency_ms') or '':>6} {credits:>5}  {(call.get('note') or '')[:90]}")
        rows = seen.get("rows", [])
        print(f"\n== Evidence Claude saw: {len(rows)} rows")
        for row in rows:
            print(f"  [{row['source'][:48]}] {row['value'][:110]}")
        print("\n== Claude's decision")
        if "output" in seen:
            print(f"  raw: {seen['output'].segment}, confidence {seen['output'].confidence}")
            print(f"  opening: {seen['output'].opening.claim}")
        print(f"  kept: {json.dumps({k: decision.get(k) for k in ('segment', 'playbook', 'confidence', 'decided_by')})}")
        print(f"  opening kept: {bool(decision.get('opening'))}")
        for reason in decision.get("reasons", []):
            print(f"  reason: {reason['claim']} {[c['id'] for c in reason['citations']]}")
        print(f"  subject: {decision.get('subject')}")
        for pick in decision.get("picks", []):
            print(f"  pick: {pick['service']}: {pick['claim']} {[c['id'] for c in pick['citations']]}")
        print(f"  slots: { {k: v for k, v in (decision.get('slots') or {}).items() if v} }")
        print("\n== Searches")
        for attempt in result.get("attempts") or []:
            print(f"  {attempt['label']}: {attempt['result_count']} of {attempt['total_count']}, {attempt['credits']} credits")
        print(f"  pills: {[p['label'] for p in result.get('pills') or []]}")
        if result.get("fallback_reason"):
            print(f"\n== FELL BACK to the basic profile: {result['fallback_reason']}")
        print(f"\n== Sales: {result.get('sales_alert')}. Email status: {result.get('email_status')} ({result.get('email_detail')})")
        html = result.get("email_html") or ""
        path = OUT / f"{args.persona}.html"
        path.write_text(html)
        print(f"\n== Email, saved to {path.relative_to(ROOT)}")
        print(f"Subject: {result.get('email_subject')}\n")
        print(plain_text(html))

    asyncio.run(go())


if __name__ == "__main__":
    main()
