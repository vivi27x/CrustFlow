"""Code-run collection: every Crustdata call, plus free lookups, in two parallel waves.

Background  Crustdata web search and news search on the name plus the email's domain. They are the
            slowest calls (10 to 30 s), so they start first and run alongside both waves.
Wave 1      email lookup, company_identify (email domain and its parent), and
            person_search: name plus the email's domain as current employer. If that finds nobody,
            person_search on name plus the email-domain organisation's name, as current employer
            and (for academic emails) as current school.
Wave 2      person_enrich, company_enrich, job_search, posts by the person and the employer, and the
            GitHub profile tied to the email. Then the homepages found by the web search.

Each call is one HTTP request made by code, so no model time goes on planning them. A person is
matched only when exactly one candidate has the name and the email's domain (as current employer,
or as current school); otherwise the match is left to the researcher agent.
"""

from __future__ import annotations

import html
import json
import re
import time
import unicodedata
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from profile_agent.crustdata import Call, Client, cached_call, exact_company_for_domain, exact_match, parallel
from profile_agent.lookups import USER_AGENT, domain_type, email_lookup

PERSON_FIELDS = [
    "basic_profile", "experience", "education", "skills", "honors", "professional_network",
    "social_handles", "dev_platform_profiles", "research_profiles",
]
SEARCH_FIELDS = [
    "crustdata_person_id", "basic_profile.name", "basic_profile.headline", "basic_profile.current_title",
    "social_handles.professional_network_identifier.profile_url", "experience.employment_details.current",
]
SCHOOL_FIELDS = SEARCH_FIELDS + ["education.schools.school", "education.schools.end_year"]
COMPANY_FIELDS = ["basic_info", "headcount", "funding", "hiring", "taxonomy", "news", "social_profiles"]
JOB_FIELDS = ["job_details.title", "job_details.category", "location.raw", "metadata.date_added", "job_details.url"]
SECOND_LEVEL = {"ac", "edu", "co", "com", "org", "gov", "net", "or", "ne"}
PAGE_CHARS = 5000


@dataclass
class Source:
    """A non-Crustdata source (email lookup, GitHub, a homepage), shown like a Crustdata call."""
    tool: str
    request: str
    output: Any = None
    error: str | None = None
    fetched_at: float = field(default_factory=time.time)


@dataclass
class Collected:
    email: str
    name: str
    domain: str
    client: Client
    sources: list[Source] = field(default_factory=list)
    person: dict[str, Any] | None = None        # the matched search row: id, name, profile URL, current roles
    company_id: int | None = None
    company_name: str = ""
    match: str = "none"                         # identify | domain | school | researcher | none | ambiguous
    match_note: str = ""
    seconds: float = 0.0
    x: dict[str, Any] = field(default_factory=dict)  # X handles: {"person": [...], "company": {...}}
    background: Any = None                      # executor running the two web searches
    web: Any = None
    news: Any = None

    @property
    def credits(self) -> float:
        return round(sum(c.credits for c in self.client.calls if not c.cached), 4)


# ---- Matching ----

def tokens(name: str) -> set[str]:
    text = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode().lower()
    return {t for t in re.split(r"[^a-z]+", text) if len(t) > 1}


def names_match(given: str, candidate: str) -> bool:
    """Same tokens in any order, allowing extra ones: 'Ada Example' matches 'Ada (Grace) Example'."""
    a, b = tokens(given), tokens(candidate)
    return bool(a and b) and (a <= b or b <= a)


def registrable(domain: str) -> str:
    """cs.ucla.edu -> ucla.edu, air.tsinghua.edu.cn -> tsinghua.edu.cn."""
    parts = domain.lower().split(".")
    if len(parts) >= 3 and len(parts[-1]) == 2 and parts[-2] in SECOND_LEVEL:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def same_site(a: str, b: str) -> bool:
    a, b = (a or "").lower().removeprefix("www."), (b or "").lower().removeprefix("www.")
    return bool(a and b) and (a == b or a.endswith("." + b) or b.endswith("." + a) or registrable(a) == registrable(b))


