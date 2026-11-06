# Crustdata API notes (version 2025-11-01)

Compiled 4-5 Nov 2026 from docs.crustdata.com. Defaults may differ on our keys: check `GET /account/endpoints` (free) and fill in "Verified on our key" at the bottom.

## Basics
- Base URL `https://api.crustdata.com`. Headers on every call: `Authorization: Bearer <key>`, `x-api-version: 2025-11-01`, `content-type: application/json`.
- Every response carries an `X-Credits-Used` header.
- Free account calls: `GET /account/endpoints` (access, field permissions, rate limits, prices) and `GET /account/credits` (balance, recurring grant, refresh date).
- MCP server: `https://install.crustdata.com/mcp` (streamable HTTP; OAuth or API key). There is also a Claude Code plugin, a Codex plugin and a CLI.
- Docs index for agents: `https://static-assets.crustdata.com/llms.txt` (the docs-domain `/llms.txt` lists only a subset). Append `.md` to any docs URL for markdown.

## Endpoints we use
| Purpose | Endpoint | Cost (credits) | Requests/min |
|---|---|---|---|
| Domain / name to company | POST /company/identify | free | 30 |
| Company profile | POST /company/enrich | 2 per company (+2 technographics) | 15 |
| Email / profile URL to person | POST /person/identify | 1 per match; unmatched free | 30 |
| Person profile | POST /person/enrich | 1 base (+ add-ons) | 15 |
| People search | POST /person/search | 0.03 per result + premium groups | 30 |
| Company search | POST /company/search | 0.03 per result + premium groups | 30 |
| Job postings | POST /job/search | 0.03 per result | 30 |
| Autocomplete (person / company / job) | POST /{dataset}/search/autocomplete | free | 300 (`/account/endpoints`). The 45 on the API reference belongs to the disabled `/professional_network/search/autocomplete` |
| Web search | POST /web/search/live | 1 per query | 10 |
| Web fetch (up to 10 URLs) | POST /web/enrich/live | 1 per page | 10 |
| Watchers | /watch/{dataset}/... | first run free (baseline up to 5 matches); then per new or changed record | 10 per path |

Avoid the professional-network live endpoints (`.../professional_network/.../live`) and `enrich_realtime`: enterprise or add-on only, and slow.

## Person identify
- Accepts profile URLs, emails (business or personal) or Google Scholar URLs. One identifier type per request; up to 25 per sync call. Batch: up to 300 emails per job.
- Body: `{"emails": ["a@b.com"]}`. `business_emails` is rejected with a 400.
- The response is a top-level array, one row per input: `matched_on`, `match_type`, `match_status` (`matched` / `not_found` / `redacted`), `matches[0].confidence_score` (1.0 for exact; 0.5-1.0 for name + domain matches), `matches[0].person_data` with `crustdata_person_id`, `basic_profile` (name, headline, `current_title`, `normalized_title`, location), `social_handles` and `updated_at`.
- No employer in the response. To get one: `POST /person/enrich` with `professional_network_profile_urls` and `fields: ["experience"]`, then `/company/identify` with `names`.
- `redacted` means the person asked for their data to be removed. Stop processing; it never bills.
- 403 `permission_error` means identify is not enabled on the key. 503 is free to retry.
- Enabled per account: ask for access. A malformed email in a sync request rejects the whole request.

## Company enrich
- Company identify and enrich both return a top-level array of `{matched_on, match_type, matches: [{confidence_score, company_data}]}`. No match is a 200 with `matches: []`.
- Inputs (one type per request, up to 25): `domains`, `professional_network_profile_urls`, `names`, `crustdata_company_ids`. `exact_match: true` limits domain matches to exact primary domains.
- `fields` groups: basic_info (default), headcount, funding, hiring, people, locations, taxonomy, revenue, followers, competitors, web_traffic, seo, news, social_profiles, software_reviews, employee_reviews, market_intel, public_launches, technographics (needs permission), social_posts (needs permission). Request whole groups, not subfields (subfield requests return sibling keys as null). `taxonomy` must be listed explicitly.
- Useful keys: `headcount.total`, `headcount.growth_percent.{mom,qoq,six_months,yoy,two_years}`, `funding.total_investment_usd`, `funding.last_round_type`, `funding.last_fundraise_date`, `hiring.openings_count`, `hiring.recent_titles_csv`. In company search the growth filters use different keys: `headcount.growth_percent.{1m,3m,6m,12m}`.

