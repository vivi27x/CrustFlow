# Build log

What went wrong while building CrustFlow, what we tried, what didn't work, what did, and which alternatives turned out better. Entries are added as things happen, newest last.

Each entry: **Problem**, **Tried**, **Worked**, and **Better alternative** where one exists.

## Planning (before any code)

### Repeat identity can't run before a paid call
- **Problem:** the spec lists repeat identity under gates that run before any paid call, but the check needs `crustdata_person_id`, which only comes from the 1-credit `/person/identify`.
- **Worked:** split the gate in two. Repeats of the same normalised email are caught free before identify. The person-ID check runs straight after identify, before anything else paid.

### Matched Gmail signups have no company
- **Problem:** `/person/identify` accepts personal emails, but its response has no employer, so the company context step had nothing to work with.
- **Tried:** the original PRD used web search to bridge from Gmail to an employer. That was already cut: it works poorly and looks invasive.
- **Worked:** `POST /person/enrich` (1 credit) for the current employer, then `/company/identify` by name (free).

### Our API notes disagreed with the live docs
- **Problem:** rereading the live docs turned up conflicts, some inside the docs themselves:
  - Autocomplete is 300/min on the rate-limits page and 45/min on the API reference.
  - Out of credits is 402 in best practices and 403 in the person-search reference.
  - `X-Credits-Used` is absent on 429 responses.
  - Growth keys are `six_months` in enrich but `6m` in company search.
  - The employer is `current.name` in `fields` but `current.company_name` in filters.
  - Job results are under `job_listings`.
  - "Founder" and "CEO" are not valid seniority values and return 400.
- **Worked:** throttle at the lower autocomplete limit, branch on `error.type` rather than status, keep seniority out of the LLM's hands. Corrections are in `docs/crustdata-api.md`.

### Credit rounding may make signups dearer than the headers suggest
- **Problem:** the credits page says balance deduction rounds a fractional cost up to the next whole credit, while `X-Credits-Used` reports the exact decimal. If that applies per call, a 5.45-credit signup really deducts about 7.
- **Status:** not tested yet. It needs a key. `scripts/probe_api.py` checks the balance before and after a fractional call.

### Model choice changed from Sonnet 5.5 to Opus 5.5
- **Problem:** we first planned `claude-sonnet-5-5` for the classifier, assuming it would be faster.
- **Worked:** the current API guidance defaults to `claude-opus-5-5`, so v1 uses it at low effort, set through `LLM_MODEL`. Latency needs measuring on the first real run; swapping back is one env var.

## Build

### No API keys at the start of the build
- **Problem:** no Crustdata, Anthropic, Resend or Slack credentials existed when building began, so nothing could call a real API.
- **Tried:** the plan was static fixtures shaped from the docs. Fixtures can only answer requests we predicted, so they can't test whether a relaxed filter really returns more people, or whether a 400 fires on a bad value.
- **Worked:** an in-process simulator (`app/crustdata/simulator.py`) that evaluates filters over an invented population of 1,400 people, computes credits from the documented pricing, and rejects what the docs say the API rejects. It plugs in as an httpx transport, so the real client code runs unchanged.
- **Better alternative:** the simulator beat fixtures. It caught bugs fixtures would have hidden, such as the empty gtm ladder below.
- **Limit:** it encodes our reading of the docs. It can't tell us the docs or our reading are wrong, so `scripts/probe_api.py` must run against the real API before the demo.

### Two signups for one person could both get through
- **Problem:** the demo lands signups at once. Two emails for one person can both finish identify before either is stored, so a "have we seen this person?" lookup passes both.
- **Worked:** an atomic claim (`INSERT OR IGNORE`, then read the holder) in `db.claim_identity`. Exactly one signup wins. A test runs both concurrently.

### A wrong-guess correction could wipe out a signup's results
- **Problem:** Priya was classified as recruiting, clicked "wrong guess" and chose Sales & GTM, and ended up with no results. Their industry was the fallback market guess, and all three gtm rungs kept that filter, so company search returned nothing.
- **Tried:** nothing else; the test failed on the first run.
- **Worked:** the last gtm rung drops the industry filter. Our industry guess is the weakest input, so it should be the first thing we give up.

