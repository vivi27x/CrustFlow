"""Deterministic monthly credit estimate. The inputs are printed on the card, so reps can argue with them."""

from __future__ import annotations

from dataclasses import dataclass, field

from app.models import Company, Playbook

PER_RESULT = 0.23  # 0.03 base, 0.1 to filter on experience, 0.1 to receive it.
RESULTS_PER_SEARCH = 25
SEARCHES_PER_ROLE = 20
SHORTLIST_PER_ROLE = 25
SEARCHES_PER_SELLER = 20
SHORTLIST_PER_SELLER = 30
SELLER_SHARE = 0.1


@dataclass
class Estimate:
    credits_per_month: int
    assumptions: list[str] = field(default_factory=list)


def _open_roles(company: Company) -> int:
    return max(company.openings or 0, company.jobs_total or 0, len(company.jobs), 1)


def estimate_credits(playbook: Playbook, company: Company) -> Estimate:
    if playbook is Playbook.RECRUITING:
        roles = _open_roles(company)
        searches = roles * SEARCHES_PER_ROLE
        enriches = roles * SHORTLIST_PER_ROLE
        credits = searches * RESULTS_PER_SEARCH * PER_RESULT + enriches
        return Estimate(round(credits), [
            f"{roles} open roles",
            f"{SEARCHES_PER_ROLE} searches per role = {searches} searches",
            f"{RESULTS_PER_SEARCH} results per search at {PER_RESULT} credits each",
            f"{SHORTLIST_PER_ROLE} shortlisted per role = {enriches} enriches at 1 credit",
        ])
    sellers = max(1, round((company.headcount or 10) * SELLER_SHARE))
    searches = sellers * SEARCHES_PER_SELLER
    enriches = sellers * SHORTLIST_PER_SELLER
    credits = searches * RESULTS_PER_SEARCH * PER_RESULT + enriches
    return Estimate(round(credits), [
        f"{sellers} sellers ({SELLER_SHARE:.0%} of {company.headcount or 'about 10'} people)",
        f"{SEARCHES_PER_SELLER} searches per seller = {searches} searches",
        f"{RESULTS_PER_SEARCH} results per search at {PER_RESULT} credits each",
        f"{SHORTLIST_PER_SELLER} shortlisted per seller = {enriches} enriches at 1 credit",
    ])
