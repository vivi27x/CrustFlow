# Profile agent

Takes a signup's **name and email** and writes **`user.md`**: everything Crustdata and the open web say about that person professionally, with a source on every fact and the time each source was fetched. Another agent reads the file next.

## Run it

```bash
python3 -m venv profile_agent/.venv
profile_agent/.venv/bin/pip install -r profile_agent/requirements.txt
profile_agent/.venv/bin/python -m profile_agent --email ada@example.edu --name "Ada Example"
```

Keys are read from the repo's `.env`:

| Variable | Needed | What for |
|---|---|---|
| `CRUSTDATA_API_KEY` | Yes | Crustdata's REST API and MCP server |
| `ANTHROPIC_API_KEY` | Yes | Claude |
| `ANTHROPIC_WORKSPACE_ID` | Only if the Anthropic key isn't scoped to a workspace | Sent as the `anthropic-workspace-id` header |

Optional settings:

| Flag | `.env` | Default |
|---|---|---|
| `--no-cache` | `PROFILE_CACHE=0` | Use the company cache |
| `--max-usd` | `PROFILE_MAX_USD` | $5 of Claude usage per step |
| | `PROFILE_RESEARCH_MODEL`, `PROFILE_RESEARCH_EFFORT` | `claude-sonnet-5-5`, `low` |
| | `PROFILE_WRITE_MODEL`, `PROFILE_WRITE_EFFORT` | `claude-opus-5-5`, `medium` |
| | `PROFILE_MAX_TURNS` | 40 |

There is no Crustdata credit cap. Every run's spend is reported in `run.json` and in the file's Sources table.

From Python:

```python
import asyncio
from profile_agent.agent import ProfileAgent
from profile_agent.config import settings

run = asyncio.run(ProfileAgent(settings(), "ada@example.edu", "Ada Example").run())
print(run["user_md"])
```

## Input

| Field | Required | Notes |
|---|---|---|
| `email` | Yes | The signup's email. Its domain is the main clue to employer or university. |
| `name` | Recommended | The name given at signup. The Crustdata match needs it. |

## Output

Two files, in `profile_agent/output/<email>/`. That folder is gitignored, because it holds real people's data.

| File | For | Contents |
|---|---|---|
| `user.md` | The next agent | The profile, in the format below |
| `run.json` | Debugging and cost | Timings per step, Claude cost, every Crustdata call (request, credits, cached or not, fetched at), how the person was matched, the researcher's calls and findings |

A new run for the same email overwrites both.

### `user.md` format

YAML frontmatter with fixed keys (an empty string when unknown), then the same headings in the same order every time:

```markdown
---
schema: "user-profile/v1"
email: "ada@example.edu"
name_given: "Ada Example"
name: "Ada Example"
headline: "Associate Professor at Example University"
current_title: "Associate Professor"
current_company: "Example University"
company_domain: "example.edu"
location: "Example City, United States"
linkedin_url: "https://www.linkedin.com/in/ada-example"
github_url: "https://github.com/ada-example"
x_url: "https://x.com/ada_example"
company_x_url: "https://x.com/example_university"
homepage_url: "https://ada.example.edu"
scholar_url: ""
crustdata_person_id: "12345"
crustdata_company_id: "67890"
persona: "Systems researcher and open-source builder"
generated_at: "2026-11-05T22:05:18Z"
---

# Ada Example

Three or four sentences for the next agent.

Matched on: one line saying which profile was used and why it fits this email

## Current role
- Associate Professor at Example University [person_enrich · experience.employment_details.current[0] · 2026-07]
## Company
## Experience
## Education
## Skills and projects
## Online presence
## Recent activity
## Conflicts and caveats
## Checked, nothing found

## Sources
| Source | Request | Result | Credits | Fetched at (UTC) |
|---|---|---|---|---|
| `/person/search` | person_search | ok | 0.99 | 2026-11-05T22:05:21Z |
| `/company/enrich` | company_enrich | ok | 0 | 2026-11-05T21:58:17Z (cache) |
| WebFetch | https://ada.example.edu | ok | 0 | 2026-11-05T22:05:40Z |
```

**Bullets and sources:**
- Each heading has bullets, or `- Nothing found` when empty.
- Every bullet ends with its source in square brackets: a Crustdata tool and field (`[person_enrich · experience.employment_details.current[0] · 2025-12]`) or a URL.