### Our gate test was stricter than the feature
- **Problem:** a global kill-switch test expected 0 credits at a 0.5-credit cap and failed, because a 0.15-credit job search correctly fits under 0.5.
- **Worked:** the test now asserts total spend stays under the cap. The code was right.

### "Activated" means two different things in our docs
- **Problem:** `CLAUDE.md` says activated also covers a sales brief being sent, but the spec's acceptance criteria and KPI say a sandbox pill. If a brief set the status, it would flip before the pill click, and the demo's key moment would disappear.
- **Worked:** only a successful pill sets Activated. Briefs are counted separately on the dashboard.

### Tests wrote into the dev database
- **Problem:** the first HTTP tests failed strangely, with 16 signups where we expected 8. The app's startup connected to the real `cache/crustflow.sqlite`, the same file the running dev server was using, so tests and server shared data.
- **Worked:** the test fixture connects to its own temporary database first. `db.connect` reuses an open connection, so the app's startup picks it up.

### Headless Chrome hung on the dashboard
- **Problem:** `google-chrome --headless --virtual-time-budget` produced no screenshot of the dashboard, yet worked on `/healthz`.
- **Cause:** virtual time waits for the page to go idle, and the dashboard's open SSE connection never does.
- **Worked:** Playwright driving the system Chrome (`channel="chrome"`). No browser download, and real clicks.
- **Better alternative:** Playwright beat screenshots. It proved what API tests couldn't: updates arrive with no reload, and a pill inside the embedded sandbox flips the status live. It also caught the favicon 404 as a console error.

### `pkill -f` killed its own shell
- **Problem:** `pkill -f "uvicorn app.main:app"` matched the shell running that command, so the whole step died with exit 144.
- **Worked:** find the process by its port with `ss -ltnp`, then kill that PID.

### Simulated people could have reached a real inbox
- **Problem:** with `CRUSTDATA_MODE=sim` and `EMAIL_LIVE=true`, invented people would be emailed to a real person as "live results".
- **Worked:** the mailer and Slack notifier refuse to send in simulator mode, whatever the other flags say. A test covers it.

### External profile data reached an `href`
- **Problem:** profile links come from outside our system. Jinja escaping stops HTML injection, but a `javascript:` URL survives escaping and would be a live link.
- **Worked:** only `http` and `https` links reach a rendered row. A test covers hostile names, titles and URLs.

### Replay misses stalled the pipeline
- **Problem:** a request with no recording returned a 503, which the client retried with backoff, wasting seconds on something retries can't fix.
- **Worked:** a dedicated `replay_miss` error type fails immediately and lands in the safe path.

### Demo reruns showed 0 credits
- **Problem:** caching identify and enrich is right for live reruns, but a second simulated run showed nearly 0 credits, which would misrepresent the real cost on the video.
- **Worked:** reset clears the response cache in simulator mode only. Live mode keeps it, because live credits are real.

### Long code lines broke the email layout
- **Problem:** in the gtm email, Python snippet lines inside `<pre>` pushed the table past 640px. The API tests all passed; only a screenshot showed it.
- **Worked:** `table-layout: fixed`, and code that wraps with `pre-wrap`. Horizontal scrolling inside email is unreliable anyway.

### One refused field could have failed every search
- **Problem:** we ask for `updated_at` and `start_date` only for the sales freshness line. The docs are unclear on whether search accepts them, and a 400 or field-level 403 would have failed every person search for every signup.
- **Worked:** on a field refusal, retry once without the optional fields, and store the request that worked so the emailed code still matches. It can't loop: required fields aren't removed.

### Pretty-printed Python was unreadable on a phone
- **Problem:** `pprint` lays nested dicts out in deep, uneven columns. It's correct Python, but the code is a core deliverable and it looked broken in the email.
- **Tried:** `json.dumps`. It reads well but writes `true` and `null`, which aren't Python.
- **Worked:** a small formatter (`snippets._literal`). Tests execute the generated code and compare the result to the original body, including quotes, backslashes, non-ASCII text and empty containers.