def current_roles(row: dict[str, Any]) -> list[dict[str, Any]]:
    current = ((row.get("experience") or {}).get("employment_details") or {}).get("current") or []
    return current if isinstance(current, list) else [current]


def profiles(call: Call) -> list[dict[str, Any]]:
    out = call.output if isinstance(call.output, dict) else {}
    return [p for p in out.get("profiles") or [] if isinstance(p, dict)]


def person_name(row: dict[str, Any]) -> str:
    return ((row.get("basic_profile") or {}).get("name")) or ""


def profile_url(row: dict[str, Any]) -> str:
    return ((((row.get("social_handles") or {}).get("professional_network_identifier")) or {}).get("profile_url")) or ""


def by_domain(rows: list[dict[str, Any]], name: str, domain: str) -> list[dict[str, Any]]:
    keep = []
    for row in rows:
        if not names_match(name, person_name(row)):
            continue
        if any(same_site(role.get("company_website_domain") or "", domain) for role in current_roles(row)):
            keep.append(row)
    return keep


def by_employer_name(rows: list[dict[str, Any]], name: str, company: str) -> list[dict[str, Any]]:
    want = tokens(company)
    return [row for row in rows if names_match(name, person_name(row))
            and any(want and want <= tokens(role.get("name") or role.get("company_name") or "") for role in current_roles(row))]


def by_school(rows: list[dict[str, Any]], name: str, school: str) -> list[dict[str, Any]]:
    year = time.gmtime().tm_year
    keep = []
    for row in rows:
        if not names_match(name, person_name(row)):
            continue
        for s in (row.get("education") or {}).get("schools") or []:
            end = s.get("end_year") or 0
            if tokens(school) and tokens(school) <= tokens(s.get("school") or "") and (not end or end >= year):
                keep.append(row)
                break
    return keep


# ---- Free sources ----

def _get(url: str, headers: dict[str, str] | None = None, timeout: float = 10) -> Any:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read()
        kind = response.headers.get("content-type", "")
    return json.loads(raw) if "json" in kind else raw.decode("utf-8", "replace")


X_LINK = re.compile(r"(?:twitter|x)\.com/(?!intent|share|home|search|i/|hashtag|status|privacy|tos)([A-Za-z0-9_]{1,15})\b", re.I)


def x_handles_in(text: str) -> list[str]:
    return sorted({m.group(1) for m in X_LINK.finditer(text or "")})


def page_text(url: str) -> str:
    """The page's visible text, plus any X links it carries (they'd be lost with the tags)."""
    raw = _get(url)
    text = raw if isinstance(raw, str) else json.dumps(raw)
    links = x_handles_in(text)
    text = re.sub(r"(?is)<(script|style|noscript|svg).*?</\1>", " ", text)
    text = html.unescape(re.sub(r"(?s)<[^>]+>", " ", text))
    text = re.sub(r"\s+", " ", text).strip()[:PAGE_CHARS]
    return text + (f"\nX links on this page: {', '.join('https://x.com/' + h for h in links)}" if links else "")


def source(tool: str, request: str, fn: Any) -> Source:
    s = Source(tool=tool, request=request)
    try:
        s.output = fn()
    except Exception as exc:  # noqa: BLE001 - a failed lookup is recorded, never fatal.
        s.error = f"{type(exc).__name__}: {exc}"[:200]
    return s


def homepage_candidates(web: Call, domain: str, name: str, limit: int = 2) -> list[str]:
    """Web results on the email's own site whose title or URL carries the name: usually the person's page."""
    out = web.output if isinstance(web.output, dict) else {}
    urls = []
    for r in out.get("results") or []:
        url = r.get("url") or ""
        host = urllib.parse.urlparse(url).hostname or ""
        if "google.com" in host or not same_site(host, domain):
            continue
        if tokens(name) & tokens(f"{r.get('title', '')} {url}"):
            urls.append(url)
    return urls[:limit]


# ---- The waves ----

