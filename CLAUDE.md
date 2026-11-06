# CrustFlow

A service any company can use to answer its signups. When someone signs up with a name and email, CrustFlow researches them, reads what the company offers, and has Claude write a personal, cited email saying what fits them. Sales only hears about high-fit accounts. Crustdata is the data behind the research. The demo mimics Supabase; Supabase isn't involved.

Built at ContextCon 04 (IIT Kanpur; Crustdata x Y Combinator), theme "Services, Replaced". The service replaced: the solutions engineer who builds a custom proof of concept for each promising account.

## Where we left off
- Building v1. `docs/spec.md` is the source of truth for the MVP; `docs/build-log.md` records problems, what worked and what didn't, as they happen. Add to it whenever something fails, surprises us or changes the design.
- Decided: team of up to 4; FastAPI with Jinja, vanilla JS, SSE and SQLite; Anthropic Python SDK with `claude-opus-5-5` (env `LLM_MODEL`); real Resend email and Slack webhook, always mirrored in the dashboard, with a recipient allowlist; sandbox runs on our key.
- Crustdata refused `/person/identify`, so signups give a name and we match it.
- The profile agent (`profile_agent/`) and the pipeline are connected: `PROFILE_BUILDER=react` runs it through `app/pipeline/react_profiles.py`.
- A company is a file in `app/personas/`: its services and who signs up. `VENDOR_PERSONA=supabase` is the demo. Crustdata's persona still runs a real first search, but it isn't the demo.
- The goal is now the time to a first working result. A clear name match skips the agent (about 15 s instead of 2-3 minutes), and the Supabase email carries a starter: SQL our code compiles from Claude's spec, running in real Postgres in the signup's browser at `/sandbox/{token}`.
- Test on real profiles with `EMAIL_REDIRECT_TO`, so nobody real is emailed.
- Still needed: a Slack webhook, and a public URL. ngrok fails on the campus network with `ERR_NGROK_8000`.

## Running it
- App: `uv run uvicorn app.main:app --port 8000`. With no `.env` it runs on the simulator, so no keys are needed.
- Demo as Supabase: `PROFILE_BUILDER=react VENDOR_PERSONA=supabase EMAIL_REDIRECT_TO=<test inbox> uv run uvicorn app.main:app --port 8000`.
- Tests: `uv run pytest`. Test matrix against a running server: `uv run python scripts/run_matrix.py --reset`.
- Real API check, once a key exists: `uv run python scripts/probe_api.py --yes --email <work email>`.

## Hackathon constraints
- Questions for their engineers are in `docs/spec.md` under "Open questions".
- From the Builder Handbook (the PDF is gitignored):
  - **Commit history:** coding starts at 11:00 PM on 5 Nov, in a new repo whose commit history starts then. The working repo, `crustdata_v1`, has a first commit at 9:59 PM, so it can't be submitted as is. `CrustFlow` is a fresh copy of that code, made on 6 Nov at about 6 AM. That doesn't settle it: the code still began before 11:00 PM. Confirm the way forward with an organiser, and keep `crustdata_v1` so the real history can be shown. Never backdate or rewrite history.
  - **Agents stay in the room:** during the event, agents talk only to our team and the organisers, never to real people outside. Keep `EMAIL_ALLOWLIST` explicit and never use `*`.
  - **Team:** up to 4 people.
  - **Submit by 7:00 AM:** a public repo, the README, one two-minute video link that plays in a private window, a diagram link open to anyone, and every member's details exactly as on the YC registration. Later submissions replace earlier ones.
  - **Theme:** closest is 9, Revenue: "Answer and route every inbound lead within a minute, without inventing a claim."
- Judges score as the customer who would buy the service. Design every feature against these (ties go to the first, then the second):
  1. **End-to-end execution (25):** a real request goes in and finished work comes out. It asks a person only when the answer changes the result.
  2. **Facts and verification (20):** every fact is right for the day it's used, with reasons and sources. One person or company under two names stays one record. When it can't check a fact, it says so instead of guessing.
  3. **Robustness and edge cases (20):** messy input, missing fields, tools that time out or return nonsense. After a crash or retry it continues where it stopped and never sends or pays twice.
  4. **Customer value and idea (20):** a job businesses pay for today, with an approach that isn't obvious.
  5. **Demo and understanding (15):** the approaches weighed, why this one, what worked and didn't, the hardest case, and what comes next.