### Our word-stripper clipped research fields
- **Problem:** with no LLM, "Professor of Computer Science, natural language processing" became the query "Computer Science natural language". The first search for faculty was worse for it.
- **Worked:** split the headline into segments, remove role words, skip places, and take the last segment, since researchers tend to list their department first and their specific field after it.
- **Note:** we only saw this by reading a rendered email. Every test passed before and after.

### Naming a scratch script `email.py`
- **Problem:** a throwaway script called `email.py` shadowed Python's standard `email` module and crashed `urllib` with a circular import.
- **Worked:** renamed it.

## First run against the real APIs
Keys arrived on 5 Nov. We ran the probe, then real signups through a test server with sending turned off. About 27 credits in total, and it found more than the whole simulator had.

| Path, real data | Credits | Time |
|---|---|---|
| Recruiting (identify disabled) | 4.45 | 3.2 s to research, 1.4 s to search |
| GTM (identify disabled) | 6.1 | 3.5 s to research, 1.5 s to search |
| Sandbox pill | 0.23 per row returned | 0.3-0.4 s |
| Every gate | 0 | Instant |

### `/person/identify` is disabled on our key
- **Problem:** it returns 403. This was the top risk in the plan. Our fallback, a name and domain search, only works when the email contains a name.
- **Tried:** `/person/enrich`, in case it took an email. The docs and `/account/endpoints` show it takes profile URLs only.
- **Also tried:** the 15,000-credit hackathon key. Same 403, and enrich rejects `business_emails` with a 400.
- **Worked:** nothing on our side can replace it, because it's "enabled per account on request". Until then the safe path holds: every unresolved signup gets the three buttons at 0 credits. Personal emails can't be identified at all, so the student split and repeat-identity catch wait on Crustdata.
- **Better alternative:** read `/account/endpoints` first. It's free and answers most of what the probe tests by spending credits.

### Docs-shaped field names were wrong
- **Problem:** person search refused `updated_at`, and company search refused `basic_info.industries`. Neither failed in the simulator, because the simulator was built from the same misreading.
- **Note:** the self-heal would have dropped `updated_at` and carried on, so that one would have quietly lost the freshness signal. Company search would have failed outright.
- **Worked:** the error names the right field. We fixed both, then changed the simulator to refuse the wrong names, so the tests can't drift back.

### Company search costs far more than we assumed
- **Problem:** the plan used 0.03 per row. Real cost is up to 0.63 with `headcount` and `taxonomy` filtered and received, which pushed GTM past the 8-credit cap.
- **Worked:** measured each case and stopped receiving premium fields we only displayed. `basic_info.employee_count_range` is free, so the email table now shows size and country. GTM came in at 6.1.

### The supporting step starved the main one
- **Problem:** at real prices, a Gmail signup spent 3.79 credits relaxing to get 5 companies when it already had 4. Nothing was left for the people search, so the signup paid and got no results.
- **Worked:** relaxing stops at 3 companies, a rung that can't beat what we have is skipped, and credits are reserved for the people search.
- **What didn't work:** a full reserve. It broke the wrong-guess flow, which arrives with little budget, and lost the signup their results. A test caught it. The reserve is now capped at half of what's left.

### Snapping took the first suggestion
- **Problem:** "Bangalore" would have become "Bangalore Rural". "Security Engineer, Privy" became "Security Engineer - Penetration Tester", which left a real sandbox pill with 0 rows.
- **Worked:** an exact match wins, then suggestions with every word typed, then the most common. Job-post padding is stripped first.

### A city was treated as a country
- **Problem:** a real Greenhouse job had the location "Bengaluru", which we read as the country, so every rung filtered on a country that doesn't exist and the signup got nothing. Enrich's `IND` would have failed the same way.
- **Worked:** a lone place is a city, ISO codes map to names, and a country that autocomplete doesn't recognise is dropped rather than sent. Other filters still keep the typed value, but countries are a closed list.

### A citation named a call that had failed
- **Problem:** people found by the fallback were cited to `person/identify`, which had returned 403. That's a false source on our most important rule.
- **Worked:** the citation now uses the call that returned the fact, and a test covers it.