def wave1(c: Collected, use_cache: bool) -> None:
    email, name, domain = c.email, c.name, c.domain
    domains = sorted({domain, registrable(domain)})
    client = c.client
    identify_company = (lambda d: exact_company_for_domain(client, d)) if use_cache else (lambda d: client.post("company_identify", "/company/identify", {"domains": [d]}))
    search_body = {
        "filters": {"op": "and", "conditions": [
            {"field": "basic_profile.name", "type": "(.)", "value": name},
            {"op": "or", "conditions": [{"field": "experience.employment_details.current.company_website_domain", "type": "=", "value": d} for d in domains]},
        ]},
        "fields": SEARCH_FIELDS, "limit": 10,
    }
    jobs = [
        lambda: source("email_lookup", f"email_lookup({email})", lambda: email_lookup(email)),
        lambda: client.post("person_search", "/person/search", search_body),
        *[(lambda d=d: identify_company(d)) for d in domains],
    ]
    lookup, search, *companies = parallel(jobs)
    c.sources.append(lookup)

    for d, call in zip(domains, companies):
        match = exact_match(call.output, d)
        if match:
            c.company_id = match.get("crustdata_company_id") or (match.get("basic_info") or {}).get("crustdata_company_id")
            c.company_name = (match.get("basic_info") or {}).get("name") or ""
            break

    # 1. Exactly one person with this name whose current employer is the email's domain.
    found = by_domain(profiles(search), name, domain)
    if len(found) == 1:
        c.person, c.match = found[0], "domain"
        c.match_note = "person_search: the only profile with this name whose current employer's domain is the email's domain"
        return
    if len(found) > 1:
        c.match, c.match_note = "ambiguous", f"{len(found)} profiles with this name list the email's domain as current employer"
        return
    # 3. Profiles often lack the employer's domain (CMU's has none), so match on the email-domain
    #    organisation's name: as current employer, and for academic emails also as current school.
    if c.company_name:
        searches = [lambda: client.post("person_search", "/person/search", {
            "filters": {"op": "and", "conditions": [
                {"field": "basic_profile.name", "type": "(.)", "value": name},
                {"field": "experience.employment_details.current.company_name", "type": "(.)", "value": c.company_name},
            ]},
            "fields": SEARCH_FIELDS, "limit": 10,
        })]
        if domain_type(domain) == "academic":
            searches.append(lambda: client.post("person_search", "/person/search", {
                "filters": {"op": "and", "conditions": [
                    {"field": "basic_profile.name", "type": "(.)", "value": name},
                    {"field": "education.schools.school", "type": "(.)", "value": c.company_name},
                ]},
                "fields": SCHOOL_FIELDS, "limit": 10,
            }))
        results = parallel(searches)
        found_by: dict[Any, tuple[dict[str, Any], str]] = {}
        for row in by_employer_name(profiles(results[0]), name, c.company_name):
            found_by.setdefault(row.get("crustdata_person_id"), (row, "employer"))
        if len(results) > 1:
            for row in by_school(profiles(results[1]), name, c.company_name):
                found_by.setdefault(row.get("crustdata_person_id"), (row, "school"))
        if len(found_by) == 1:
            row, how = next(iter(found_by.values()))
            c.person, c.match = row, how
            c.match_note = (f"person_search: the only profile with this name currently at {c.company_name}" if how == "employer"
                            else f"person_search: the only profile with this name currently studying at {c.company_name}")
            return
        if len(found_by) > 1:
            c.match, c.match_note = "ambiguous", f"{len(found_by)} profiles with this name are currently at {c.company_name}"
            return
    c.match_note = c.match_note or "No Crustdata profile with this name and the email's domain"


def employer_from_person(c: Collected) -> None:
    """Prefers the matched person's own current employer id when it sits on the email's domain."""
    for role in current_roles(c.person or {}):
        if role.get("crustdata_company_id") and same_site(role.get("company_website_domain") or "", c.domain):
            c.company_id = role["crustdata_company_id"]
            c.company_name = role.get("name") or c.company_name
            return


