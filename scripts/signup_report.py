#!/usr/bin/env python
"""Writes a report of how the pipeline handled each signup: input, reasoning and the email.

    uv run python scripts/signup_report.py
    uv run python scripts/signup_report.py --db cache/live.sqlite --out cache/reports/older.html

It reads a database without changing it. Reports hold real people's data, so they go in cache/,
which is gitignored. Never commit one.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import time
from html import escape
from pathlib import Path
from typing import Any

STYLE = """
:root { --ink:#16172a; --muted:#666a7a; --line:#e6e4ee; --panel:#fff; --bg:#f6f5f1; --accent:#5546e8;
  --ok:#17693d; --ok-soft:#e4f4ea; --warn:#8a5a00; --warn-soft:#f8ecd0; --bad:#a3241d; --bad-soft:#fbe7e5; --info-soft:#ececfd; }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--ink); font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif; }
main { max-width:1040px; margin:0 auto; padding:32px 20px 80px; }
h1 { margin:0 0 4px; font-size:28px; line-height:1.2; } h2 { margin:0; font-size:21px; line-height:1.3; }
h3 { margin:26px 0 8px; font-size:12px; letter-spacing:.06em; text-transform:uppercase; color:var(--muted); }
p { margin:0 0 10px; } .muted { color:var(--muted); } .small { font-size:13px; }
.card { background:var(--panel); border:1px solid var(--line); border-radius:12px; padding:22px 24px; margin-top:22px; }
.head { display:flex; flex-wrap:wrap; gap:10px; align-items:baseline; justify-content:space-between; }
.chip { display:inline-block; padding:2px 10px; border-radius:11px; font-size:12px; font-weight:600; background:var(--info-soft); color:var(--accent); white-space:nowrap; }
.chip.ok { background:var(--ok-soft); color:var(--ok); } .chip.warn { background:var(--warn-soft); color:var(--warn); } .chip.bad { background:var(--bad-soft); color:var(--bad); }
.wrap { overflow-x:auto; } table { width:100%; border-collapse:collapse; font-size:14px; }
th { text-align:left; font-size:12px; color:var(--muted); font-weight:600; padding:6px 10px 6px 0; border-bottom:2px solid var(--line); white-space:nowrap; }
td { padding:7px 10px 7px 0; border-bottom:1px solid var(--line); vertical-align:top; }
td.num { font-variant-numeric:tabular-nums; white-space:nowrap; } .id { color:var(--muted); font-size:12px; white-space:nowrap; }
dl { display:grid; grid-template-columns:150px minmax(0,1fr); gap:4px 14px; margin:0; font-size:14px; } dt { color:var(--muted); } dd { margin:0; overflow-wrap:anywhere; }
.note { padding:9px 12px; border-radius:8px; background:var(--warn-soft); color:var(--warn); font-size:14px; margin:14px 0 0; }
.why { padding:10px 14px; border-left:3px solid var(--accent); background:#f8f7ff; border-radius:0 8px 8px 0; font-size:14px; }
.cite { display:block; font-size:12.5px; color:var(--muted); overflow-wrap:anywhere; } ul.plain { margin:0; padding-left:18px; } ul.plain li { margin-bottom:9px; }
details { margin-top:8px; font-size:14px; } summary { cursor:pointer; color:var(--accent); font-weight:600; }
iframe { width:100%; height:640px; border:1px solid var(--line); border-radius:10px; background:#f3f2ee; margin-top:8px; }
pre { white-space:pre-wrap; overflow-wrap:anywhere; font-size:12.5px; background:#f6f5f1; padding:12px; border-radius:8px; max-height:420px; overflow:auto; }
@media (max-width:640px) { main { padding:20px 14px 60px; } .card { padding:16px; } dl { grid-template-columns:1fr; } dt { margin-top:6px; } }
"""

OUTCOMES = {
    "delivered": ("Email delivered", "ok"), "activated": ("Activated", "ok"), "awaiting_intent": ("Asked with buttons", "warn"),
    "nurture": ("Free guide", "warn"), "human_queue": ("Held for a person", "bad"), "failed_safe": ("Failed safely", "bad"),
}


def e(value: Any) -> str:
    return escape(str(value if value is not None else ""))


def load(path: Path) -> list[dict[str, Any]]:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    signups = []
    for row in connection.execute("SELECT * FROM signups ORDER BY created_at"):
        signup = dict(row)
        signup.update(json.loads(signup.pop("data")))
        calls = connection.execute("SELECT * FROM calls WHERE signup_id = ? ORDER BY ts, id", (signup["id"],)).fetchall()
        signup["calls"] = [dict(call) for call in calls]
        signups.append(signup)
    connection.close()
    return signups


def credits(signup: dict[str, Any], kind: str = "crustdata") -> float:
    return round(sum(c["credits"] or 0 for c in signup["calls"] if c["kind"] == kind), 2)


def agent_seconds(signup: dict[str, Any]) -> int | None:
    """From the app's own log, so a later test run can't change this signup's numbers."""
    calls = signup["calls"]
    start = next((c["ts"] for c in calls if c["note"] == "profile agent started"), None)
    end = next((c["ts"] for c in calls if c["endpoint"] == "profile report" and c["credits"]), None)
    return round(end - start) if start and end else None


def claude_cost(signup: dict[str, Any]) -> str:
    for call in signup["calls"]:
        found = re.search(r"\$([0-9.]+) of Claude", call["note"] or "")
        if found:
            return f"${found.group(1)}"
    return "not recorded"


def total_seconds(signup: dict[str, Any]) -> int | None:
    calls = signup["calls"]
    return round(calls[-1]["ts"] - signup["created_at"]) if calls else None


def picks_text(signup: dict[str, Any]) -> str:
    picks = (signup.get("decision") or {}).get("picks") or []
    return ", ".join(p["service"].replace("_", " ") for p in picks) or "none"


def explain(signup: dict[str, Any]) -> str:
    """Why this outcome, in words, from the same values the pipeline used."""
    decision = signup.get("decision") or {}
    confidence = decision.get("confidence")
    if signup.get("status") == "human_queue":
        return f"Held for a person: {signup.get('route_detail') or signup.get('route')}"
    if signup.get("status") == "awaiting_intent":
        if confidence is None:
            return "We couldn't work out who this is, so nothing went to Claude. They were asked with the company's buttons, which is cheaper and safer than guessing."
        return f"Claude's confidence was {confidence:.0%}, below our 60% minimum, so we asked with buttons instead of sending a guess."
    if not decision:
        return str(signup.get("route") or "")
    parts = [f"Claude's confidence was {confidence:.0%}, above our 60% minimum, so the email went out."]
    if signup.get("sales_alert") == "sent":
        parts.append("The segment is marked for sales and the company is big enough, so a sales brief was written.")
    else:
        parts.append("Sales wasn't alerted, because this segment or company size doesn't call for it.")
    return " ".join(parts)


def timeline(signup: dict[str, Any]) -> str:
    rows = []
    for call in signup["calls"]:
        offset = call["ts"] - signup["created_at"]
        latency = f"{call['latency_ms']} ms" if call["latency_ms"] else ""
        rows.append(
            f"<tr><td class='num'>{offset:.1f} s</td><td>{e(call['endpoint'])}</td><td class='num'>{e(call['status'] or '')}</td>"
            f"<td class='num'>{latency}</td><td class='num'>{call['credits'] or ''}</td><td>{e(call['note'])}</td></tr>"
        )
    return (
        "<details><summary>Every step, in order</summary><div class='wrap'><table><tr><th>When</th><th>Step</th><th>Status</th>"
        f"<th>Took</th><th>Credits</th><th>Note</th></tr>{''.join(rows)}</table></div></details>"
    )


def evidence_table(signup: dict[str, Any]) -> str:
    rows = "".join(
        f"<tr><td class='id'>{e(item.get('id'))}</td><td>{e(item.get('value'))}</td>"
        f"<td class='small muted'>{e(item.get('source'))}, {e(item.get('field'))}{', ' + e(item.get('date')) if item.get('date') else ''}</td></tr>"
        for item in signup.get("evidence") or []
    )
    count = len(signup.get("evidence") or [])
    return (
        f"<details><summary>All {count} facts Claude was given</summary><div class='wrap'><table><tr><th>Row</th><th>Fact</th>"
        f"<th>Source</th></tr>{rows}</table></div></details>"
    )


def cited(item: dict[str, Any]) -> str:
    return "".join(f"<span class='cite'>{e(c.get('id'))}: {e(c.get('value'))[:150]}</span>" for c in item.get("citations") or [])


def decision_block(signup: dict[str, Any]) -> str:
    decision = signup.get("decision")
    if not decision:
        return "<h3>Claude's reasoning</h3><p class='muted'>Claude wasn't called, because we didn't know enough about this person.</p>"
    by = {"llm": "Claude", "rules": "our rules", "rules-fallback": "our rules, after Claude failed", "intent": "the signup, by clicking a button"}
    html = [
        "<h3>Claude's reasoning</h3><dl>",
        f"<dt>Decided by</dt><dd>{e(by.get(decision.get('decided_by'), decision.get('decided_by')))}</dd>",
        f"<dt>Segment</dt><dd>{e(decision.get('segment'))}</dd>",
        f"<dt>Confidence</dt><dd>{decision.get('confidence', 0):.0%}</dd>",
        f"<dt>Subject it wrote</dt><dd>{e(decision.get('subject')) or 'none'}</dd></dl>",
    ]
    if decision.get("reasons"):
        items = "".join(f"<li>{e(r['claim'])}{cited(r)}</li>" for r in decision["reasons"])
        html.append(f"<p class='small muted' style='margin-top:14px'>Why this segment, with the evidence it cited:</p><ul class='plain'>{items}</ul>")
    if decision.get("opening"):
        html.append(f"<p class='small muted' style='margin-top:14px'>Opening it wrote:</p><p>{e(decision['opening']['claim'])}{cited(decision['opening'])}</p>")
    if decision.get("picks"):
        items = "".join(f"<li><strong>{e(p['service'].replace('_', ' '))}:</strong> {e(p['claim'])}{cited(p)}</li>" for p in decision["picks"])
        html.append(f"<p class='small muted' style='margin-top:14px'>Services it picked, and why:</p><ul class='plain'>{items}</ul>")
    if decision.get("sales_angle"):
        html.append(f"<p class='small muted' style='margin-top:14px'>Sales angle:</p><p>{e(decision['sales_angle']['claim'])}{cited(decision['sales_angle'])}</p>")
    starter = signup.get("starter")
    if starter:
        pills = ", ".join(p["label"] for p in starter["pills"])
        html.append(
            f"<p class='small muted' style='margin-top:14px'>Starter compiled from Claude's spec: <code>{e(starter['table'])}</code>. "
            f"Sandbox buttons: {e(pills)}. Activated: {'yes, ' + e(signup.get('activated_pill')) if signup.get('activated_at') else 'not yet'}.</p>"
            f"<pre style='white-space:pre-wrap;font-size:12px'>{e(starter['sql'])}</pre>"
        )
    return "".join(html)


def identity_block(signup: dict[str, Any]) -> str:
    person, company = signup.get("person") or {}, signup.get("company") or {}
    meta = signup.get("profile_meta") or {}
    source = "Profile agent's report" if person.get("match_method") == "report" else "Basic builder"
    if person.get("status") not in ("matched", None) or not person:
        source = "Not identified"
    rows = [
        ("Built by", source), ("Person", person.get("name") or "not found"), ("Title", person.get("title") or "not found"),
        ("Company", f"{company.get('name') or 'not found'}" + (f", {company['headcount']} people" if company.get("headcount") else "")),
        ("How we're sure", f"{person.get('match_note') or 'no match'} (confidence {person.get('confidence') or 0:.2f})"),
    ]
    if meta.get("matched_on"):
        rows.append(("Agent's match", meta["matched_on"]))
    if meta.get("tools"):
        rows.append(("Agent used", ", ".join(meta["tools"])))
    return "<h3>Who we decided this is</h3><dl>" + "".join(f"<dt>{e(k)}</dt><dd>{e(v)}</dd>" for k, v in rows) + "</dl>"


def signup_card(signup: dict[str, Any], index: int) -> str:
    label, tone = OUTCOMES.get(signup["status"], (signup["status"], ""))
    html = [f"<section class='card' id='s{index}'><div class='head'><h2>{index}. {e(signup.get('name') or 'No name given')}</h2>"
            f"<span><span class='chip {tone}'>{e(label)}</span> <span class='chip'>{e(signup.get('persona_id') or 'default')}</span></span></div>"]
    moment = time.strftime("%H:%M:%S", time.localtime(signup["created_at"]))
    seconds = agent_seconds(signup)
    html.append(
        f"<h3>Input</h3><dl><dt>Name typed</dt><dd>{e(signup.get('name') or '(none)')}</dd><dt>Email typed</dt><dd>{e((signup.get('email_raw') or '').strip())}</dd>"
        f"<dt>Signed up to</dt><dd>{e(signup.get('persona_id') or 'default')}</dd><dt>Time</dt><dd>{moment}</dd></dl>"
    )
    if signup.get("fallback_reason"):
        html.append(f"<p class='note'><strong>Basic profile used:</strong> {e(signup['fallback_reason'])}.</p>")
    elif not signup.get("profile_report") and any(c["note"] and "basic builder took over" in c["note"] for c in signup["calls"]):
        reason = next(c["note"] for c in signup["calls"] if c["note"] and "basic builder took over" in c["note"])
        html.append(f"<p class='note'><strong>Basic profile used:</strong> {e(reason)}. This ran before the queue fix.</p>")
    html.append(identity_block(signup))
    html.append(evidence_table(signup))
    html.append(decision_block(signup))
    html.append(f"<h3>Why this outcome</h3><p class='why'>{e(explain(signup))}</p>")
    brief = signup.get("brief")
    if brief:
        extra = f" {e(brief['fallback'])}." if brief.get("fallback") else ""
        html.append(f"<h3>Sales brief</h3><p class='small'><strong>{e(brief.get('who'))}</strong>. {e(brief.get('freshest'))}.{extra}</p>")
    html.append(
        "<h3>Cost and time</h3><dl>"
        f"<dt>Crustdata credits</dt><dd>{credits(signup)}</dd><dt>Claude, agent run</dt><dd>{e(claude_cost(signup))}</dd>"
        f"<dt>Agent took</dt><dd>{f'{seconds} s' if seconds else 'no finished run'}</dd>"
        f"<dt>Whole signup</dt><dd>{total_seconds(signup) or 0} s</dd></dl>"
    )
    html.append(timeline(signup))
    if signup.get("email_html"):
        html.append(
            f"<h3>The email</h3><dl><dt>Subject</dt><dd>{e(signup.get('email_subject'))}</dd><dt>Status</dt>"
            f"<dd>{e(signup.get('email_status'))}: {e(signup.get('email_detail'))}</dd></dl>"
            f"<iframe sandbox title='Email for {e(signup.get('name'))}' srcdoc=\"{e(signup['email_html'])}\"></iframe>"
        )
    else:
        html.append("<h3>The email</h3><p class='muted'>No email was written for this signup.</p>")
    if signup.get("profile_report"):
        html.append(f"<details><summary>The agent's full report</summary><pre>{e(signup['profile_report'])}</pre></details>")
    html.append("</section>")
    return "".join(html)


def summary_table(signups: list[dict[str, Any]]) -> str:
    rows = []
    for index, signup in enumerate(signups, 1):
        decision = signup.get("decision") or {}
        label, tone = OUTCOMES.get(signup["status"], (signup["status"], ""))
        confidence = f"{decision['confidence']:.0%}" if decision.get("confidence") is not None else "–"
        source = "Agent report" if signup.get("profile_report") else "Basic"
        rows.append(
            f"<tr><td><a href='#s{index}'>{e(signup.get('name') or (signup.get('email_raw') or '').strip())}</a></td><td><span class='chip {tone}'>{e(label)}</span></td>"
            f"<td>{e(decision.get('segment') or '–')}</td><td class='num'>{confidence}</td><td>{e(picks_text(signup))}</td>"
            f"<td>{source}</td><td class='num'>{credits(signup)}</td><td>{'Yes' if signup.get('sales_alert') == 'sent' else 'No'}</td></tr>"
        )
    return (
        "<section class='card'><h2>Every signup at a glance</h2><div class='wrap' style='margin-top:12px'><table><tr><th>Who</th><th>Outcome</th>"
        "<th>Segment</th><th>Confidence</th><th>Services picked</th><th>Profile</th><th>Credits</th><th>Sales</th></tr>"
        f"{''.join(rows)}</table></div></section>"
    )


def build(signups: list[dict[str, Any]], source: Path) -> str:
    made = time.strftime("%d %b %Y, %H:%M")
    cards = "".join(signup_card(signup, index) for index, signup in enumerate(signups, 1))
    return (
        f"<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>Signup Pipeline Report</title><style>{STYLE}</style></head><body><main><h1>Signup pipeline report</h1>"
        f"<p class='muted'>{len(signups)} signups, from {e(source)}, written {made}. Holds real people's data, so keep it local.</p>"
        "<p class='small muted'>The evidence, decisions and emails are what the app stored at the time. Claude's raw reply isn't stored, so anything we dropped for failing a check won't appear here.</p>"
        f"{summary_table(signups)}{cards}</main></body></html>"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", type=Path, default=Path("cache/supabase.sqlite"))
    parser.add_argument("--out", type=Path, default=Path("cache/reports/pipeline_report.html"))
    args = parser.parse_args()
    signups = load(args.db)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(build(signups, args.db))
    print(f"{len(signups)} signups written to {args.out}")


if __name__ == "__main__":
    main()