## Person search
- Body: `filters` is one `{field, type, value}` condition or a group `{op: and|or|all_of, conditions: [...]}`. Also `fields`, `sorts`, `limit` (1-1000), `cursor`, `search`, `mode`, `explain`, `post_processing`.
- `=>` and `=<` are the only range spellings; `>=` and `<=` return 400.
- Seniority is a closed list: Entry Level, Entry Level Manager, Senior, Director, Owner / Partner, CXO, Vice President, In Training, Experienced Manager, Strategic. "Founder" and "CEO" return 400.
- `fields` and filters name the employer differently: `...current.name` in `fields`, `...current.company_name` in filters.
- Filters:
  - Education: `education.schools.school`, `.degree`, `.end_year`.
  - Current role: `experience.employment_details.current.company_name`, `.current.title`, `.current.company_website_domain`, `.current.seniority_level`, `.current.start_date`.
  - Past roles: `experience.employment_details.past.company_name`, `.past.title`.
  - Profile: `basic_profile.headline`, `basic_profile.normalized_title`.
  - Location: `basic_profile.location.country`, `.full_location`; `professional_network.location.raw` (supports `geo_distance`).
  - `recently_changed_jobs`: a role ended within 90 days of the last index rebuild.
- Operators: `=`, `in`, `(.)` fuzzy, `[.]` exact phrase, `=>`, `=<`.
- Semantic ranking: a `search` block (`{"query": "..."}`) ranks by meaning (drop `sorts` when using it). `"mode": "exact"` sits at the top level. `search.explain` returns scores. `"mode": "hybrid"` is not documented: test before using.
- Cost: 0.03 per result base. `experience`, `education` and `skills` each add 0.1 per result to filter on and 0.1 per result to receive (filtering on and receiving the same group bills twice). Sorting on them is free. Empty results are free.
- Preview mode (`"preview": true`) is premium; 403 without access.
- Response: `profiles`, `next_cursor`, `total_count`, `total_count_relation`, `remarks`.

## Job search
- Results are under `job_listings`. Filter with `company.basic_info.primary_domain` or `company.basic_info.company_id` (`crustdata_company_id` is not filterable). Sort with `{"field": "metadata.date_added", "order": "desc"}`.
- Fields: `job_details.title`, `.url`, `.source` (professional_network, greenhouse, lever, workday...; search only, not live), `.category`, `workplace_type`, `employment_type`, `reposted_job`; `company.basic_info.name`, `primary_domain`, `crustdata_company_id`; `location.raw` plus normalised city, state and country; `metadata.date_added`, `date_updated`; `content.description`; `crustdata_job_id` (dedupe key). No documented salary field.

## Web search and fetch
- `sources`: web, news, scholar-articles, scholar-author, ai, social. Always set them explicitly. Params: query, location, site, start_date / end_date (unix seconds), page, human_mode.
- Results: source, title, url, snippet, position, plus source-specific fields. `ai` results have no top-level url: cite `references[].url`. Retry `failed_pages`, not `empty_pages`. Don't quote company names. `fetch_content` was removed: search first, then fetch.
- Web fetch returns raw HTML in `content`; failed entries have `url: null`.

## Freshness
Indexed people, companies and jobs refresh every 1-30 days (social posts 1-45). Check `updated_at` (jobs: `date_added`, `date_updated`).