## The pipeline
1. **Intake**: signup `{email, name}`, from the form or a demo button. Each signup has a vendor persona (`persona_id`, default `VENDOR_PERSONA`).
2. **Free gates**: malformed email; disposable domain; `+alias` normalisation; repeat inbox; the persona's `own_domains` go to an internal branch; its `competitor_domains` go to the human queue.
3. **`find`** (`app/pipeline/profiles.py`, 1 credit at most): `POST /company/identify` (free), then `/person/search` for the name at the email's company. Every part of the name, then any part. Accept a partial match only if one person fits; a tie is ambiguous. A personal email needs a typed full name that exactly one person has. `/person/identify` is refused on our keys, so it's never called.
4. **Gates that need an identity**: repeat `crustdata_person_id` goes to a human; a student headline gets the zero-credit guide. Neither triggers the loop. They run again on whatever the report reveals. Never treat every `.ac.in` address as a student (some judges are IIT Kanpur professors).
5. **Fast path, or the report**: a match at 0.8 or above skips the agent and runs person enrich, company enrich and job search together (`profiles.deepen`), unless a saved report exists. This isn't a fallback and isn't flagged. With `PROFILE_BUILDER=react`, everyone else gets the loop's `user-profile/v1` report (`app/pipeline/profile_doc.py`), which fills the person and gives Claude its evidence. Limit 240 s, from when its turn starts; one agent runs at a time. Reports are cached by email and persona. Any failure falls back to `profiles.deepen`, and is flagged on the signup, the dashboard and the sales brief, with the reason. Company research still runs, because routing needs headcount and open roles as numbers. `FAST_PATH=false` runs the agent on everyone.
6. **Decide**: Claude reads the persona and the evidence, and picks one of that persona's segments or `unknown`, with cited reasons and a short opening. Under 0.6 confidence it asks. The model never writes free-form API queries.
7. **Query**, for segments with a playbook: template plus LLM-proposed values, each snapped with autocomplete (free), limit 10, minimal `fields`. Under 10 results, relax. Never send an empty result. GTM searches the buyer's title across the market first. Builders get a sample of real companies to try the API on. Segments without a playbook skip this and get a tailored message.
8. **Deliver**: for a company with services, Claude picks up to three, each with a cited reason, and links come from the company's list, never from Claude (`t7_feature_match.html`). If the persona has `starter: true`, Claude also fills a spec (table, typed columns, invented rows) and `app/pipeline/starter.py` writes the SQL, or drops the starter if anything can't be made safe; the email still goes. The email is a blueprint (`ui.blueprint`): each pick sits on the SQL lines it adds, in the order the SQL runs. `/sandbox/{token}` runs that same SQL in PGlite in the browser, and nothing generated runs on our server. For Crustdata: email with Claude's opening, cited reasons, a "wrong guess?" link, 10 results (name, title, company, profile link only) and copy-paste curl and Python using the exact filters; plus `/sandbox/{id}` with 2-3 filter pills that fire the second call.
9. **Route**: a segment with `sales_alert` at a big enough company gets a Slack card (who, how they were matched, why with citations, credit estimate where it applies, freshest signal). Otherwise no sales alert.
10. **Dashboard**: live log of every call (endpoint, latency, `X-Credits-Used`), email preview, sandbox, Slack card, and each signup's status.
11. **Founder watch** (`app/pipeline/founders.py`, `watcher.py`, `signals.py`), after a signup finishes: a founder or CEO of a startup, matched at 0.85 or better, gets a pull-only Crustdata entity watch on their profile (new role, headline) and their company (funding, investors, acquisitions, news). Rules decide who; never a guess. We poll run history (no public URL for a webhook). Each change becomes a cited fact written by code, is added to their `user.md` under Recent activity, and Claude judges it: strong, relevant and cited to the change, then one email (`t8_signal.html`), at most one per 7 days, with a stop link. Events are stored by notification uid and handled in saved steps, so a re-read or a crash never acts or emails twice. Without Claude, the rules only re-offer services already matched to them. On the real API watches are only planned until `WATCH_LIVE=true` (5 credits per change).

`app/pipeline/react_profiles.py` connects the agent, one run at a time. Agent v2 runs its Crustdata calls in code and counts them from `X-Credits-Used`, in about 80 s and 7-10 credits. It has no credit cap of its own, so `max_credits` is ignored, and it doesn't use our throttled client. The agent can't confirm identity, so `profile_doc.identity` decides from what we can check. Try a saved report without running the agent: `uv run python scripts/try_report.py <file>`. Reports hold real people's data, so never commit them.