### Simulated cache entries could reach a live run
- **Problem:** cache keys were the endpoint and body only. Switching one database from sim to live could reuse a simulated company, and simulated data must never reach a real inbox.
- **Worked:** the cache key now includes whether the answer came from the simulator.

### Other setup problems
- **Anthropic key:** rejected with 400, "not scoped to a workspace". The classifier fell back to rules and asked the signup, as designed. We added an optional `ANTHROPIC_WORKSPACE_ID`, which sends the header the error asks for, and that fixed it.
- **Resend sender:** `Name address` without angle brackets gets a 422. `Name <address>` was accepted by Resend's test address.
- **`scholar-articles`:** returned 503 on every try, while plain web search worked. The academic playbook depends on it.
- **Our test scripts:** two false alarms came from reading the wrong keys, because result rows are stored as `a/b/c` and tokens aren't columns. Neither was an app bug.

### First real inference: right answer, weak query
With Claude connected, we ran two real signups on company evidence alone, because `/person/identify` is off.

| Signup | Decision | Credits | Time |
|---|---|---|---|
| ATS vendor | gtm, confidence 0.6, correct and cited | 5.6 | 8.7 s |
| Automation software | unknown, confidence 0.3, so it asked | 2.15 | 4.9 s |

- **Worked:** the citations held up, and Claude declined to guess when the evidence was thin. Its calls took 3-4 s.
- **Problem:** Claude's market, "Talent acquisition / HR", isn't a canonical industry, so the search relaxed to "any industry". The inference was right, but the 10 people were unrelated to it. The email only says the search was loosened, not that the industry was dropped.
- **Also seen:** Claude copied "PA" from a job location as the country. Snapping dropped it, as designed.
- **Not fixed yet:** a likely fix is giving the model a fixed list of industries, so it can't propose a value that won't snap.

## Working without an email lookup
Crustdata declined to enable `/person/identify`, so an email can no longer tell us who someone is. The team changed the product: ask for a name, build the profile from the endpoints we have, and give Claude a description of the vendor.

### A name search missed a real person
- **Problem:** a real signup at a small company got no match, though a web search found them at once. We searched their full name as a phrase, and the address used a different surname from the profile.
- **Worked:** search every part of the name, then any part, and accept a partial match only if one person at the company fits. That case costs 0.13 credits, and a tie asks.
- **Better alternative to a profile-link field:** we nearly added one. For work emails the name and company are enough, and the field only helped personal emails.

### The first persona run read well but wasn't true
We ran real signups through Claude and read the emails. Every test passed, and four things were still wrong.
- **An opening claimed work that never happened:** "We've set WisprFlow up for you." The prompt said to say what we'd done, and for that vendor we'd done nothing. It now has separate wording for vendors with no search, and openings can't promise results, because they're written before the search runs.
- **An ATS vendor got "10 matches: Student Support Associate":** jobs under its domain include its customers' postings. Our persona wording also said "a recruiting product", which pulled it into the wrong segment. Claude noticed the jobs looked external on another run, so the prompt now tells it to skip such rows.
- **A student guess skipped the confidence bar:** the student branch ran before the check. An intern was marked a student at 55%, and 3 credits were spent. The bar now applies to every segment.
- **Note:** none of these showed in tests, because the simulator can't write misleading prose. Reading real output is part of testing here.

### GTM results didn't match the inference
- **Tried:** a fixed industry list, so Claude can't propose a market that won't match. All 39 values were checked against autocomplete. Results improved, but were still mostly CEOs.
- **Why:** we looked for "Head of Talent Acquisition" inside five companies, which found 0 or 1 people, then fell back to whoever was senior there.
- **Worked:** search the buyer's title across the market, filtered by the employer's industry and size. It's one search instead of two, and it cost 4.45 credits rather than 6.1.
- **What didn't work:** falling back from a rare title to the company route. "Studio Owner" found 2 people, and the fallback sent a shipping specialist labelled as a buyer. Senior decision-makers in the same market is the honest next step.

