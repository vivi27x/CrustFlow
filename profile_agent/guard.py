"""Checks every Crustdata script before it runs.

The Crustdata MCP server runs JavaScript that can call any of ~60 tools, including ones that cost a
lot, reveal contact details or create standing watchers. Claude Code's PreToolUse hook lets us read
each script first and deny it with a reason Claude sees and can act on.
"""

from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass

# People-search and contact-broker sites: they sell contact data and often merge namesakes.
# LinkedIn forbids scraping, and Crustdata already covers it.
BLOCKED_SITES = {
    "linkedin.com", "contactout.com", "rocketreach.co", "zoominfo.com", "apollo.io", "lusha.com", "signalhire.com",
    "spokeo.com", "whitepages.com", "truecaller.com", "peekyou.com", "pipl.com", "beenverified.com", "radaris.com",
    "intelius.com", "fastpeoplesearch.com", "truepeoplesearch.com", "humantic.ai", "seamless.ai", "clearbit.com",
    "hunter.io", "snov.io", "skrapp.io", "adapt.io", "leadiq.com", "uplead.com", "kaspr.io", "salesql.com",
}


def blocked_url(url: str) -> str | None:
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    for site in BLOCKED_SITES:
        if host == site or host.endswith("." + site):
            return f"Blocked {host}: people-search and contact-broker sites, and LinkedIn, are off limits. Use Crustdata for LinkedIn data."
    return None


CALL = re.compile(r"callTool\s*\(\s*(['\"`])([A-Za-z0-9_/]+)\1")
ANY_CALL = re.compile(r"callTool\s*\(")
CONTACT_FIELD = re.compile(
    r"contact\.(?!has_)[a-z_]+|(?<!has_)business_emails?|(?<!has_)personal_emails?|(?<!has_)phone_numbers?|\bverified\s*:"
)

# Free, read-only or cheap research tools the agent may use.
ALLOWED = {
    "company_identify", "company_search", "company_enrich", "company_autocomplete",
    "person_search", "person_enrich", "person_autocomplete",
    "job_search", "job_autocomplete", "social_post_search", "dev_platform_enrich",
    "employee_review_enrich", "web_search_live", "web_enrich_live",
    "account_credits", "account_endpoints", "describe_scope",
    "crustdata_docs_search", "crustdata_docs_fs",
    "watch_preview", "watch_list", "watch_get", "watch_discovery_list", "watch_discovery_get",
}
REASONS = {
    "person_contact_enrich": "contact data is out of scope: we never fetch emails or phone numbers for a signup",
    "watch_create": "creating watchers has standing side effects", "watch_discovery_create": "creating watchers has standing side effects",
    "watch_update": "changing watchers has side effects", "watch_discovery_update": "changing watchers has side effects",
    "watch_cancel": "cancelling watchers has side effects", "watch_discovery_cancel": "cancelling watchers has side effects",
    "watch_test": "test notifications send real messages", "watch_discovery_test": "test notifications send real messages",
}


@dataclass
class Verdict:
    allowed: bool
    reason: str
    tools: list[str]


def check_script(code: str) -> Verdict:
    tools = [m.group(2) for m in CALL.finditer(code)]
    if len(tools) != len(ANY_CALL.findall(code)):
        return Verdict(False, "Every callTool must name its tool as a string literal, such as callTool('person_search', …), so the guard can check it.", tools)
    for name in tools:
        if name in REASONS:
            return Verdict(False, f"Blocked {name}: {REASONS[name]}.", tools)
        if name.startswith("batch_") or (name.endswith("_live") and name not in ALLOWED):
            return Verdict(False, f"Blocked {name}: batch and live people, company, job and social endpoints are off for this agent. Use the indexed tools.", tools)
        if name not in ALLOWED:
            return Verdict(False, f"Blocked {name}: not on this agent's allowlist. Allowed: {', '.join(sorted(ALLOWED))}.", tools)
    if CONTACT_FIELD.search(code):
        return Verdict(False, "Blocked: the script asks for contact fields (emails, phones). Contact data is out of scope; contact.has_* flags are fine.", tools)
    return Verdict(True, "", tools)