## Rate limits and retries
Defaults are in the table above; the window is sliding, not fixed. Spread traffic evenly; use exponential backoff with jitter; wait `x-ratelimit-reset` seconds on a 429 and honour `Retry-After` if present; use one shared client with per-endpoint limits. `X-Credits-Used` is absent on 429 responses.

## Errors
- Shape: `{"error": {"type", "message", "metadata"}}`.
- Out of credits is documented as 402 in best practices and 403 in the person-search reference. Branch on `error.type == "insufficient_credits"`, never on the status alone, and don't retry.
- 403 `permission_error` covers endpoint denial and field denial (`metadata.denied_fields`). Don't retry the same body.

## Credit rounding
The credits page says "Balance deduction rounds a fractional cost up to the next whole credit", while `X-Credits-Used` reports the exact decimal. If that applies per call, a 5.45-credit signup really deducts about 7 (1 + 2 + 1 + 3). Test it by reading `/account/credits` before and after a fractional call.

## Verified on our key
Measured on 5 Nov 2026 with `scripts/probe_api.py` and `/account/endpoints`, which lists every endpoint, field, rate limit and price for a key.

| Check | Result |
|---|---|
| `/person/identify` enabled? | **No: 403 `permission_error`.** Enabled per account on request. `/person/enrich` takes profile URLs only, so nothing else turns an email into a person |
| Credit balance on the hackathon key | 15,192 with the promo-code key. Its `/person/identify` is also disabled |
| Fractional credits rounded up per call? | No. Enrich (2) plus jobs (0.03) moved the balance from 231.97 to 229.94 exactly |
| Latency | Company identify 0.4-1 s, company enrich 1.3-1.8 s, person search and job search 0.3-0.4 s, autocomplete 0.3 s |
| Autocomplete | "Bangalore" gives `Bangalore Rural` first, then `Bangalore`. "SaaS" gives nothing on `taxonomy.professional_network_industry` but matches on `taxonomy.categories` |
| Rate-limit headers seen | `x-ratelimit-limit: 30`, `x-ratelimit-remaining`, `x-ratelimit-reset: 60` |
| `/web/search/live` | Works. `sources: ["scholar-articles"]` returned 503 `service_unavailable` on 4 of 4 tries, at no charge |
| Person-search semantic syntax | Not tested. We don't use it |

### Where the live API differs from what we assumed
| Topic | Real behaviour |
|---|---|
| Person search `fields` | `updated_at` is refused. Use `metadata.updated_at`. Identify still returns a top-level `updated_at` |
| `current` in person search | Always a list, even for one role |
| `basic_info.industries` | Returned by identify and accepted as a search filter. Refused in search `fields`, absent from enrich, and autocomplete gives nothing for it. Use `taxonomy.professional_network_industry` |
| Company search price | 0.03 per row, plus per group, once to filter and once to receive: `headcount` 0.2, `taxonomy` 0.1, `funding` 0.2, `hiring` 0.2 |
| `headcount.largest_headcount_country` | An ISO code such as `IND`, not a name. Person search filters on names |
| Job `location.raw` | Can be a bare city, such as `Bengaluru`, with no country |
| `recently_changed_jobs` | Filter on that bare name. `experience.recently_changed_jobs` is refused as a filter |
| `/person/enrich` | Takes profile URLs only; `business_emails` gets a 400. Returns the same `crustdata_person_id` as search, `current` and `past` roles, `education.schools`, `skills.professional_network_skills` and `company_website_domain`. 1 credit a match; an unknown URL is `not_found` at 0 credits. A messy URL form still matches |
| `basic_profile.name` filters | `(.)` treats the value as a phrase, so "Noor Khan" misses a profile called "Noor Ahmed". Search each part. `total_count` tells you whether a match is unique |
| Buyers by market | `experience.employment_details.current.company_industries` and `.company_headcount_latest` filter people directly at 0.23 credits per row, so no company search is needed first |
| `/web/search/live` latency | Normally under 2 s, but one call took over 25 s, so don't put it on the main path |
