"""Free lookups keyed on the email itself. These are the strongest identity links we can get:
a public profile or commit made with this exact address belongs to whoever owns it.
"""

from __future__ import annotations

import hashlib
import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from profile_agent.config import DOMAIN_LISTS

USER_AGENT = "crustflow-profile-agent/0.1"
ACADEMIC_SUFFIXES = (".edu", ".ac.in", ".ac.uk", ".edu.in", ".ac.jp", ".edu.au", ".ac.nz", ".ac.kr", ".edu.sg")


def _list(name: str) -> set[str]:
    path = DOMAIN_LISTS / name
    if not path.exists():
        return set()
    return {l.strip().lower() for l in path.read_text().splitlines() if l.strip() and not l.startswith("#")}


def _in(domain: str, domains: set[str]) -> bool:
    parts = domain.split(".")
    return any(".".join(parts[i:]) in domains for i in range(len(parts) - 1))


def _get(url: str, headers: dict[str, str] | None = None) -> tuple[int | str, Any]:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status, json.loads(response.read() or b"null")
    except urllib.error.HTTPError as exc:
        return exc.code, None
    except Exception as exc:  # noqa: BLE001 - a failed lookup is reported, never fatal.
        return f"error: {type(exc).__name__}", None


def domain_type(domain: str) -> str:
    if _in(domain, _list("disposable_domains.txt")):
        return "disposable"
    if domain in _list("freemail_domains.txt") or domain == "googlemail.com":
        return "personal"
    if domain.endswith(ACADEMIC_SUFFIXES):
        return "academic"
    if _in(domain, _list("competitor_domains.txt")):
        return "competitor_data_vendor"
    return "work_or_other"


def gravatar(email: str) -> dict[str, Any]:
    digest = hashlib.sha256(email.strip().lower().encode()).hexdigest()
    status, data = _get(f"https://api.gravatar.com/v3/profiles/{digest}")
    if status != 200 or not isinstance(data, dict):
        return {"found": False, "status": status}
    keep = ("display_name", "profile_url", "location", "job_title", "company", "description", "verified_accounts", "pronouns")
    return {"found": True, **{k: data.get(k) for k in keep if data.get(k)}}


def github_by_email(email: str) -> dict[str, Any]:
    headers = {"Accept": "application/vnd.github+json"}
    q = urllib.parse.quote(f"author-email:{email}")
    status, data = _get(f"https://api.github.com/search/commits?q={q}&per_page=5&sort=author-date", headers)
    commits: list[dict[str, Any]] = []
    if status == 200 and isinstance(data, dict):
        for item in data.get("items") or []:
            author = item.get("author") or {}
            commit = item.get("commit") or {}
            commits.append({
                "github_login": author.get("login"),
                "github_url": author.get("html_url"),
                "author_name": (commit.get("author") or {}).get("name"),
                "date": ((commit.get("author") or {}).get("date") or "")[:10],
                "repo": (item.get("repository") or {}).get("full_name"),
            })
    q = urllib.parse.quote(f"{email} in:email")
    ustatus, udata = _get(f"https://api.github.com/search/users?q={q}", headers)
    users = [u.get("html_url") for u in (udata or {}).get("items", [])] if ustatus == 200 and isinstance(udata, dict) else []
    return {
        "commit_search_status": status,
        "commits_by_this_email": commits,
        "total_commits": (data or {}).get("total_count") if isinstance(data, dict) else None,
        "users_with_public_email": users,
    }


def email_lookup(email: str) -> dict[str, Any]:
    email = email.strip()
    local, _, domain = email.lower().partition("@")
    return {
        "email": email,
        "local_part": local,
        "domain": domain,
        "domain_type": domain_type(domain),
        "gravatar": gravatar(email),
        "github": github_by_email(email),
    }