### What we kept from the old design
- **Identify is gone, not hidden behind a flag.** The simulator returns 403 for it, as the real key does, so tests can't pass on a path that can't run.
- **The ReAct loop has a clean join point.** `find` and `deepen` have fixed signatures, and a crash, hang or missing loop falls back to the basic builder. That let two people work without waiting on each other.

## The report became the profile
A teammate's ReAct loop writes a cited markdown report. We'd built a contract, `find` and `deepen` returning Python objects, before seeing that output. When a sample arrived, nothing in the app could read it.

### We changed our contract, not their output
- **Tried first:** a throwaway harness feeding the report through the old join point. Claude used it, but its identity proof, namesakes and caveats had no natural home in those objects.
- **Worked:** the loop returns its report from one function, and `profile_doc.py` reads it. Teammates keep working, and a person can open and check the same file the agent used.
- **Kept from the old design:** the cheap name search runs first. A repeat or a clear student stops for about 0.13 credits, not 7.42.

### More evidence didn't mean more confidence
- **Problem:** with 38 rows of research, Claude reached 55% and asked. The basic builder, with 13 rows, reached 60%. That's one run each, but it pointed at the real issue: selling and recruiting don't describe a staff engineer, however much we know about them.
- **Worked:** a builder segment. The same report now reaches 85% on 9 of 9 runs.
- **Note:** for a data API, engineers may be the main audience. Three of our first test signups stalled for this reason before we saw it.

### Limits written for the old design
- **Problem:** the report took about 3 minutes and 7.42 credits, against 25 s and 8 credits. With its credits counted, the budget ran out and company research was skipped without any error.
- **Worked:** 180 s for a report, 15 credits a signup, and a global cap of 2,000, because 200 would have stopped the app after about 16 signups. The credit reconciler charges anything the loop spent outside our client, so totals stay honest.

### Research must not be quoted back at people
- **Problem:** email citations print the cited fact, so a line about commits authored with someone's address could land in their inbox.
- **Worked:** Claude only sees work facts. Namesakes and identity proof stay in the dashboard, GitHub trails are dropped, and contact details are masked at parse time. Our first phone pattern also matched dates, so it now needs a plus sign or ten straight digits.

### Reading the email again
All of these passed every test. We found them by reading what a real person would receive.
- **Reasons stopped mid-sentence.** A helper for short filter values clipped them at 80 characters, in every real email since v1. Our own log output was truncated too, which hid it.
- **The opening overclaimed.** It said "companies building AI search products", but the search filtered on software, India and size. Rewording the prompt helped only partly: the next version said "the market you serve".
- **Worked:** the persona now says these are a sample to try the API on, the email states what was searched, word for word from the request, and the subject makes no claim about their market. The result reads better as well: real records as test data for a vector database.
- **Citations recited their company back to them,** with long URLs. They're now short.

## Connecting the agent and the email
The profile agent and new email designs were merged separately, and neither was connected to the pipeline. The team also settled what the product is: a service any company can use, with Claude combining what the company offers and who signed up.

### The real report didn't match the sample
- **Problem:** our parser was built from an earlier sample, and would have rejected every real report. The agent writes `name`, has no `status` or `confidence`, and cites plain URLs.
- **Worked:** running the agent once before finishing the parser. The README described the format, but only a real file showed what mattered.
- **Note:** the agent can't confirm identity, so we now decide that ourselves from the email's domain and our own search.

### The agent's credit figures were wrong
- **Problem:** the report's table said 2.3 credits, while the balance fell by 9.83. Paid calls were listed as "free, refused or empty".
- **What we nearly did:** the plan turned off the balance check, because it's account-wide and blocks the server briefly. That would have left the credit cap reading almost nothing.
- **Worked:** keep the balance, run one agent at a time so it stays correct, and charge the signup the run's real total.

### A finished report was thrown away
- **Problem:** the agent took 92 s on one run and 184 s on the next. It researched for about a minute, then spent two minutes writing. It saved at 177 s, hit our 180 s limit while closing down, and we discarded 7 credits of finished work.
- **Worked:** if the report is saved when time runs out, we keep it.