Claude sees a report's work facts only. Namesakes and identity proof stay in the dashboard, GitHub trails and people's own posts (X, social post search) stay out of emails, and contact details are masked, because email citations print the cited fact.

"Activated" means the signup made a successful second call: a sandbox pill, or their own search in a starter sandbox. Opening the page doesn't count, because mail scanners open links. Sales briefs are counted separately.

## Crustdata rules
- Base URL `https://api.crustdata.com`. Every request: `Authorization: Bearer $CRUSTDATA_API_KEY`, `x-api-version: 2025-11-01`, `content-type: application/json`.
- Indexed endpoints only. Never call the professional-network live endpoints (`/person|company|job/professional_network/.../live`) or `enrich_realtime`: enterprise-only and slow. Web search and fetch (`/web/search/live`, `/web/enrich/live`) are fine.
- Always pass `fields`; without it enrich returns basic info only. In person search, `experience`, `education` and `skills` each add 0.1 credits per result to filter on and 0.1 to receive (on top of 0.03), so request the minimum.
- Log `X-Credits-Used` from every response. Per-signup credit caps: student 1, unknown 1, standard 15 (a fast-path signup costs about 3.2; one with a report and a search costs about 12.85). Stop and fall back when a cap would be exceeded.
- Rate limits (requests/min): company enrich 15; person search, identify, job search 30; web search and fetch 10; autocomplete 300 (confirmed by `/account/endpoints`). Route every call through one throttled client with exponential backoff and jitter; honour `Retry-After`.
- Cache identify and company-enrich results by domain so demo reruns cost nothing.
- Empty results cost 0 credits. One malformed email in a sync identify call rejects the whole request, so validate first.
- Verified on our key, including the 15,000-credit hackathon key: `/person/identify` is disabled (403) and missing from the handbook's API list, so don't design around it being enabled; fractional credits are not rounded up per call, and company search charges for premium groups, so request only the free fields. `GET /account/endpoints` lists every field, limit and price for free. Person-search semantic syntax is still untested and unused.
- Full notes: `docs/crustdata-api.md`. Crustdata MCP server for this project: `claude mcp add --transport http --scope project crustdata https://install.crustdata.com/mcp` (writes `.mcp.json`), then authenticate with `/mcp`.

## Output rules
- Every inference shown to a signup or to sales cites its evidence: record, field and date (e.g. "3 open recruiter roles, job/search, posted Oct 2026").
- Lead with value. Never recite the signup's profile back to them.
- Result emails contain names, titles, companies and profile links only, never anyone's contact details.
- Below a confidence threshold, ask (the persona's buttons) instead of guessing.
- An opening is written before the search runs, and searches only filter on title, industry, country and size. So it never promises specifics or says what the results do, and a vendor with no search never claims to have done anything.
- A starter's sample rows are invented. No real people, customers or contact details, and the sandbox says it's Postgres in the browser, not a Supabase project. Never print a time saved that we didn't measure.
- If a signup's company builds what a service does, don't pitch it as a replacement. Pick what surrounds their product.
- Read real emails before trusting a change. Tests can't catch a claim that reads well and isn't true.
- Say how each person was matched. On a partial or name-only match, don't state their title, employer or history.
- Every external failure (Crustdata 4xx/5xx/429, LLM error, email failure) shows in the dashboard log and degrades to a safe path. Nothing crashes mid-demo.

## Test matrix (signups the agent has never seen)
Teammate work email and name (high fit); the same teammate under a second work address (repeat-identity catch); student Gmail with a name; `iitk.ac.in` faculty; `crustdata.com`; disposable domain; malformed email; unknown or brand-new domain.

## Conventions
- Secrets in `.env` (template: `.env.example`). Never commit keys.
- Fixed playbooks and templates beat free-form generation.
- Keep a deterministic replay of one full run (cached responses) for the demo video in case the venue Wi-Fi fails.
- Last hour: README with architecture, setup and demo link. Record the video by about 6:15 AM.

## Docs
- `docs/spec.md`: corrected MVP spec, acceptance criteria, cut list, open questions. Start here.
- `docs/crustdata-api.md`: endpoints, costs, limits, fields, gotchas, and our verified findings.
- `docs/build-log.md`: problems faced, what didn't work, what worked, and better alternatives.
- `docs/research.md`: evidence for the problem, competitor landscape, sources.
- Private and gitignored: `CLAUDE.local.md` (strategy, judges, pitch) and `notes/` (idea history, original PRD and its review, prep checklist).
