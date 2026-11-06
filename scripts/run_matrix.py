#!/usr/bin/env python
"""Runs the test matrix against a running server and checks each outcome.

    uv run python scripts/run_matrix.py --reset
    uv run python scripts/run_matrix.py --base https://your-host --matrix scripts/matrix.local.json

With no --matrix it uses the simulator's cast. A real matrix is a JSON list of
{"email": ..., "name": ..., "expect": [...], "playbook": ...}. Keep real addresses in a .local.json file.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import httpx

BUSY = {"received", "gating", "identifying", "researching", "classifying", "querying", "delivering"}
SIM_MATRIX = [
    {"label": "Work email, high fit", "email": "priya@hirefast.in", "name": "Priya Sharma", "expect": ["delivered"], "playbook": "recruiting", "brief": True, "pill": True},
    {"label": "Same person, second email", "email": "priya.sharma.dev@gmail.com", "name": "Priya Sharma", "expect": ["human_queue"]},
    {"label": "Student Gmail with a name", "email": "rohan.codes@gmail.com", "name": "Rohan Kulkarni", "expect": ["nurture"], "playbook": "student", "max_credits": 1},
    {"label": "Faculty at iitk.ac.in", "email": "prof.rao@iitk.ac.in", "expect": ["delivered"], "playbook": "academic"},
    {"label": "crustdata.com", "email": "jane@crustdata.com", "expect": ["internal"], "max_credits": 0},
    {"label": "Disposable domain", "email": "throwaway@mailinator.com", "expect": ["human_queue"], "max_credits": 0},
    {"label": "Malformed email", "email": "not-an-email", "expect": ["rejected"], "max_credits": 0},
    {"label": "Brand-new domain", "email": "founder@brand-new-startup.xyz", "expect": ["awaiting_intent"], "max_credits": 1},
    # Beyond the spec's eight.
    {"label": "Founder, gtm", "email": "alex@northwindai.io", "name": "Alex Mercer", "expect": ["delivered"], "playbook": "gtm", "brief": True},
    {"label": "Gmail with a name, finds employer", "email": "meera.ops@gmail.com", "name": "Meera Joshi", "expect": ["delivered"], "playbook": "gtm"},
    {"label": "Address differs from profile name", "email": "noor_khan@hirefast.in", "name": "Noor Khan", "expect": ["delivered", "awaiting_intent"]},
    {"label": "Two people fit, so no guess", "email": "someone@gmail.com", "name": "Asha Menon", "expect": ["awaiting_intent"], "max_credits": 1},
    {"label": "Gmail with no name", "email": "nameless.signup@gmail.com", "expect": ["awaiting_intent"], "max_credits": 0},
    {"label": "Student on .ac.in", "email": "rohan.verma@iitk.ac.in", "expect": ["nurture"], "max_credits": 1},
    {"label": "Unmatched .ac.in", "email": "nobody.here@iitk.ac.in", "expect": ["awaiting_intent"]},
    {"label": "Thin evidence", "email": "dana@quietledger.io", "expect": ["awaiting_intent"]},
    {"label": "Data removal on file", "email": "private.person@gmail.com", "name": "Private Person", "expect": ["withdrawn"]},
    {"label": "Competitor", "email": "sam@zoominfo.com", "expect": ["human_queue"], "max_credits": 0},
    {"label": "Messy: alias of a known signup", "email": "  Priya+promo@HireFast.IN ", "expect": ["human_queue"], "max_credits": 0},
    {"label": "Messy: name and brackets", "email": "A Person <a.person@brand-new-startup.xyz>", "expect": ["awaiting_intent"]},
    {"label": "Messy: empty", "email": "", "expect": ["rejected"], "max_credits": 0},
]


def run_case(http: httpx.Client, case: dict) -> list:
    response = http.post("/api/signups", json={"email": case["email"], "name": case.get("name", "")})
    response.raise_for_status()
    signup_id = response.json()["ids"][0]
    started = time.monotonic()
    while True:
        detail = http.get(f"/api/signups/{signup_id}").json()
        if detail["status"] not in BUSY or time.monotonic() - started > 70:
            break
        time.sleep(0.15)
    seconds = time.monotonic() - started

    problems = []
    if detail["status"] not in case["expect"]:
        problems.append(f"status {detail['status']}, wanted {case['expect']}")
    if case.get("playbook") and detail.get("playbook") != case["playbook"]:
        problems.append(f"playbook {detail.get('playbook')}, wanted {case['playbook']}")
    max_credits = case.get("max_credits", 8)
    if detail["credits"] > max_credits:
        problems.append(f"{detail['credits']} credits, cap {max_credits}")
    if "delivered" in case["expect"]:
        if seconds > 60:
            problems.append(f"took {seconds:.0f} s, limit 60")
        rows = detail.get("result_rows") or []
        if not rows:
            problems.append("no results")
        for reason in (detail.get("decision") or {}).get("reasons", []):
            if not reason.get("citations"):
                problems.append("a reason has no citation")
    if case.get("brief") and not detail.get("brief"):
        problems.append("no sales brief")
    if detail["status"] in ("nurture", "human_queue", "internal") and detail.get("brief"):
        problems.append("a sales brief went out for a suppressed signup")
    if case.get("pill") and not problems:
        pill = detail["pills"][0]["id"]
        started_pill = time.monotonic()
        result = http.post(f"/sandbox/{detail['sandbox_token']}/pills/{pill}").json()
        pill_seconds = time.monotonic() - started_pill
        after = http.get(f"/api/signups/{signup_id}").json()["status"]
        if not result.get("ok") or after != "activated":
            problems.append(f"pill failed: {result.get('message')}, status {after}")
        if pill_seconds > 3:
            problems.append(f"pill took {pill_seconds:.1f} s, limit 3")
    return [case.get("label", case["email"]), detail["status"], detail.get("playbook") or "", f"{seconds:.1f}", detail["credits"], problems]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base", default="http://localhost:8000")
    parser.add_argument("--matrix", help="JSON file. Defaults to the simulator's cast.")
    parser.add_argument("--reset", action="store_true", help="Clear signups first, or repeats will trip the gates")
    args = parser.parse_args()

    matrix = json.loads(Path(args.matrix).read_text()) if args.matrix else SIM_MATRIX
    password = os.environ.get("DEMO_PASSWORD")
    auth = ("demo", password) if password else None
    with httpx.Client(base_url=args.base, timeout=75, auth=auth) as http:
        if args.reset:
            http.post("/api/reset").raise_for_status()
        results = [run_case(http, case) for case in matrix]
        kpis = http.get("/api/kpis").json()["kpis"]

    width = max(len(r[0]) for r in results)
    print(f"{'Case'.ljust(width)}  {'Status'.ljust(15)} {'Playbook'.ljust(10)} {'Secs':>5} {'Credits':>7}  Result")
    for label, status, playbook, seconds, credits, problems in results:
        verdict = "PASS" if not problems else "FAIL: " + "; ".join(problems)
        print(f"{label.ljust(width)}  {status.ljust(15)} {playbook.ljust(10)} {seconds:>5} {credits:>7}  {verdict}")
    failed = sum(1 for r in results if r[5])
    print(f"\n{len(results) - failed} of {len(results)} passed. KPIs: {json.dumps(kpis)}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