### Three signups at once lost all three reports
- **Problem:** only one agent runs at a time, and the wait in the queue counted against the time limit. Three signups in 80 seconds all timed out, about 14 credits were wasted, and they fell back to the basic profile. Nothing on the dashboard said so.
- **Worse:** the fallback took the first "current role", which on one real profile was a council seat, and that reached the sales brief. Without the report, the same person under two emails also wasn't caught.
- **Worked:** the clock starts when an agent's turn starts, and the limit is now 240 s, because real runs took 95 to 185 s. The queue is capped, past which we fall back straight away rather than wait. The fallback now picks the role at the email's company.
- **Better:** every fallback is now flagged with its reason in the log, signup list, dashboard, KPIs and sales brief. It stays out of the signup's own email.

### A company's information has to be facts
- **Worked:** Supabase's services come from its own pages, and every link was checked. Links come from that file, so Claude can't invent one. A sample written from memory was removed.

### ngrok wouldn't connect from campus Wi-Fi
- **Problem:** `ERR_NGROK_8000`, "all resolvers failed", though the machine reaches ngrok's server. Pointing it at the machine's DNS servers didn't help.
- **Worked around:** the main email's links go to supabase.com, so the test didn't depend on the tunnel.

## From a reading list to a working first run
Reading the real runs side by side showed the problem. The research found a lot, but the email ended in three documentation links, so a signup still had a schema to design before anything worked. We set a new goal: the shortest time to a first working result.

### The agent was the wrong default
- **Problem:** every signup waited 92-185 s for the agent, though the first cheap name search had usually found them already.
- **Worked:** a clear match now skips the agent and runs person enrich, company enrich and job search together. Real signups took 15.5-16.1 s at about 3.2 credits, and about 10.5 s when the lookups were cached.
- **Didn't help:** the proposal to give the agent 20 s and two turns on misses. Real runs need about 60 s of research alone, so it would always fail. Misses keep the 240 s limit.
- **Trade-off:** Claude sees fewer web facts on the fast path. The emails we compared stayed specific, because the company description and person enrich carry most of what it needed.

### The homepage fetch made things worse
We added a best-effort fetch of the company's homepage, because the agent's product detail came from the web. It took 5.3 s alone and hit the 6 s limit on both real signups, adding about 5 s and returning nothing. We cut it. It was the first item on the cut list for this work.

### Code the signup can run
- **Rejected:** Claude writing SQL. It could fail in front of the signup, and text from a public profile could reach a statement.
- **Worked:** Claude fills a spec, and `app/pipeline/starter.py` writes the SQL. A spec we can't make safe drops the code, and the email still goes.
- **Sandbox:** PGlite runs real Postgres with pgvector in the browser, so nothing generated runs on our server. We tested that before building on it. `pgmq` and `pg_cron` don't exist there, so those lines are shown as needing a real project.
- **A small change from the plan:** row-level security goes on every starter, not only for auth picks. A public table without it is exposed.
- **Hardest case:** a staff engineer at a vector database company. The old email pitched them pgvector, their own product category. A prompt rule fixed it: they now get a table for the systems around their product.

### Reading the page again
Tests passed while these were wrong. We found them by reading real output and running drills in Chrome:

- `.pills { display: grid }` beat the `hidden` attribute, so the buttons showed while Postgres was loading and after it failed.
- The page said nothing reaches our server, but a button click posts a note to mark Activated.
- The button read "Run Acme LLC.'s starter", until we trimmed legal suffixes.
- Dates displayed as raw JavaScript strings, and "View as another user" had no way back.

### Making the code readable
- **Problem:** the SQL was one colour, and 6 to 9 lines per starter wrapped mid-statement. The longest was 131 characters where about 77 fit. A sample owner ID on every row was much of that.
- **Didn't work:** sideways scrolling isn't reliable, because Gmail changes `white-space: pre` to `pre-wrap`. So the lines had to get shorter.
- **Worked:** our compiler now lays the SQL out in 72 characters or fewer, and breaks long rows between values. Pygments writes the colours inline, and the dark background sits on a table cell, because Outlook renders through Word and ignores padding on a `pre`.
- **Trade-off:** owner IDs left the emailed SQL, so sample rows have no owner on a real project. The sandbox assigns two sample owners after running that SQL unchanged, and says so.
- **Not tested:** Outlook itself. Phones still wrap some lines, cleanly, with no sideways scrolling.