def person_jobs(c: Collected) -> list[Any]:
    client, person = c.client, c.person or {}
    url, pid = profile_url(person), person.get("crustdata_person_id")
    jobs = []
    if url:
        jobs.append(lambda: client.post("person_enrich", "/person/enrich", {"professional_network_profile_urls": [url], "fields": PERSON_FIELDS}))
    if pid:
        jobs.append(lambda: client.post("social_post_search", "/social_post/search", {
            "filters": {"field": "actor.crustdata_entity_id", "type": "=", "value": int(pid)}, "limit": 5}))
    return jobs


def company_jobs(c: Collected, use_cache: bool) -> list[Any]:
    client, cid = c.client, c.company_id
    if not cid:
        return []
    key = str(int(cid))
    def call(kind: str, tool: str, endpoint: str, body: dict[str, Any]) -> Any:
        return (lambda: cached_call(client, kind, key, tool, endpoint, body)) if use_cache else (lambda: client.post(tool, endpoint, body))
    return [
        call("company_enrich", "company_enrich", "/company/enrich", {"crustdata_company_ids": [int(cid)], "fields": COMPANY_FIELDS}),
        call("job_search", "job_search", "/job/search", {
            "filters": {"field": "company.basic_info.company_id", "type": "=", "value": int(cid)},
            "sorts": [{"field": "metadata.date_added", "order": "desc"}], "fields": JOB_FIELDS, "limit": 10}),
        call("company_posts", "social_post_search", "/social_post/search", {
            "filters": {"field": "actor.crustdata_entity_id", "type": "=", "value": int(cid)}, "limit": 5}),
    ]


def wave2(c: Collected, use_cache: bool) -> None:
    client = c.client
    if c.person:
        employer_from_person(c)
    jobs = person_jobs(c) if c.person else []
    jobs += company_jobs(c, use_cache)

    lookup = next((s.output for s in c.sources if s.tool == "email_lookup" and isinstance(s.output, dict)), {})
    logins = {x.get("github_login") for x in ((lookup.get("github") or {}).get("commits_by_this_email") or []) if x.get("github_login")}
    for login in sorted(logins)[:1]:
        jobs.append(lambda login=login: source("github.user", f"GET api.github.com/users/{login}",
                                               lambda: _get(f"https://api.github.com/users/{login}", {"Accept": "application/vnd.github+json"})))
    for result in parallel(jobs):
        if isinstance(result, Source):
            c.sources.append(result)


def call_output(c: Collected, tool: str) -> Any:
    for call in reversed(c.client.calls):
        if call.tool == tool and not call.error and call.output not in (None, [], {}):
            return call.output
    return None


def enriched_person(c: Collected) -> dict[str, Any]:
    out = call_output(c, "person_enrich")
    try:
        return out[0]["matches"][0]["person_data"] or {}
    except (TypeError, KeyError, IndexError):
        return {}


def wave3(c: Collected) -> None:
    """Pages the person links to themselves: their GitHub profile and the homepage it or Crustdata names.
    These are where X handles usually are, since Crustdata's own X field is mostly empty."""
    fetched_users = {s.request.rsplit("/", 1)[-1].lower() for s in c.sources if s.tool == "github.user"}
    fetched_pages = {s.request.split(" ")[1] for s in c.sources if s.tool == "web.page"}
    gh = {"Accept": "application/vnd.github+json"}
    jobs = []
    for profile in enriched_person(c).get("dev_platform_profiles") or []:
        login = (profile.get("profile_url") or "").rstrip("/").rsplit("/", 1)[-1]
        if login and login.lower() not in fetched_users:
            fetched_users.add(login.lower())
            jobs.append(lambda login=login: source("github.user", f"GET api.github.com/users/{login}",
                                                   lambda: _get(f"https://api.github.com/users/{login}", gh)))
    for result in parallel(jobs):
        c.sources.append(result)
    pages = []
    for s in c.sources:
        if s.tool == "github.user" and isinstance(s.output, dict) and s.output.get("blog"):
            pages.append(s.output["blog"] if s.output["blog"].startswith("http") else "https://" + s.output["blog"])
    for profile in enriched_person(c).get("dev_platform_profiles") or []:
        for handle in profile.get("declared_handles") or []:
            if handle.get("provider") == "website" and handle.get("url"):
                pages.append(handle["url"])
    pages = [p for p in dict.fromkeys(pages) if p not in fetched_pages][:2]
    for result in parallel([(lambda url=url: source("web.page", f"GET {url} (text only)", lambda: page_text(url))) for url in pages]):
        c.sources.append(result)