**What code adds after the writer:**
- `generated_at`, when the file was written.
- The **Sources** table: every Crustdata call, free lookup and web call, each with its credits and the time it was fetched. Data from the company cache shows the time it was originally fetched, marked `(cache)`, so you can see how old it is.
- **Scrubbing:** every email except the signup's, and every phone number, is replaced with `[email removed]` or `[number removed]`.

The content rules (describe only this one person, refer to them by name or "they", no contact details) live in the prompts only. The file is saved exactly as written, so an occasional slip can get through.

## How it works

```
name + email
   │
   ├─ 1. Collect (code, no model, about 5 to 10 s)
   │     background:  Crustdata web search + news search (slow, capped at 25 s each)
   │     wave 1:      email lookup, company_identify, person_search
   │     wave 2:      person_enrich, company_enrich*, job_search*, posts by person and employer*,
   │                  GitHub profile tied to the email          (* from the company cache when fresh)
   │     wave 3:      GitHub profiles from person_enrich, and the homepages they or Crustdata name
   │                  → X handles for the person and the employer
   │
   ├─ 2. Research (Sonnet 5.5, Claude Code, about 13 to 21 s)
   │     one round of WebSearch / WebFetch to fill gaps
   │     if code couldn't match the person: one Crustdata search through the MCP server first
   │
   ├─ (wait for the background web searches, then fetch any homepage they found)
   │
   └─ 3. Write (Opus 5.5, one call, no tools, about 35 to 47 s) → user.md
```

### 1. Collect

Code makes every Crustdata call itself, over the REST API, so no model time goes on planning them.

How the person is matched (`collect.py`), in order:
2. `person_search`: the name plus the email's domain as current employer. It counts as a match only when exactly one profile fits.
3. If nobody fits: the name plus the email-domain organisation's name, as current employer and, for an academic email, as current school. Many profiles have no employer domain; CMU's, for example, has none.
4. Otherwise the match is ambiguous or not found, and the researcher decides.

Names match when one set of name words contains the other, in any order. So "Ada Example" matches "Ada (Grace) Example".

### 2. Research

A lighter model (Sonnet 5.5, low effort) does one round of web lookups, all in the same turn. Its targets are the homepage or faculty page, GitHub, OpenAlex or Scholar, news and the employer's site.

When code couldn't match the person, the researcher also runs one Crustdata script through the MCP server. Its prompt contains the exact request shapes, so it never needs `get_schema` or a retry. If it finds the profile, code then fetches that person's half of wave 2.

The researcher writes no text between tool calls, and the session ends the moment it calls `submit_findings`.

   | Tool | Gives |
   |---|---|
   | `person_search` | The profile, by name plus the email's domain as employer or school |
   | `person_enrich` | The full profile, with GitHub (`dev_platform_profiles`) and papers (`research_profiles`) |
   | `company_identify` | The employer's company id (free) |
   | `company_enrich` | The employer: headcount, funding, hiring, news (usually skipped for universities) |
   | `job_search` | The employer's open roles |
   | `social_post_search` | Posts by the person and the employer |
   | `web_search_live` | Crustdata's web search |

**LinkedIn** comes only through Crustdata:
- **Profile:** `person_search` and `person_enrich` give the headline, roles, education, skills and connections.
- **Posts:** `social_post_search` gives posts by the person and by the employer's page.

`linkedin.com` itself is never fetched.

**X (Twitter):**

| What | Where it comes from |
|---|---|
| The person's handle | Crustdata's `social_handles.twitter_identifier`. This is often empty; it was for all five test people. Then GitHub's `twitter_username`. Then X links on the homepage the person's own GitHub or Crustdata profile points to. |
| The employer's handle | Crustdata `company_enrich`, `social_profiles.twitter_url` |
| Recent posts | The researcher's WebSearch limited to `x.com` and `twitter.com`. x.com pages need a login, so they can't be fetched, but search results carry each post's text in the title. |

**How handles are trusted:**
- A handle from Crustdata or GitHub counts as confirmed.
- One found on a homepage is a candidate, because pages also link lab and university accounts. The researcher accepts it only if the X profile's name and bio match.
- With no handle, the researcher may search x.com by name plus employer and accept an account only on a clear match.

No X API is used, so post coverage is whatever search has indexed: recent and popular posts, not a full timeline.

### 3. Write

Opus 5.5 gets every source, trimmed of images, timeseries and long text, plus the researcher's findings. It writes the file in one call with no tools.

### Company cache

`profile_agent/cache/` holds company data, gitignored. It's keyed by Crustdata's numeric company id, never by name.