### Designing the email
We listed what every onboarding email does, then pushed four ideas away from it: fuse the features with the code, deliver the work as a pull request, strip it down to a note from an engineer, and put the result first. We built all four as working emails on one invented company and chose the first, a blueprint where each pick sits on the lines it adds. Building it for real showed the preview had a flaw: the vector section came after the table that needs the extension, so running it top to bottom would fail. The sections now follow the order the SQL runs in, and a test holds that.

### A merge that broke the agent without breaking a test
- **Problem:** merging agent v2 left `profile_agent/agent.py` with a syntax error. A conflict kept the old top half of the file and lost the new prompts, and all our tests passed, because they replace the agent with a stub. Every signup the name check missed would have fallen back, flagged, to the basic builder.
- **Worked:** restoring the author's file, then a real run through the app.
- **Added:** a test that imports the real agent package, which would have caught this.
- **Also found:** v2 wrote people's X posts into Recent activity, which Claude reads. Posts are now filtered out with the GitHub trail, and the refused `/person/identify` call is gone.

### A bug that looked like a data gap
One signup seemed to be missed by our name check. It wasn't: the agent's report left the fields blank, and the blanks overwrote what our search had found, which dropped the person to asking. Blanks now keep what we knew, when both found the same person.

### What we refused to print
The proposal included "saved about 45 minutes" and a measured-looking time to Hello World. We had no measurement behind them, so the sandbox shows only the real time it took. The email says it runs in the browser with no login.

## What this build still hasn't proved
Proved on real data: the request and response shapes for company identify, enrich, job search, person search, company search and autocomplete; both query playbooks; a sandbox pill flipping a signup to Activated; a send accepted by Resend; and the repeat-identity catch across two addresses for one real person, which cost 7 credits because the second went through the agent.

Still open:

- The starter SQL on a real Supabase project. We ran it in PGlite only, so the `auth.uid()` default, policies, publication, `pgmq` and `pg_cron` lines are from Supabase's documented patterns, not a test.
- How the agent behaves across many people: real timing, failure rate and how often it settles on a namesake. We've run it on a handful.
- How consistent Claude is. It changed its mind on one company before the persona wording was fixed, and we've only rerun that case twice since.
- The academic playbook on real data, because `scholar-articles` is returning 503.
- Delivery to a real inbox, and real Slack posts.
- The Docker image. Docker isn't installed on the build machine, so it has never been built.

## Watching founders

### Crustdata already has watchers, so we didn't build polling of enrich
- **Problem:** we wanted to hear when a high-value signup's company raises, hires or makes news. Re-enriching every founder on a schedule would cost 2 to 3 credits each time, changed or not.
- **Worked:** entity watches (`POST /watch/person`, `/watch/company`). Creating one is free, the first run is a free baseline, and after that it's 5 credits per changed subject at the 30-day freshness. A `track` with `or` groups covers several fields in one watch, and `keys: ["article_url"]` stops a reposted article from billing twice.
- **Tried:** webhooks. We have no public URL (ngrok fails on campus), so watches are pull-only (`notifications: []`) and we read `GET /watch/{dataset}/{id}/runs` and each run's `/summary`. Both are free.

### A watch must never be started on a guess
- **Problem:** a wrong-person match would mean emailing someone about a stranger's funding round, and paying for it.
- **Worked:** rules only, at 0.85 match confidence (above a name-only match), with a profile URL. "Founder's Office", "Founding Engineer", "Product Owner" and "Advisor to founders" don't count. Late-stage, public and very large companies aren't startups.

### The facts in a signal email come from code, not Claude
- **Problem:** "Congrats on your Series B" is exactly the claim a model might invent.
- **Worked:** code turns Crustdata's diff into facts (S1, S2...) and Claude can only cite them. An opening that cites no S row is dropped, and so is the email. The prompt forbids saying we watch or track anyone.
