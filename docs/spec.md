# CrustFlow MVP spec

Corrected from the original PRD draft after a fact check against Crustdata's docs (Nov 2026). Where this file and the draft disagree, this file wins.

## Problem
Self-serve signups to an API product get a key and the docs, don't know what to try first, and leave. At Crustdata, activation was about 25% before onboarding started asking signups their goal (it then rose to over 50%). Companies pay solutions engineers to build custom proofs of concept, but only for big deals; the long tail of signups gets nothing.

## What CrustFlow does
For every signup, from their name and email: identify the person and company, infer the use case with cited evidence, run the first Crustdata query, deliver results plus runnable code plus a one-click second call, route to sales or self-serve, and catch abuse.

## The service
CrustFlow works for any company with self-serve signups. Three inputs make the email: what the company offers, the signup's profile, and Claude. The demo mimics Supabase, labelled a sample in every email, and sent only to the team's inbox.

Emails for Crustdata's own searches carry curl for engineers; everyone else gets the code in the sandbox, one click away.

## Vendor personas
A company is described in `app/personas/`: what it sells, its services and features, who signs up, and what to look for. Claude reads this to pick a segment and the services that fit the person. A segment with a playbook below runs a search. Any other segment gets the personal email, because a search only makes sense when the company sells data.

## Playbooks
| Playbook | Who | Signal | First query | Delivered |
|---|---|---|---|---|
| recruiting | Head of Talent at a startup (e.g. priya@hirefast.in) | open roles in job search; recruiting titles | person search for an open role: title + location, limit 10 | 10 matching profiles + code |
| gtm | Founder, RevOps, growth, AI-SDR builder | title; B2B SaaS taxonomy | company search for their ICP (growth, size, industry), then person search for buyers there | 5 companies + 10 buyer profiles + code |
| builder | Engineers, technical founders and data teams building on the API | their own role is technical, whatever their company sells | company search for a sample of real companies | 10 companies, code, sandbox; no sales alert |
| academic | Faculty or researcher (`.ac.in` / `.edu`, title Professor / Researcher) | profile title, department | web search (scholar-author / scholar-articles) in their field, or person search for researchers | relevant researchers or papers + code |
| student | Student (`.ac.in` / `.edu` or headline "Student") | profile headline | none (zero credit) | quickstart for free endpoints; no sales alert |
| unknown | Gmail or unresolved, low confidence | nothing reliable | none | "What are you building?" email with 3 buttons: Recruiting / Sales & GTM / Investing |
| investor (stretch) | VC / PE | company taxonomy | person search: founders at fast-growing startups | 10 founders + watcher config |

## Profile reports
A cheap name search runs first, so repeats and clear students stop there. A clear match takes the fast path: person enrich, company enrich and job search run together, in about 15 s overall with Claude. Everyone else gets a ReAct loop, which researches them and writes a `user-profile/v1` report that becomes their profile. A late, crashed or invalid report falls back to the basic builder. See `app/pipeline/profile_doc.py`.

## Starters
For a company whose persona sets `starter: true`, the email carries code the signup can run. Claude fills in a spec: a table, 2 to 6 typed columns, 5 invented rows and two search phrases. `app/pipeline/starter.py` writes the SQL from it, adding row-level security always, and what each picked service needs: vector gets an embedding column, an HNSW index and a match function; realtime gets the publication line; queues and cron get lines marked as needing a real project.

`/sandbox/{token}` runs the emailed SQL unchanged in PGlite, real Postgres with pgvector in the browser. Three buttons each change one thing, and a click is the second run that marks a signup Activated, for no credits. Sample embeddings are a word hash and are labelled that way. A spec we can't make safe drops the starter, and the email goes without it.

The email is a blueprint: each service Claude picked sits directly on the lines it adds, with its reason, in the order the SQL runs. A pick that adds no SQL still appears, with its reason alone. The sandbox shows the same SQL as one file to copy. Lines are laid out to fit in 72 characters, and colours are written inline by Pygments (`app/pipeline/highlight.py`), because email clients strip stylesheets. Sample rows have no owner in the SQL people read, and the sandbox gives them two sample owners in a labelled extra step.

## Gates (run before any paid call)
- Malformed email: reject with a friendly message.
- Disposable domain (static list): no credits, human queue.
- `+alias`: normalise before dedupe.
- Repeat identity: same `crustdata_person_id` as an earlier signup. Flag as possible free-credit farming, human queue, no new proof of concept.
- `crustdata.com`: internal branch.
- Competitor data vendors (configurable domain list): human queue, no free proof of concept.