def find_x(c: Collected) -> None:
    """X handles for the person and the employer, each with where it came from.

    Person: Crustdata's own field first, then GitHub's twitter_username, then X links on pages the
    person's own GitHub or Crustdata profile points to. Links from a page can be a lab's or a
    university's account, so they're candidates for the researcher to check, not facts.
    """
    person: list[dict[str, str]] = []
    seen: set[str] = set()

    def add(handle: str, where: str, confirmed: bool) -> None:
        key = handle.lower()
        if handle and key not in seen:
            seen.add(key)
            person.append({"handle": handle, "url": f"https://x.com/{handle}", "source": where, "confirmed": confirmed})

    slug = (((enriched_person(c).get("social_handles") or (c.person or {}).get("social_handles") or {}).get("twitter_identifier")) or {}).get("slug")
    if slug:
        add(slug.lstrip("@"), "person_enrich · social_handles.twitter_identifier.slug", True)
    for s in c.sources:
        if s.tool == "github.user" and isinstance(s.output, dict) and s.output.get("twitter_username"):
            add(s.output["twitter_username"], f"{s.request} · twitter_username", True)
    for s in c.sources:
        if s.tool == "web.page" and isinstance(s.output, str):
            for handle in x_handles_in(s.output.rsplit("X links on this page:", 1)[-1] if "X links on this page:" in s.output else ""):
                add(handle, s.request.split(" ")[1], False)
    company = None
    try:
        info = call_output(c, "company_enrich")[0]["matches"][0]["company_data"]
        url = (info.get("social_profiles") or {}).get("twitter_url")
        if url:
            company = {"url": url, "source": "company_enrich · social_profiles.twitter_url"}
    except (TypeError, KeyError, IndexError):
        pass
    c.x = {"person": person, "company": company}


WEB_TIMEOUT = 25  # seconds; Crustdata's live web search sometimes takes 30 s or more


def collect(email: str, name: str, client: Client, use_cache: bool = True) -> Collected:
    """Runs both waves and returns as soon as they're done. The two web searches keep running in the
    background; call finish_web() before writing to wait for them and fetch the homepages they find."""
    started = time.perf_counter()
    domain = email.split("@")[-1].strip().lower()
    c = Collected(email=email.strip(), name=(name or "").strip(), domain=domain, client=client)
    query = f"{c.name} {registrable(domain)}"
    c.background = ThreadPoolExecutor(max_workers=2)
    c.web = c.background.submit(client.post, "web_search_live", "/web/search/live", {"query": query, "sources": ["web"]}, WEB_TIMEOUT)
    c.news = c.background.submit(client.post, "web_search_live", "/web/search/live", {"query": query, "sources": ["news"]}, WEB_TIMEOUT)
    wave1(c, use_cache)
    wave2(c, use_cache)
    wave3(c)
    find_x(c)
    c.seconds = round(time.perf_counter() - started, 2)
    return c


def finish_web(c: Collected) -> None:
    """Waits for the background web searches, then fetches the homepages they point at."""
    if c.background is None:
        return
    web = c.web.result()
    c.news.result()
    c.background.shutdown()
    c.background = None
    fetched = {s.request.split(" ")[1] for s in c.sources if s.tool == "web.page"}
    pages = [p for p in homepage_candidates(web, c.domain, c.name) if p not in fetched]
    for result in parallel([(lambda url=url: source("web.page", f"GET {url} (text only)", lambda: page_text(url))) for url in pages]):
        c.sources.append(result)
    find_x(c)


def add_person(c: Collected, person_url: str, person_id: str | None) -> None:
    """After the researcher identifies the person: fetch the person's half of wave 2, then their pages."""
    c.person = {"crustdata_person_id": person_id, "social_handles": {"professional_network_identifier": {"profile_url": person_url}}}
    c.match = "researcher"
    parallel(person_jobs(c))
    wave3(c)
    find_x(c)
