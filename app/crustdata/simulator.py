"""An in-process stand-in for the Crustdata API, shaped from docs.crustdata.com.

It lets the whole product run with no key and no credits. It really evaluates filters over a
synthetic population, so empty results, the relax ladder and credit charges all behave, and it
rejects what the docs say the API rejects (bad seniority values, `>=`, filter-only `fields`).

Everyone in it is invented. The dashboard shows a banner whenever this mode is on, and profile
links point at example.com.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import random
import re
from collections import Counter
from typing import Any

import httpx

SENIORITY = {
    "Entry Level", "Entry Level Manager", "Senior", "Director", "Owner / Partner",
    "CXO", "Vice President", "In Training", "Experienced Manager", "Strategic",
}
OPERATORS = {"=", "!=", "<", "=<", ">", "=>", "in", "not_in", "(.)", "[.]", "is_null", "is_not_null"}
FILTER_ONLY_FIELDS = (
    "experience.employment_details.current.company_name",
    "experience.employment_details.current.company_website_domain",
    "experience.employment_details.company_name",
)

CITIES = [
    ("Bengaluru", "India"), ("Hyderabad", "India"), ("Pune", "India"), ("Mumbai", "India"),
    ("Gurugram", "India"), ("San Francisco", "United States"), ("New York", "United States"),
    ("Austin", "United States"), ("London", "United Kingdom"), ("Berlin", "Germany"),
    ("Singapore", "Singapore"), ("Toronto", "Canada"),
]
ROLES = [
    ("Software Engineer", "Entry Level", 14), ("Senior Software Engineer", "Senior", 12),
    ("Backend Engineer", "Entry Level", 8), ("Senior Backend Engineer", "Senior", 2),
    ("Staff Engineer", "Senior", 3), ("Frontend Engineer", "Entry Level", 5),
    ("Data Scientist", "Senior", 5), ("Machine Learning Engineer", "Senior", 4),
    ("Product Manager", "Experienced Manager", 6), ("Product Designer", "Senior", 3),
    ("Recruiter", "Entry Level", 3), ("Talent Partner", "Senior", 2),
    ("Account Executive", "Senior", 5), ("Sales Development Representative", "Entry Level", 4),
    ("Head of Sales", "Director", 3), ("VP Sales", "Vice President", 3),
    ("VP Marketing", "Vice President", 2), ("Chief Revenue Officer", "CXO", 2),
    ("Head of Growth", "Director", 2), ("Founder", "Owner / Partner", 3),
    ("Co-Founder & CEO", "CXO", 3), ("Chief Technology Officer", "CXO", 2),
    ("Director of Engineering", "Director", 3), ("Research Scientist", "Senior", 2),
]
FIRST = "Aarav Maya Noah Ishaan Lena Tara Omar Zoe Rhea Felix Nina Kabir Sofia Dev Anya Leo Mira Arjun Eva Rohit".split()
LAST = "Quill Marlowe Fenwick Tandon Okafor Lindqvist Rao Vance Halden Iyer Moreau Castellan Nair Brandt Sethi Ansel".split()
COMPANY_A = "Bluepeak Cedar Lumen Quartz Harbor Copper Tidal Northstar Juniper Vector Saffron Ember Atlas Kestrel".split()
COMPANY_B = "Labs Systems Cloud Works Analytics Health Pay Logistics".split()
INDUSTRIES = [
    "Software Development", "Financial Services", "Technology, Information and Internet",
    "IT Services and IT Consulting", "Hospitals and Health Care", "Retail",
]

NAMED_COMPANIES: dict[str, dict[str, Any]] = {
    "hirefast.in": {
        "id": 5001, "name": "HireFast", "industries": ["Staffing and Recruiting"], "headcount": 42,
        "growth": {"six_months": 18.4, "yoy": 40.0}, "city": "Bengaluru", "country": "India",
        "description": "HireFast helps Indian startups hire engineers quickly.", "company_type": "Privately Held",
        "funding": {"total_investment_usd": 4200000, "last_round_type": "seed", "last_fundraise_date": "2026-03-11"},
        "openings": 5,
        "jobs": [
            ("Senior Backend Engineer", "Engineering", "Bengaluru, Karnataka, India", "greenhouse", "2026-10-28"),
            ("Recruiter", "Human Resources", "Bengaluru, Karnataka, India", "greenhouse", "2026-10-21"),
            ("Senior Recruiter", "Human Resources", "Bengaluru, Karnataka, India", "greenhouse", "2026-10-14"),
            ("Talent Partner", "Human Resources", "Remote, India", "greenhouse", "2026-10-09"),
            ("Product Designer", "Design", "Bengaluru, Karnataka, India", "lever", "2026-09-30"),
        ],
    },
    "northwindai.io": {
        "id": 5002, "name": "Northwind AI", "industries": ["Software Development"], "headcount": 28,
        "growth": {"six_months": 35.0, "yoy": 90.0}, "city": "San Francisco", "country": "United States",
        "description": "Northwind AI builds AI SDR agents for B2B SaaS revenue teams.", "company_type": "Privately Held",
        "funding": {"total_investment_usd": 11000000, "last_round_type": "series_a", "last_fundraise_date": "2026-06-02"},
        "openings": 2,
        "jobs": [
            ("Account Executive", "Sales", "San Francisco, California, United States", "ashby", "2026-10-25"),
            ("Founding GTM Engineer", "Sales", "Remote, United States", "ashby", "2026-10-02"),
        ],
    },
    "cedarbyte.com": {
        "id": 5003, "name": "Cedarbyte", "industries": ["Software Development"], "headcount": 310,
        "growth": {"six_months": 12.0, "yoy": 22.0}, "city": "Austin", "country": "United States",
        "description": "Cedarbyte sells revenue analytics software to mid-market SaaS companies.",
        "company_type": "Privately Held",
        "funding": {"total_investment_usd": 64000000, "last_round_type": "series_b", "last_fundraise_date": "2025-11-19"},
        "openings": 14,
        "jobs": [("Revenue Operations Manager", "Sales", "Austin, Texas, United States", "workday", "2026-10-30")],
    },
    "quietledger.io": {
        "id": 5004, "name": "Quiet Ledger", "industries": ["Financial Services"], "headcount": 6,
        "growth": {"six_months": 0.0, "yoy": 0.0}, "city": "London", "country": "United Kingdom",
        "description": "Ledger tooling.", "company_type": "Privately Held", "funding": {}, "openings": 0, "jobs": [],
    },
    "iitk.ac.in": {
        "id": 5005, "name": "Indian Institute of Technology, Kanpur", "industries": ["Higher Education"],
        "headcount": 2100, "growth": {}, "city": "Kanpur", "country": "India",
        "description": "Public research university.", "company_type": "Educational", "funding": {}, "openings": 0, "jobs": [],
    },
}

# email -> (person id, confidence, name, headline, title, department, city, country, employer)
IDENTITIES: dict[str, tuple] = {
    "priya@hirefast.in": (9001, 1.0, "Priya Sharma", "Head of Talent at HireFast", "Head of Talent", "Human Resources", "Bengaluru", "India", "HireFast"),
    "priya.sharma.dev@gmail.com": (9001, 1.0, "Priya Sharma", "Head of Talent at HireFast", "Head of Talent", "Human Resources", "Bengaluru", "India", "HireFast"),
    "alex@northwindai.io": (9002, 1.0, "Alex Mercer", "Founder & CEO at Northwind AI", "Founder & CEO", "Executive", "San Francisco", "United States", "Northwind AI"),
    "meera.ops@gmail.com": (9003, 0.95, "Meera Joshi", "VP Revenue Operations at Cedarbyte", "VP Revenue Operations", "Sales", "Austin", "United States", "Cedarbyte"),
    "dana@quietledger.io": (9004, 0.62, "Dana Okoye", "Builder", "Builder", "", "London", "United Kingdom", "Quiet Ledger"),
    "prof.rao@iitk.ac.in": (9005, 1.0, "S. Rao", "Professor of Computer Science, natural language processing", "Professor", "Computer Science", "Kanpur", "India", "IIT Kanpur"),
    "rohan.verma@iitk.ac.in": (9006, 1.0, "Rohan Verma", "B.Tech student at IIT Kanpur", "Student", "", "Kanpur", "India", "IIT Kanpur"),
    # The address and the profile disagree, which real people do all the time.
    "noor_khan@hirefast.in": (9010, 1.0, "Noor Ahmed", "Engineering Manager at HireFast", "Engineering Manager", "Engineering", "Bengaluru", "India", "HireFast"),
    # Two people who share a first name at one company, and two who share a full name.
    "sam.iyer@hirefast.in": (9011, 1.0, "Sam Iyer", "Backend Engineer at HireFast", "Backend Engineer", "Engineering", "Bengaluru", "India", "HireFast"),
    "sam.nair@hirefast.in": (9012, 1.0, "Sam Nair", "Sales Lead at HireFast", "Sales Lead", "Sales", "Bengaluru", "India", "HireFast"),
    "asha.one@gmail.com": (9013, 1.0, "Asha Menon", "Designer", "Designer", "", "Pune", "India", ""),
    "asha.two@gmail.com": (9014, 1.0, "Asha Menon", "Teacher", "Teacher", "", "Delhi", "India", ""),
    "private.person@gmail.com": (9008, 1.0, "Private Person", "Engineer", "Engineer", "", "Pune", "India", ""),
    "rohan.codes@gmail.com": (9007, 0.9, "Rohan Kulkarni", "Computer science student, learning APIs", "Student", "", "Pune", "India", ""),
}
REDACTED = {9008}


def _error(status: int, kind: str, message: str) -> httpx.Response:
    return httpx.Response(status, json={"error": {"type": kind, "message": message, "metadata": {}}})


def _path_values(obj: Any, path: list[str]) -> list[Any]:
    """Follows a dotted path, fanning out over lists."""
    if isinstance(obj, list):
        return [v for item in obj for v in _path_values(item, path)]
    if not path:
        return [obj] if obj is not None else []
    if isinstance(obj, dict) and path[0] in obj:
        return _path_values(obj[path[0]], path[1:])
    return []


def _matches(condition: dict[str, Any], row: dict[str, Any]) -> bool:
    if "conditions" in condition:
        results = [_matches(c, row) for c in condition["conditions"]]
        return any(results) if condition.get("op") == "or" else all(results)
    values = _path_values(row, condition["field"].split("."))
    kind, target = condition["type"], condition.get("value")
    if kind == "is_null":
        return not values
    if kind == "is_not_null":
        return bool(values)
    lowered = [v.lower() if isinstance(v, str) else v for v in values]
    if kind == "=":
        return (target.lower() if isinstance(target, str) else target) in lowered
    if kind == "!=":
        return (target.lower() if isinstance(target, str) else target) not in lowered
    if kind in ("in", "not_in"):
        wanted = {t.lower() if isinstance(t, str) else t for t in target}
        hit = bool(wanted & set(lowered))
        return hit if kind == "in" else not hit
    if kind == "(.)":
        words = str(target).lower().split()
        return any(all(w in str(v).lower() for w in words) for v in values)
    if kind == "[.]":
        return any(str(target).lower() in str(v).lower() for v in values)
    numbers = [v for v in values if isinstance(v, (int, float))]
    if kind == "=>":
        return any(v >= target for v in numbers)
    if kind == "=<":
        return any(v <= target for v in numbers)
    if kind == ">":
        return any(v > target for v in numbers)
    if kind == "<":
        return any(v < target for v in numbers)
    return False


def _walk_conditions(node: dict[str, Any]) -> list[dict[str, Any]]:
    if "conditions" in node:
        return [c for child in node["conditions"] for c in _walk_conditions(child)]
    return [node]


def _project(row: dict[str, Any], fields: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}

    def copy(source: Any, target: dict[str, Any], path: list[str]) -> None:
        if not isinstance(source, dict) or path[0] not in source:
            return
        head, rest = path[0], path[1:]
        if not rest:
            target[head] = source[head]
        elif isinstance(source[head], list):
            items = target.setdefault(head, [{} for _ in source[head]])
            for item, original in zip(items, source[head]):
                copy(original, item, rest)
        else:
            copy(source[head], target.setdefault(head, {}), rest)

    for field_path in fields:
        copy(row, out, field_path.split("."))
    return out


def _size_range(headcount: int) -> str:
    for low, high in ((1, 10), (11, 50), (51, 200), (201, 500), (501, 1000), (1001, 5000)):
        if headcount <= high:
            return f"{low}-{high}"
    return "5001+"


def _company_cost(filter_paths: list[str], fields: list[str]) -> float:
    """Measured on the real API: charged once to filter on a group and once to receive it."""
    premium = {"taxonomy": 0.1, "headcount": 0.2, "funding": 0.2, "hiring": 0.2}
    filtered = {p.split(".")[0] for p in filter_paths}
    received = {p.split(".")[0] for p in fields}
    return 0.03 + sum(premium.get(g, 0) for g in filtered) + sum(premium.get(g, 0) for g in received)


def _full_profile(person: dict[str, Any]) -> dict[str, Any]:
    """What enrich adds on top of a search row. Everything here is invented too."""
    experience = person["experience"]["employment_details"]
    return {
        **person,
        "experience": {"employment_details": {**experience, "past": [{"title": "Analyst", "name": "Bluepeak Labs"}]}},
        "education": {"schools": [{"degree": "BS", "field_of_study": "Computer Science", "school": "Invented Institute"}]},
        "skills": {"professional_network_skills": ["Python", "SQL"]},
        "updated_at": person["metadata"]["updated_at"],
    }


def _premium_groups(paths: list[str]) -> set[str]:
    return {p.split(".")[0] for p in paths} & {"experience", "education", "skills"}


class World:
    """The invented population. Built once from a fixed seed."""

    def __init__(self) -> None:
        rng = random.Random(27)
        self.companies: list[dict[str, Any]] = []
        for domain, c in NAMED_COMPANIES.items():
            self.companies.append(self._company(domain, c))
        next_id = 6000
        for first in COMPANY_A:
            for second in COMPANY_B:
                if rng.random() < 0.55:
                    city, country = rng.choice(CITIES)
                    next_id += 1
                    domain = f"{first}{second}.example".lower()
                    self.companies.append(self._company(domain, {
                        "id": next_id, "name": f"{first} {second}", "industries": [rng.choice(INDUSTRIES)],
                        "headcount": rng.choice([35, 60, 85, 120, 180, 250, 400, 650, 900, 1500]),
                        "growth": {"six_months": round(rng.uniform(-4, 42), 1)}, "city": city, "country": country,
                        "description": f"{first} {second} is an invented company.", "company_type": "Privately Held",
                        "funding": {}, "openings": rng.randint(0, 25), "jobs": [],
                    }))
        employers = [c for c in self.companies if c["basic_info"]["primary_domain"].endswith(".example")]
        self.people: list[dict[str, Any]] = []
        weights = [r[2] for r in ROLES]
        for index in range(1400):
            title, seniority, _ = rng.choices(ROLES, weights=weights)[0]
            city, country = rng.choices(CITIES, weights=[9, 4, 4, 4, 3, 5, 5, 3, 4, 3, 2, 2])[0]
            first, last = rng.choice(FIRST), rng.choice(LAST)
            employer_row = rng.choice(employers)
            employer = employer_row["basic_info"]
            slug = f"{first}-{last}-{index}".lower()
            year, month = rng.choice([2023, 2024, 2025, 2026]), rng.randint(1, 10)
            self.people.append({
                "crustdata_person_id": 100000 + index,
                "recently_changed_jobs": rng.random() < 0.18,
                "metadata": {"updated_at": f"2026-{rng.randint(8, 10):02d}-{rng.randint(1, 28):02d}T08:00:00Z"},
                "basic_profile": {
                    "name": f"{first} {last}", "headline": f"{title} at {employer['name']}", "current_title": title,
                    "location": {"city": city, "country": country, "full_location": f"{city}, {country}", "raw": f"{city}, {country}"},
                },
                "experience": {"employment_details": {"current": [{
                    "name": employer["name"], "title": title, "company_name": employer["name"],
                    "company_website_domain": employer["primary_domain"], "seniority_level": seniority,
                    "company_industries": employer["industries"], "company_headcount_latest": employer_row["headcount"]["total"],
                    "start_date": f"{year}-{month:02d}-01",
                }]}},
                "social_handles": {"professional_network_identifier": {"profile_url": f"https://example.com/profiles/{slug}"}},
            })

    @staticmethod
    def _company(domain: str, c: dict[str, Any]) -> dict[str, Any]:
        growth = c.get("growth", {})
        return {
            "crustdata_company_id": c["id"],
            "basic_info": {
                "crustdata_company_id": c["id"], "name": c["name"], "primary_domain": domain, "all_domains": [domain],
                "website": f"https://{domain}/", "description": c["description"], "company_type": c["company_type"],
                "employee_count_range": _size_range(c["headcount"]), "industries": c["industries"],
            },
            "headcount": {
                "total": c["headcount"],
                "largest_headcount_country": {"India": "IND", "United States": "USA"}.get(c["country"], c["country"]),
                "growth_percent": {**growth, "6m": growth.get("six_months")},
            },
            "funding": c["funding"],
            "hiring": {"openings_count": c["openings"], "recent_titles_csv": ",".join(j[0] for j in c["jobs"])},
            "taxonomy": {
                "professional_network_industry": c["industries"][0],
                "professional_network_industries": c["industries"],
                "categories": c["industries"],
            },
            "locations": {"country": c["country"], "city": c["city"], "headquarters": f"{c['city']}, {c['country']}"},
            "_jobs": c["jobs"],
        }


class SimulatorTransport(httpx.AsyncBaseTransport):
    def __init__(self, latency: bool = True) -> None:
        self.latency = latency
        self.world = World()
        self.world.people.extend(self._cast())
        self.watches = Watches()
        self.routes = {
            "/person/identify": lambda _: _error(
                403, "permission_error", "You do not have permission to access /person/identify."
            ),
            "/person/enrich": self._person_enrich,
            "/person/search": self._person_search,
            "/person/search/autocomplete": lambda b: self._autocomplete(b, self.world.people),
            "/company/identify": self._company_identify,
            "/company/enrich": self._company_enrich,
            "/company/search": self._company_search,
            "/company/search/autocomplete": lambda b: self._autocomplete(b, self.world.companies),
            "/job/search": self._job_search,
            "/web/search/live": self._web_search,
        }

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        handler = self.routes.get(path)
        if handler is None and not path.startswith("/watch/"):
            return _error(404, "not_found", path)
        if self.latency:
            await asyncio.sleep(random.uniform(0.12, 0.5))
        await request.aread()
        try:
            body = json.loads(request.content) if request.content else {}
            if handler is None:
                return self.watches.handle(request.method, path, body, self.world)
            return handler(body)
        except (KeyError, TypeError, ValueError) as exc:
            return _error(400, "invalid_request", f"Malformed request: {exc}")

    # ---- helpers ----

    @staticmethod
    def _ok(payload: Any, credits: float) -> httpx.Response:
        return httpx.Response(200, json=payload, headers={"x-credits-used": str(round(credits, 4))})

    @staticmethod
    def _validate_filters(filters: dict[str, Any] | None) -> httpx.Response | None:
        if not filters:
            return None
        for condition in _walk_conditions(filters):
            if condition.get("type") not in OPERATORS:
                return _error(400, "invalid_request", f"Unsupported operator {condition.get('type')}")
            if condition["field"].endswith("seniority_level"):
                values = condition["value"] if isinstance(condition["value"], list) else [condition["value"]]
                bad = [v for v in values if v not in SENIORITY]
                if bad:
                    return _error(400, "invalid_request", f"Invalid seniority_level: {bad}")
        return None

    # ---- person ----

    def _cast(self) -> list[dict[str, Any]]:
        """The named people, searchable like anyone else. One person can sit behind two emails."""
        people: dict[int, dict[str, Any]] = {}
        for email, identity in IDENTITIES.items():
            pid, title, employer = identity[0], identity[4], identity[8]
            if pid in people:
                continue
            domain = email.split("@")[1]
            if domain == "gmail.com":
                company = next((c for c in self.world.companies if c["basic_info"]["name"] == employer), None)
                domain = company["basic_info"]["primary_domain"] if company else ""
            row = self._person_data(email)
            row["metadata"] = {"updated_at": row.pop("updated_at")}
            row["recently_changed_jobs"] = False
            row["experience"] = {"employment_details": {"current": [{
                "name": employer, "title": title, "company_name": employer, "company_website_domain": domain,
                "seniority_level": "Director", "start_date": "2024-02-01",
            }] if employer else []}}
            people[pid] = row
        return list(people.values())

    def _person_data(self, email: str) -> dict[str, Any]:
        pid, _, name, headline, title, department, city, country, _ = IDENTITIES[email]
        slug = name.lower().replace(" ", "-").replace(".", "")
        return {
            "crustdata_person_id": pid,
            "basic_profile": {
                "name": name, "headline": headline, "current_title": title,
                "first_name": name.split()[0], "last_name": name.split()[-1],
                "normalized_title": {"department": department or None},
                "location": {"city": city, "country": country, "raw": f"{city}, {country}", "full_location": f"{city}, {country}"},
            },
            "social_handles": {"professional_network_identifier": {"profile_url": f"https://example.com/profiles/{slug}"}},
            "updated_at": "2026-10-20T09:00:00Z",
        }

    def _person_enrich(self, body: dict[str, Any]) -> httpx.Response:
        rows, credits = [], 0
        for url in body["professional_network_profile_urls"]:
            person = next(
                (p for p in self.world.people
                 if p["social_handles"]["professional_network_identifier"]["profile_url"] == url),
                None,
            )
            status = "not_found"
            if person is not None:
                status = "redacted" if person["crustdata_person_id"] in REDACTED else "matched"
            matched = status == "matched"
            credits += 1 if matched else 0
            rows.append({
                "matched_on": url, "match_type": "professional_network_profile_url", "match_status": status,
                "matches": [{"confidence_score": 1, "person_data": _full_profile(person)}] if matched else [],
            })
        return self._ok(rows, credits)

    def _person_search(self, body: dict[str, Any]) -> httpx.Response:
        filters, fields = body.get("filters"), body.get("fields", [])
        problem = self._validate_filters(filters)
        if problem:
            return problem
        bad = [f for f in fields if f in FILTER_ONLY_FIELDS or f == "updated_at"]
        if bad:
            return _error(400, "invalid_request", f"Invalid fields: {bad}")
        hits = [p for p in self.world.people if not filters or _matches(filters, p)]
        limit = int(body.get("limit", 20))
        page = hits[:limit]
        per_result = 0.03
        filter_paths = [c["field"] for c in _walk_conditions(filters)] if filters else []
        per_result += 0.1 * len(_premium_groups(filter_paths)) + 0.1 * len(_premium_groups(fields))
        return self._ok({
            "profiles": [_project(p, fields) | {"crustdata_person_id": p["crustdata_person_id"]} for p in page],
            "next_cursor": "sim-cursor" if len(hits) > limit else None,
            "total_count": len(hits), "total_count_relation": "eq", "remarks": [],
        }, per_result * len(page))

    # ---- company ----

    def _find_company(self, *, domain: str | None = None, name: str | None = None) -> dict[str, Any] | None:
        for company in self.world.companies:
            info = company["basic_info"]
            if domain and info["primary_domain"] == domain:
                return company
            if name and name.lower() in info["name"].lower():
                return company
        return None

    def _company_identify(self, body: dict[str, Any]) -> httpx.Response:
        rows = []
        for kind, key in (("domain", "domains"), ("name", "names")):
            for value in body.get(key, []):
                company = self._find_company(**{kind: value})
                rows.append({
                    "matched_on": value, "match_type": kind,
                    "matches": [{
                        "confidence_score": 1,
                        "company_data": {"crustdata_company_id": company["crustdata_company_id"], "basic_info": company["basic_info"]},
                    }] if company else [],
                })
        return self._ok(rows, 0)

    def _company_enrich(self, body: dict[str, Any]) -> httpx.Response:
        rows, credits = [], 0.0
        groups = body.get("fields", ["basic_info"])
        for domain in body["domains"]:
            company = self._find_company(domain=domain)
            data = None
            if company:
                credits += 2
                data = {"crustdata_company_id": company["crustdata_company_id"]}
                data.update({g: company[g] for g in groups if g in company})
                if "basic_info" in data:  # Real enrich omits this. Identify and search filters still know it.
                    data["basic_info"] = {k: v for k, v in data["basic_info"].items() if k != "industries"}
            rows.append({
                "matched_on": domain, "match_type": "domain",
                "matches": [{"confidence_score": 1, "company_data": data}] if data else [],
            })
        return self._ok(rows, credits)

    def _company_search(self, body: dict[str, Any]) -> httpx.Response:
        filters, fields = body.get("filters"), body.get("fields", [])
        problem = self._validate_filters(filters)
        if problem:
            return problem
        if "basic_info.industries" in fields:  # A filter alias on the real API, not a field.
            return _error(400, "invalid_request", "Invalid fields: basic_info.industries")
        hits = [c for c in self.world.companies if not filters or _matches(filters, c)]
        limit = int(body.get("limit", 20))
        page = hits[:limit]
        filter_paths = [c["field"] for c in _walk_conditions(filters)] if filters else []
        return self._ok({
            "companies": [_project(c, fields) | {"crustdata_company_id": c["crustdata_company_id"]} for c in page],
            "next_cursor": None, "total_count": len(hits),
        }, _company_cost(filter_paths, fields) * len(page))

    # ---- jobs, autocomplete, web ----

    def _job_search(self, body: dict[str, Any]) -> httpx.Response:
        condition = body["filters"]
        if condition["field"] != "company.basic_info.primary_domain":
            return _error(400, "invalid_request", f"Unsupported field {condition['field']}")
        company = self._find_company(domain=condition["value"])
        jobs = []
        if company:
            for title, category, location, source, added in company["_jobs"]:
                jobs.append({
                    "job_details": {"title": title, "category": category, "url": "https://example.com/jobs/1", "source": source},
                    "company": {"basic_info": {"name": company["basic_info"]["name"], "primary_domain": condition["value"]}},
                    "location": {"raw": location}, "metadata": {"date_added": f"{added}T10:00:00"},
                })
        limit = int(body.get("limit", 20))
        return self._ok({"job_listings": jobs[:limit], "next_cursor": None, "total_count": len(jobs)}, 0.03 * len(jobs[:limit]))

    @staticmethod
    def _autocomplete(body: dict[str, Any], rows: list[dict[str, Any]]) -> httpx.Response:
        if body["field"] == "basic_info.industries":  # A filter alias only; real autocomplete returns nothing.
            return httpx.Response(200, json={"suggestions": []}, headers={"x-credits-used": "0"})
        query = body["query"].lower().strip()
        aliases = {"bangalore": "bengaluru", "usa": "united states", "uk": "united kingdom"}
        query = aliases.get(query, query)
        counts: Counter[str] = Counter()
        for row in rows:
            for value in _path_values(row, body["field"].split(".")):
                if isinstance(value, str) and all(word in value.lower() for word in query.split()):
                    counts[value] += 1
        ordered = sorted(counts, key=lambda v: (v.lower() != query, -counts[v]))
        suggestions = [{"value": v, "document_count": counts[v]} for v in ordered[: int(body.get("limit", 20))]]
        return httpx.Response(200, json={"suggestions": suggestions}, headers={"x-credits-used": "0"})

    @staticmethod
    def _web_search(body: dict[str, Any]) -> httpx.Response:
        query, source = body["query"], body["sources"][0]
        rows = []
        for index in range(1, 11):
            if source == "scholar-author":
                rows.append({
                    "source": source, "url": f"https://example.com/scholar/author-{index}",
                    "name": f"{FIRST[index]} {LAST[index]}", "affiliation": "Invented University",
                    "interests": [{"title": query, "link": "https://example.com"}],
                    "citations": {"all": 4000 - index * 300, "since_2020": 1800 - index * 100},
                })
            else:
                rows.append({
                    "source": source, "title": f"Advances in {query}: study {index}",
                    "url": f"https://example.com/papers/{index}", "snippet": f"An invented paper about {query}.",
                    "metadata": f"{FIRST[index]} {LAST[index]} - 2025 - example.com", "position": index,
                    "authors": [{"name": f"{FIRST[index]} {LAST[index]}", "profile_url": "https://example.com/scholar"}],
                    "citations": 300 - index * 20,
                })
        return httpx.Response(200, json={
            "success": True, "query": query, "results": rows,
            "metadata": {"total_results": len(rows), "failed_pages": [], "empty_pages": []},
        }, headers={"x-credits-used": "1"})


# ---- Watches ----

WATCH_PATH = re.compile(r"^/watch/(person|company)(?:/(\d+)(?:/runs(?:/(\d+)/summary)?)?)?$")
PRICE_PER_NOTIFICATION = 5.0  # The documented rate at the default 30-day freshness.

# What `simulate` can make happen, for the dashboard's buttons and the tests.
ACTIVITY = {
    "company": ("funding", "news", "investor"),
    "person": ("role", "headline"),
}


def _today() -> str:
    return datetime.date.today().isoformat()


class Watches:
    """Entity watches, shaped like /watch/{dataset}. A run only happens when `simulate` makes one.

    Like the real thing, creating a watch starts a free baseline run whose sample has empty
    `changes`, and later runs report each change with the subject's current record.
    """

    def __init__(self) -> None:
        self.items: dict[int, dict[str, Any]] = {}
        self.next_id = 70000
        self.next_run = 900000

    def handle(self, method: str, path: str, body: dict[str, Any], world: "World") -> httpx.Response:
        found = WATCH_PATH.match(path)
        if not found:
            return _error(404, "not_found", path)
        dataset, watch_id, run_id = found.group(1), found.group(2), found.group(3)
        if watch_id is None:
            return self._create(dataset, body, world) if method == "POST" else _error(405, "invalid_request", method)
        watch = self.items.get(int(watch_id))
        if watch is None or watch["dataset"] != dataset:
            return _error(404, "not_found", "Watch not found")
        if method == "DELETE":
            watch["status"] = "cancelled"
            return httpx.Response(204)
        if run_id is not None:
            run = next((r for r in watch["runs"] if r["id"] == int(run_id)), None)
            return httpx.Response(200, json=run) if run else _error(404, "not_found", "Run not found")
        if path.endswith("/runs"):
            runs = [{k: v for k, v in r.items() if k != "notifications"} for r in reversed(watch["runs"])]
            return httpx.Response(200, json={"runs": runs, "next_cursor": None, "total_count": len(runs)})
        return httpx.Response(200, json={k: v for k, v in watch.items() if k not in ("runs", "subject")})

    def _create(self, dataset: str, body: dict[str, Any], world: "World") -> httpx.Response:
        for key in ("entities", "track", "config", "notifications"):
            if key not in body:
                return _error(400, "invalid_request", f"{key} is required")
        subject = self._resolve(dataset, body["entities"], world)
        self.next_id += 1
        watch = {
            "id": self.next_id, "kind": "entity", "dataset": dataset, "status": "active",
            "entities": body["entities"], "track": body["track"], "fields": body.get("fields"),
            "config": body["config"], "notifications": body["notifications"], "created_at": _today(),
            "subject": subject, "runs": [],
        }
        self.items[watch["id"]] = watch
        sample = [{"uid": f"{dataset}_baseline_{watch['id']}", "changes": [], "record": subject}] if subject else []
        self._run(watch, sample, credits=0.0)  # The baseline is free and fires on nothing.
        return httpx.Response(200, json={k: v for k, v in watch.items() if k not in ("runs", "subject")})

    @staticmethod
    def _resolve(dataset: str, entities: dict[str, Any], world: "World") -> dict[str, Any] | None:
        if dataset == "person":
            urls = entities.get("professional_network_profile_urls", [])
            person = next(
                (p for p in world.people
                 if p["social_handles"]["professional_network_identifier"]["profile_url"] in urls),
                None,
            )
            return json.loads(json.dumps(person)) if person else None
        ids = set(entities.get("crustdata_company_ids", []))
        domains = set(entities.get("domains", []))
        company = next(
            (c for c in world.companies
             if c["crustdata_company_id"] in ids or c["basic_info"]["primary_domain"] in domains),
            None,
        )
        if company is None:
            return None
        record = {k: v for k, v in company.items() if not k.startswith("_")}
        record.setdefault("news", [])
        return json.loads(json.dumps(record))

    def _run(self, watch: dict[str, Any], notifications: list[dict[str, Any]], credits: float) -> dict[str, Any]:
        self.next_run += 1
        run = {
            "id": self.next_run, "started_at": _today(), "completed_at": _today(), "status": "SUCCESS",
            "records_searched": 1, "records_matched": 1 if watch["subject"] else 0,
            "records_after_filter": len(notifications), "new_records_count": len(notifications),
            "credits_deducted": credits, "notification_http_status": None,
            "notifications": [{
                "sent_at": _today(), "http_status": None,
                "payload": {
                    "subscription_id": watch["id"], "event_type": f"indb:entity:{watch['dataset']}",
                    "timestamp": _today(), "notifications": notifications,
                },
            }] if notifications else [],
        }
        watch["runs"].append(run)
        return run

    def simulate(self, watch_id: int, kind: str) -> dict[str, Any]:
        """Makes one change happen to the watched subject, and the run that reports it."""
        watch = self.items[watch_id]
        record = watch["subject"]
        if watch["status"] != "active" or record is None:
            raise ValueError("that watch isn't active")
        changes = self._change(watch["dataset"], kind, record)
        run_id = self.next_run + 1
        uid = f"{watch['dataset']}_{watch_id}_{run_id}"
        return self._run(watch, [{"uid": uid, "changes": changes, "record": record}], credits=PRICE_PER_NOTIFICATION)

    @staticmethod
    def _change(dataset: str, kind: str, record: dict[str, Any]) -> list[dict[str, Any]]:
        if dataset == "company":
            name = record["basic_info"]["name"]
            funding = record.setdefault("funding", {})
            if kind == "funding":
                rounds = ["pre_seed", "seed", "series_a", "series_b", "series_c", "series_d"]
                before = funding.get("last_round_type") or ""
                after = rounds[min(rounds.index(before) + 1, len(rounds) - 1)] if before in rounds else "seed"
                total_before = funding.get("total_investment_usd")
                total_after = (total_before or 0) + 30000000
                funding.update(last_round_type=after, total_investment_usd=total_after, last_fundraise_date=_today())
                return [
                    {"field": "funding.last_round_type", "type": "changed", "from": before or None, "to": after},
                    {"field": "funding.total_investment_usd", "type": "changed", "from": total_before, "to": total_after},
                ]
            if kind == "investor":
                investor = "Example Ventures"
                funding.setdefault("investors", []).append(investor)
                return [{"field": "funding.investors", "type": "added", "new_elements": [investor]}]
            article = {
                "title": f"{name} launches an agent platform for revenue teams",
                "article_url": f"https://example.com/news/{record['crustdata_company_id']}-{len(record.get('news', []))}",
                "published_at": _today(),
            }
            record.setdefault("news", []).append(article)
            return [{"field": "news", "type": "added", "new_elements": [article]}]
        profile = record["basic_profile"]
        if kind == "headline":
            before = profile.get("headline", "")
            profile["headline"] = f"{before} | Hiring engineers".strip(" |")
            return [{"field": "basic_profile.headline", "type": "changed", "from": before, "to": profile["headline"]}]
        role = {
            "name": "Lumen Labs", "title": "Co-Founder & CEO", "company_name": "Lumen Labs",
            "company_website_domain": "lumenlabs.example", "start_date": _today()[:7] + "-01",
        }
        record["experience"]["employment_details"]["current"].insert(0, role)
        return [{"field": "experience.employment_details.current", "type": "added", "new_elements": [role]}]