## Gate ordering
Crustdata declined to enable `/person/identify`, so a signup gives a name and we match it. Repeat identity needs `crustdata_person_id`, which comes from that search, so it runs straight after it. Repeats of the same normalised email are caught for free before any call.

A personal email gives no company. A typed full name that exactly one person has is accepted and marked `name_only`, and `/person/enrich` (1 credit) then gives the employer's domain. Anything else gets the buttons.

Each signup's budget starts at 1 credit and rises to 15 only when a company or a non-student person resolves. The 15-credit cap covers the agent's own work; sandbox pills count separately, at most 3 per sandbox, and each is cached so a re-click costs nothing.

## Acceptance criteria
1. **Work-email signup**: company identify (free), then a name search at that company; company enrich (cached) and job search (limit 5) follow; the email lands with cited reasons, 10 results and curl + Python within 60 s (target 15-30 s). Total credits at most 8, summed from `X-Credits-Used`.
2. **No empty demo**: every LLM-proposed filter value is snapped with autocomplete (free) before search. If fewer than 10 results, relax the strictest filter and retry (at most 2 retries). The dashboard log shows the snap and the retry.
3. **Sandbox**: the email link opens `/sandbox/{id}` with the 10 results and the exact request body. 2-3 pills each change one filter and fire a live second call in under 3 s; status becomes "Activated".
4. **Sales routing**: high fit gets a Slack card with who, cited reasons, estimated credits per month (formula below) and the freshest signal in the results (most recent job start or `updated_at`). Student: no paid credits beyond identify, no Slack alert, dashboard shows "self-serve nurture, 0 rep hours".
5. **Gates**: a repeat identity and a disposable domain each produce a visible human-queue item with the reason.
6. **Failures**: a Crustdata 429/4xx/5xx, LLM failure or email failure each show in the log and fall back (retry with backoff, cached sample, or the 3-button email). No crash.

## Credits-per-month estimate (Slack card)
Deterministic and shown with its inputs, for example: searches per month = open roles x searches per role; enriches per month = shortlisted profiles x roles; credits = searches x results x per-result cost + enriches x 1. Print the assumptions on the card. Show credits, not dollars: Crustdata's credit-to-dollar rate isn't public.

## Cut list
| Keep | Cut or fake |
|---|---|
| Gates, identify, academic split | Web-search "gmail bridge" (use person identify, then the 3 buttons) |
| recruiting and gtm playbooks done well; academic; student; unknown | investor playbook (stretch) |
| Autocomplete snapping and relax-on-empty | Custom MCP config with 3 prompts (link the standard MCP, `https://mcp.crustdata.com/mcp`) |
| Email with results and curl/Python | Raw vs rounded credit display (just sum `X-Credits-Used`) |
| Sandbox with 2-3 pills; founder watch (real watches stay planned until `WATCH_LIVE=true`) | Error auto-healer |
| Slack card with the credit estimate | Docs code-block personalisation, CRM sync, batch identify |
| Starter SQL and sandbox buttons | Company homepage fetch (tried: over 6 s, and the emails were specific without it), creating real Supabase projects |
| Dashboard: call log, email preview, Slack card, statuses | |

## KPIs to show in the demo
Time to first result per signup; non-empty first result rate on the test matrix; credits per served signup; activations (second call); sales alerts sent vs suppressed; abuse catches.

## Open questions
Decided (5 Nov 2026):
- Team of 4 or more, in four tracks: data, decisions, delivery, surface.
- Stack: FastAPI, Jinja pages with vanilla JS, SSE, SQLite. One long-running process with one worker.
- LLM: Anthropic Python SDK, `claude-opus-5-5` at low effort, set through `LLM_MODEL`.
- Delivery: real Resend email and Slack webhook, always mirrored in the dashboard, with a recipient allowlist.
- Sandbox: runs on our key, because demo signups have no key of their own. The browser sends a pill ID, never a filter or a key.

For Crustdata engineers at 10:30 PM:
- Is `/person/identify` enabled on hackathon keys?
- Exact semantic-search syntax for person search.
- Is preview mode available?
- Can they raise rate limits for the demo?
- Are fractional credits rounded up per call?
