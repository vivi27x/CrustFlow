"""Direct calls to Crustdata's REST API, and the company cache.

Code makes these calls itself, in parallel, so no model time goes on planning them. Each call
records its credits (from the X-Credits-Used header) and when it was fetched.

Company cache
- Keyed by Crustdata's numeric company id, never by name, so two companies with the same name
  never share an entry.
- A domain maps to an id only when it is that company's exact primary domain. company_identify
  can fuzzy-match several companies for one domain; anything else is not cached.
- Entries expire: company profile after 7 days, jobs and posts after 1 day. An entry only counts
  when it was fetched with exactly the same request, so adding a field refetches.
- Files are written atomically, so runs that overlap can't leave a half-written entry.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from profile_agent.config import CACHE_DIR, CRUSTDATA_API_URL, CRUSTDATA_API_VERSION, Settings
from profile_agent.lookups import USER_AGENT

DAY = 86_400
TTL = {"domain": 30 * DAY, "company_enrich": 7 * DAY, "job_search": DAY, "company_posts": DAY}


@dataclass
class Call:
    """One Crustdata request: what was asked, what came back, what it cost and when."""
    tool: str
    endpoint: str
    request: dict[str, Any]
    output: Any = None
    error: str | None = None
    credits: float = 0.0
    fetched_at: float = 0.0
    cached: bool = False
    seconds: float = 0.0


@dataclass
class Client:
    settings: Settings
    calls: list[Call] = field(default_factory=list)
    on_call: Callable[[Call], None] | None = None   # told about every call, for live progress

    def record(self, call: Call) -> None:
        self.calls.append(call)
        if self.on_call is not None:
            self.on_call(call)

    def post(self, tool: str, endpoint: str, body: dict[str, Any], timeout: float = 45) -> Call:
        call = Call(tool=tool, endpoint=endpoint, request=body, fetched_at=time.time())
        request = urllib.request.Request(
            f"{CRUSTDATA_API_URL}{endpoint}", data=json.dumps(body).encode(), method="POST",
            headers={
                "Authorization": f"Bearer {self.settings.crustdata_api_key}", "x-api-version": CRUSTDATA_API_VERSION,
                "content-type": "application/json", "User-Agent": USER_AGENT,
            },
        )
        started = time.perf_counter()
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                call.credits = float(response.headers.get("x-credits-used") or 0)
                call.output = json.loads(response.read() or b"null")
        except urllib.error.HTTPError as exc:
            call.error = f"HTTP {exc.code}"
            try:
                call.output = json.loads(exc.read() or b"null")
            except ValueError:
                pass
        except Exception as exc:  # noqa: BLE001 - one failed call must not lose the others.
            call.error = f"{type(exc).__name__}: {exc}"[:200]
        call.seconds = round(time.perf_counter() - started, 2)
        self.record(call)
        return call


# ---- Cache ----

def _path(kind: str, key: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in key.lower())
    return CACHE_DIR / kind / f"{safe}.json"


def cache_get(kind: str, key: str, body: dict[str, Any]) -> dict[str, Any] | None:
    """A fresh entry fetched with exactly this request. Asking for different fields is a miss, so an
    entry saved before a field was added can't hide that field."""
    path = _path(kind, key)
    try:
        entry = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if time.time() - entry.get("fetched_at", 0) > TTL[kind] or entry.get("request") != body:
        return None
    return entry


def cache_put(kind: str, key: str, call: Call) -> None:
    """Only successful, non-empty answers are cached; an error or empty result is retried next time."""
    if call.error or call.output in (None, [], {}):
        return
    path = _path(kind, key)
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {"fetched_at": call.fetched_at, "request": call.request, "output": call.output, "credits": call.credits}
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    with os.fdopen(fd, "w") as handle:
        json.dump(entry, handle)
    os.replace(tmp, path)


def cached_call(client: Client, kind: str, key: str, tool: str, endpoint: str, body: dict[str, Any]) -> Call:
    entry = cache_get(kind, key, body)
    if entry is not None:
        call = Call(tool=tool, endpoint=endpoint, request=entry["request"], output=entry["output"],
                    fetched_at=entry["fetched_at"], cached=True)
        client.record(call)
        return call
    call = client.post(tool, endpoint, body)
    cache_put(kind, key, call)
    return call


def exact_company_for_domain(client: Client, domain: str) -> Call:
    """company_identify on a domain, cached only when one match's primary domain is exactly this domain."""
    entry = cache_get("domain", domain, {"domains": [domain]})
    if entry is not None:
        call = Call(tool="company_identify", endpoint="/company/identify", request=entry["request"],
                    output=entry["output"], fetched_at=entry["fetched_at"], cached=True)
        client.record(call)
        return call
    call = client.post("company_identify", "/company/identify", {"domains": [domain]})
    if exact_match(call.output, domain) is not None:
        cache_put("domain", domain, call)
    return call


def exact_match(output: Any, domain: str) -> dict[str, Any] | None:
    """The one identified company whose primary domain is exactly `domain`, else None."""
    matches = []
    for row in output if isinstance(output, list) else []:
        for match in (row or {}).get("matches") or []:
            info = (match.get("company_data") or {}).get("basic_info") or {}
            if (info.get("primary_domain") or "").lower() == domain.lower():
                matches.append(match["company_data"])
    return matches[0] if len(matches) == 1 else None


def parallel(jobs: list[Callable[[], Any]]) -> list[Any]:
    from concurrent.futures import ThreadPoolExecutor

    if not jobs:
        return []
    with ThreadPoolExecutor(max_workers=min(8, len(jobs))) as pool:
        return list(pool.map(lambda job: job(), jobs))
