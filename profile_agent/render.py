"""Turns collected data into the writer's input, and finishes the written file.

finish() is the only place user.md is changed after the writer: it stamps the time the file was
generated, appends the sources table (each with the time its data was fetched, so cached data shows
its real age) and scrubs contact details.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

from profile_agent.collect import Collected

EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE = re.compile(r"(?<![\w/.-])\+?\d[\d ()-]{8,}\d(?![\w/-])")
# Images, timeseries, internal ids and per-job employer descriptions are what make raw output big.
DROP_KEYS = re.compile(
    r"(logo|picture|permalink|thumbnail|icon|avatar|timeseries|_urn$|^urn$|gravatar_id|node_id|_url$|^company_description$"
    r"|professional_network_id|position_id|^is_default$|similarity|^confident$|^continent$|^metadata$)", re.I,
)
KEEP_KEYS = {"profile_url", "html_url", "url", "website", "company_website", "homepage", "blog", "doi", "article_url"}
CONTACT_KEYS = re.compile(r"email|phone|^contact$|fax", re.I)
MAX_STRING, MAX_LIST = 400, 15


def iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def prune(value: Any) -> Any:
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if CONTACT_KEYS.search(k) or (DROP_KEYS.search(k) and k not in KEEP_KEYS):
                continue
            v = prune(v)
            if v not in (None, "", [], {}):
                out[k] = v
        return out
    if isinstance(value, list):
        items = [v for v in (prune(v) for v in value[:MAX_LIST]) if v not in (None, "", [], {})]
        if len(value) > MAX_LIST:
            items.append(f"… {len(value) - MAX_LIST} more")
        return items
    if isinstance(value, str) and len(value) > MAX_STRING:
        return value[:MAX_STRING] + "…"
    return value


def scrub(text: str, keep_email: str) -> str:
    keep = keep_email.strip().lower()
    text = EMAIL.sub(lambda m: m.group(0) if m.group(0).lower() == keep else "[email removed]", text)
    return PHONE.sub("[number removed]", text)


def sources_for_writer(c: Collected, findings: dict[str, Any] | None) -> str:
    """Every collected source as trimmed JSON, keyed so the writer can cite it."""
    blocks = []
    for call in c.client.calls:
        if call.error or call.output in (None, [], {}):
            status = call.error or "empty"
            blocks.append(f"### {call.tool} ({call.endpoint})\nRequest: {json.dumps(call.request)}\nResult: {status}")
            continue
        body = json.dumps(prune(call.output), ensure_ascii=False)
        blocks.append(f"### {call.tool} ({call.endpoint}), fetched {iso(call.fetched_at)}{' from cache' if call.cached else ''}\n"
                      f"Request: {json.dumps(call.request)}\n{body}")
    for s in c.sources:
        body = s.error or (s.output if isinstance(s.output, str) else json.dumps(prune(s.output), ensure_ascii=False))
        blocks.append(f"### {s.tool}, fetched {iso(s.fetched_at)}\nRequest: {s.request}\n{body}")
    if findings:
        blocks.append("### researcher (web search and fetch)\n" + json.dumps(findings, ensure_ascii=False))
    return "\n\n".join(blocks)


def sources_table(c: Collected, research_calls: list[dict[str, Any]], research_endpoints: dict[str, dict[str, float]]) -> str:
    lines = ["## Sources", "", "| Source | Request | Result | Credits | Fetched at (UTC) |", "|---|---|---|---|---|"]
    for call in c.client.calls:
        result = call.error or ("empty" if call.output in (None, [], {}) else "ok")
        when = iso(call.fetched_at) + (" (cache)" if call.cached else "")
        lines.append(f"| `{call.endpoint}` | {call.tool} | {result} | {0 if call.cached else call.credits} | {when} |")
    for s in c.sources:
        lines.append(f"| {s.tool} | {s.request[:80]} | {s.error or 'ok'} | 0 | {iso(s.fetched_at)} |")
    for path, stats in sorted(research_endpoints.items()):
        lines.append(f"| `{path}` | researcher, {int(stats['requests'])} request(s) | ok | {stats['credits']} | during research |")
    for call in research_calls:
        if call["tool"] in ("WebSearch", "WebFetch"):
            what = call.get("input") if isinstance(call.get("input"), str) else json.dumps(call.get("input"))[:80]
            lines.append(f"| {call['tool']} | {str(what)[:80]} | {'blocked' if not call.get('allowed', True) else 'ok'} | 0 | {iso(call['at'])} |")
    return "\n".join(lines)


def fill_blank(text: str, key: str, value: str) -> str:
    """Sets a frontmatter key only when the writer left it empty."""
    return re.sub(rf'(?m)^{key}: ""\s*(#.*)?$', f"{key}: {json.dumps(value)}", text, count=1)


def finish(markdown: str, c: Collected, table: str, generated_at: float) -> str:
    """Stamps generated_at in the frontmatter, fills X keys the writer left blank from data code
    already holds, appends the sources table and scrubs contact details."""
    text = markdown.strip()
    if "---" in text and not text.startswith("---"):
        text = text[text.index("---"):]  # drop any preamble before the frontmatter
    stamp = f'generated_at: "{iso(generated_at)}"'
    if text.startswith("---"):
        head, sep, rest = text[3:].partition("\n---")
        head = re.sub(r'(?m)^generated_at:.*$\n?', "", head)
        text = "---" + head.rstrip("\n") + "\n" + stamp + sep + rest
    else:
        text = f"---\n{stamp}\n---\n\n{text}"
    company_x = (c.x.get("company") or {}).get("url")
    if company_x:
        text = fill_blank(text, "company_x_url", company_x)
    confirmed = [h for h in c.x.get("person") or [] if h.get("confirmed")]
    if confirmed:
        text = fill_blank(text, "x_url", confirmed[0]["url"])
    return scrub(text + "\n\n" + table, c.email) + "\n"