| Entry | Key | Kept for |
|---|---|---|
| Domain → company | the email domain | 30 days |
| `company_enrich` | company id | 7 days |
| `job_search` | company id | 1 day |
| Employer's posts | company id | 1 day |

**Caveats:**
- **Same-name companies never collide**, because the key is the id. A domain is mapped to an id only when exactly one identified company has that domain as its *primary* domain. Fuzzy or multiple matches aren't cached.
- **Parent and child organisations stay separate.** An email at `cs.ucla.edu` checks `cs.ucla.edu` and `ucla.edu`, and uses whichever has an exact primary-domain match. A department or subsidiary with its own Crustdata id gets its own entry.
- **Domains that move.** If a company changes its primary domain, or a domain is sold, the old mapping lasts up to 30 days. Delete `cache/domain/<domain>.json` to refresh it.
- **Stale data within the window.** A funding round or a new job post can be missing for up to 7 days or 1 day respectively. The Sources table shows the fetch time, so the next agent can tell.
- **Profile ids that differ.** The employer id on a person's profile can differ from the one `company_identify` gives for the domain, and sometimes it's 0. Code prefers the person's own employer id only when that role sits on the email's domain.
- **Errors aren't cached.** Empty results and errors are always fetched again.
- **A changed request is a miss.** An entry is used only when it was fetched with exactly the same request. Adding a field, as when `social_profiles` was added, refetches instead of returning an entry without it.
- **Safe across runs.** Entries are written atomically, so overlapping runs can't leave a half-written file. Two runs may both fetch the same company once; the cost is a duplicate call, not bad data.
- **Turning it off:** `--no-cache` fetches everything fresh. Deleting `profile_agent/cache/` clears it.

### Guardrails

| Where | What |
|---|---|
| Crustdata scripts run by the researcher (`guard.py`) | Blocks contact data, creating or changing watchers, batch jobs, live people/company/job/social endpoints, tools not on the allowlist, and tool names that aren't string literals |
| Every `WebFetch` | Blocks people-search sites, contact brokers and LinkedIn |
| Writing the file | Scrubs every email but the signup's, and every phone number |

The agent never sees the API keys. Crustdata's `feedback` tool, which sends reports to Crustdata, is turned off.

## Files

| File | Job |
|---|---|
| `__main__.py` | Command line |
| `agent.py` | The three steps: research and write prompts, request examples, hooks, the run |
| `collect.py` | Code-run collection: the three waves, person matching, homepage fetch, X handles |
| `crustdata.py` | Crustdata REST calls (credits, fetch time) and the company cache |
| `render.py` | Trims sources for the writer, adds `generated_at` and the Sources table, scrubs contact details |
| `guard.py` | Crustdata script checks and the blocked-site list |
| `lookups.py` | Free email lookups (domain type, Gravatar, GitHub) |
| `config.py` | Settings and paths |

The domain lists come from the repo's `app/data/`.

## Performance

Tested on 5 people at once:

| Case | Time | Collect | Research | Write | Claude $ | Credits |
|---|---|---|---|---|---|---|
| A professor at a US university (matched by code) | 69 s | 8 s | 21 s | 40 s | 0.28 | 7.95 |
| A professor with a shared name (two candidates, researcher picked) | 71 s | 4 s | 22 s | 45 s | 0.33 | 9.87 |
| A professor not in Crustdata | 71 s | 5 s | 18 s + 8 s wait | 41 s | 0.24 | 2.0 |
| A student at an Indian university (matched as a student) | 60 s | 9 s | 13 s + 3 s wait | 35 s | 0.20 | 2.83 |
| An engineer at a startup (matched by employer name) | 71 s | 6 s | 13 s + 5 s wait | 47 s | 0.25 | 5.46 |

The previous single-agent version took 79 to 102 s on the same kind of case. About 60% of the time is now the one writing step.

## Known issues

- **Content rules can slip:** they live in the prompts only, so a file can occasionally mention another person with the name or use a pronoun.
- **No hard identity confirmation:** a unique name-and-domain match, or the researcher's judgement, is trusted. The "Matched on" line says why.
- **Slow web searches:** Crustdata's live web search sometimes takes over 25 s, and is then dropped and recorded as a timeout. A dropped call may still be billed.
- **No credit cap:** the most expensive calls are posts (0.5 a post) and `person_enrich` with GitHub and research add-ons (up to 4). A typical run spends 2 to 10 credits.
