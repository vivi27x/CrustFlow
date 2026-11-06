#!/usr/bin/env python
"""Checks our assumptions against the real Crustdata API.

The simulator encodes our reading of the docs, so run this as soon as a key exists. It prints the
"Verified on our key" table for docs/crustdata-api.md. It spends about 4 credits, or about 5 with
--email, so it refuses to start without --yes.

    uv run python scripts/probe_api.py --yes --email teammate@company.com
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings  # noqa: E402
from app.playbooks.base import COMPANY_FIELDS, COUNTRY, INDUSTRY, PERSON_FIELDS, TITLE, cond, person_body  # noqa: E402

ENRICH_FIELDS = ["basic_info", "headcount", "funding", "hiring", "taxonomy"]
rows: list[tuple[str, str]] = []


def record(check: str, result: Any) -> None:
    rows.append((check, str(result)))
    print(f"  {check}: {result}", flush=True)


def path_present(obj: Any, path: str) -> bool:
    node = obj
    for key in path.split("."):
        if isinstance(node, list):
            node = node[0] if node else None
        if not isinstance(node, dict) or key not in node:
            return False
        node = node[key]
    return True


async def call(http: httpx.AsyncClient, method: str, path: str, body: dict | None = None) -> tuple[int, Any, int, str | None, dict]:
    started = time.perf_counter()
    response = await http.request(method, path, json=body)
    latency = int((time.perf_counter() - started) * 1000)
    try:
        data = response.json()
    except ValueError:
        data = response.text[:200]
    return response.status_code, data, latency, response.headers.get("x-credits-used"), dict(response.headers)


def describe(status: int, data: Any, latency: int, credits: str | None) -> str:
    if status != 200:
        error = data.get("error", data) if isinstance(data, dict) else data
        return f"{status} after {latency} ms: {str(error)[:160]}"
    return f"200 in {latency} ms, {credits} credits"


async def balance(http: httpx.AsyncClient) -> Any:
    status, data, *_ = await call(http, "GET", "/account/credits")
    return data.get("account", {}).get("credits") if status == 200 else f"unavailable ({status})"


async def main(args: argparse.Namespace) -> None:
    settings = get_settings()
    if not settings.crustdata_api_key:
        sys.exit("CRUSTDATA_API_KEY is not set. Add it to .env first.")
    async with httpx.AsyncClient(
        base_url=settings.crustdata_base_url,
        timeout=20,
        headers={
            "Authorization": f"Bearer {settings.crustdata_api_key}",
            "x-api-version": settings.crustdata_api_version,
            "content-type": "application/json",
        },
    ) as http:
        print("Account")
        record("Credit balance before", await balance(http))
        status, data, *_ = await call(http, "GET", "/account/endpoints")
        record("/account/endpoints", f"{status}, {len(str(data))} bytes")
        if status == 200:
            endpoints = {e["path"]: e for e in data.get("endpoints", [])}
            for path in ("/person/identify", "/person/enrich", "/person/search", "/company/search", "/person/search/autocomplete"):
                entry = endpoints.get(path, {})
                record(f"  {path}", f"{entry.get('status', 'not listed')}, {entry.get('effective_rate_limit_rpm')} rpm")

        print("Identify")
        record("/company/identify", describe(*(await call(http, "POST", "/company/identify", {"domains": ["stripe.com"]}))[:4]))
        if args.email:
            status, data, latency, credits, _ = await call(http, "POST", "/person/identify", {"emails": [args.email]})
            record("/person/identify enabled?", describe(status, data, latency, credits))
            if status == 200 and isinstance(data, list) and data:
                record("  match_status", data[0].get("match_status"))
                record("  has employer in response?", "experience" in str(data[0]))
        else:
            record("/person/identify enabled?", "skipped: pass --email to test it")

        print("Enrich and jobs")
        status, data, latency, credits, _ = await call(http, "POST", "/company/enrich", {"domains": ["stripe.com"], "fields": ENRICH_FIELDS})
        record("/company/enrich", describe(status, data, latency, credits))
        if status == 200 and data and data[0].get("matches"):
            company = data[0]["matches"][0]["company_data"]
            for path in ("headcount.total", "headcount.largest_headcount_country", "headcount.growth_percent.six_months",
                         "hiring.openings_count", "taxonomy.categories", "taxonomy.professional_network_industries"):
                record(f"  {path}", path_present(company, path))
        job_body = {
            "filters": cond("company.basic_info.primary_domain", "=", "stripe.com"),
            "sorts": [{"field": "metadata.date_added", "order": "desc"}],
            "fields": ["job_details.title", "job_details.source", "location.raw", "metadata.date_added"],
            "limit": 1,
        }
        record("/job/search", describe(*(await call(http, "POST", "/job/search", job_body))[:4]))

        print("Autocomplete")
        probes = [
            ("/person/search/autocomplete", TITLE, "Senior Backend Engineer"),
            ("/person/search/autocomplete", "basic_profile.location.city", "Bangalore"),
            ("/person/search/autocomplete", COUNTRY, "India"),
            ("/company/search/autocomplete", INDUSTRY, "Software"),
            ("/company/search/autocomplete", "locations.country", "India"),
        ]
        for path, field, query in probes:
            status, data, latency, credits, _ = await call(http, "POST", path, {"field": field, "query": query, "limit": 3})
            values = [s.get("value") for s in data.get("suggestions", [])] if status == 200 else data
            record(f'{field} "{query}" snaps to', f"{values} ({latency} ms)")

        print("Person search")
        body = person_body([cond(TITLE, "(.)", "Software Engineer"), cond(COUNTRY, "=", "India")], limit=1)
        before = await balance(http)
        status, data, latency, credits, headers = await call(http, "POST", "/person/search", body)
        after = await balance(http)
        record("/person/search with our fields", describe(status, data, latency, credits))
        record("Fractional rounding: balance before and after", f"{before} then {after} (X-Credits-Used {credits})")
        if status == 200 and data.get("profiles"):
            profile = data["profiles"][0]
            for path in PERSON_FIELDS:
                record(f"  field {path}", path_present(profile, path))
            record("  shape of current", type(profile.get("experience", {}).get("employment_details", {}).get("current")).__name__)
        record("Rate-limit headers", {k: v for k, v in headers.items() if k.lower().startswith("x-ratelimit")})

        recent = person_body([cond("recently_changed_jobs", "=", True), cond(COUNTRY, "=", "India")], limit=1)
        record("recently_changed_jobs filter", describe(*(await call(http, "POST", "/person/search", recent))[:4]))

        print("Company search")
        company_body = {
            "filters": {"op": "and", "conditions": [
                cond("headcount.total", "=>", 50), cond("headcount.total", "=<", 1000),
                cond("headcount.growth_percent.6m", "=>", 10),
            ]},
            "fields": COMPANY_FIELDS,
            "limit": 1,
        }
        status, data, latency, credits, _ = await call(http, "POST", "/company/search", company_body)
        record("/company/search with growth filter", describe(status, data, latency, credits))

        record("Credit balance after", await balance(http))

    print("\n| Check | Result |\n|---|---|")
    for check, result in rows:
        print(f"| {check.strip()} | {result.replace('|', '/')} |")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--email", help="A real work email, to test /person/identify (1 credit if matched)")
    parser.add_argument("--yes", action="store_true", help="Confirm you're fine spending about 4 credits")
    options = parser.parse_args()
    if not options.yes:
        sys.exit("This spends about 4 real credits. Rerun with --yes.")
    asyncio.run(main(options))
