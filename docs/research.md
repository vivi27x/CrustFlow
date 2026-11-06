# Research: the need, the competitors, the evidence

From a research pass on 5 Nov 2026. Anything unverified is flagged.

## Verdict
The need is real, and the specific gap appears unfilled: we found no product that runs the vendor's own product on each signup's inferred use case and returns real results plus runnable code, for every self-serve signup.

## Evidence
- Crustdata's overall activation was about 25%. Onboarding that asks signups their goal raised it to over 50%; MCP adoption among new signups went from 8% (June) to 29% (July). Source: CEO Abhilash Chowdhary, X and LinkedIn posts, Aug 2026.
- About 100,000 self-serve signups, $10M+ ARR, about 40 people. Source: Chris Pisarski, LinkedIn (snippet; not verified against the original post).
- New Crustdata accounts get $20 in free credits and a self-serve API key (crustdata.com/pricing).
- Median free-to-paid conversion is 8% within six months (ChartMogul SaaS Conversion Report, Feb 2026). Trials using product-qualified leads convert about 25% vs a 9% median, but only 24% of PLG companies use them (ProductLed PLG benchmarks).
- Personal emails: 80-95% of signups at some PLG companies; enrichment succeeds about 90% of the time on work emails vs about 25% on personal emails (Syft).
- Multi-account abuse: more than one in six signups at AI companies are linked to it (Stripe research via Fortune, 28 Sep 2026); attempted abuse rose 40% from Jan to Jun 2026 (Stripe).
- Solutions engineers: US base about $123k (ZipRecruiter) to $138k (Indeed); India Rs 9.7 lakh (Payscale) to Rs 18.1 lakh (Levels.fyi, tech-company skew). Median AE:SE ratio about 4:1 (PreSales Collective / Consensus). POCs typically run 2-8 weeks.
- PLG writers recommend generating the user's first output before they configure anything (Userpilot, ProductLed). These are recommendations, not products.
- Avoid: daily.dev's "3-4x / 10x more likely to convert" claims and the docsie time-to-first-call case study. Both are unsourced.

## Competitor landscape
| Category | Examples | What they do for a new signup | Run the product and return results + code? |
|---|---|---|---|
| Enrichment and routing | Default, HubSpot Breeze (Clearbit), Warmly (now HubSpot), UserGems, Unify, Clay | Enrich, score, route to a rep | No |
| Product-led sales / PQL | Pocus (Apollo, Mar 2026), Common Room (Zoom, Jul 2026); Koala shut down 30 Sep 2025 after the Cursor deal | Prioritise accounts for reps | No |
| In-app AI onboarding | Hyper, Skippr, Appcues, Userpilot, Chameleon, Product Fruits | Guide or operate the UI during the user's session | Partly: no inferred use case, no results or code |
| AI sales / solutions engineer | Vivun Hero (formerly Ava), Proofs.io | Prep human sellers; Proofs built PoC apps for SE-chosen deals | Proofs only, SE-triggered; no current public product |
| Personalised demos | Navattic, Storylane, Reprise, Walnut, Saleo | Captured or overlay demos with fake data | No |
| Docs AI | Kapa.ai, Inkeep, Mintlify | Answer questions from the docs | No |

Honest risk: any of these, or Crustdata itself, could add this feature. Our angle: built on the vendor's own data API, spending the product's own credits, budgeted by fit.

## Risks and mitigations
- Creepy personalisation: say why ("Based on HireFast's public job posts..."), use public business signals only, add a "wrong guess?" link.
- Privacy law: India's DPDP Rules were notified on 14 Nov 2025, with core consent and notice duties applying from about 13 May 2027. Disclose enrichment at signup and use it for onboarding only. GDPR and CAN-SPAM were not researched.
- Wrong identification: confidence threshold; ask instead of acting.
- Deliverability: send from an authenticated transactional domain; validate emails at signup.
- Cost: about 5-6 credits per fully served signup vs a $20 grant.

## Sources
- Abhilash Chowdhary on X: https://x.com/TheChowdhary
- Abhilash Chowdhary on LinkedIn: https://www.linkedin.com/in/abhilashchowdhary/
- Chris Pisarski on LinkedIn: https://www.linkedin.com/in/chris-pisarski/
- Crustdata pricing: https://crustdata.com/pricing
- Crustdata credits docs: https://docs.crustdata.com/general/credits
- Crustdata on Deepline: https://deepline.com/gtm-stack/providers/crustdata
- PLG statistics roundup: https://www.shno.co/marketing-statistics/product-led-growth-statistics
- Syft, the Gmail signup problem: https://blog.syftdata.com/p/the-gmail-signup-problem-in-b2b
- Fortune on Stripe abuse research: https://fortune.com/2026/09/28/ai-unit-economics-token-theft-stripe/
- ZipRecruiter SE salary: https://www.ziprecruiter.com/Salaries/Solutions-Engineer-Salary
- Indeed SE salary: https://www.indeed.com/career/solutions-engineer/salaries
- Levels.fyi India: https://www.levels.fyi/t/sales-engineer/locations/india
- Payscale India: https://www.payscale.com/research/IN/Job=Solutions_Engineer/Salary
- Consensus on presales ratios: https://goconsensus.com/blog/how-to-scale-presales-the-first-thing-to-know
- Proofs (The Rundown): https://www.therundown.ai/tools/proofs
- Proofs funding: https://techfundingnews.com/proofs-ai-proof-of-concept/
- Hyper AI onboarding agent: https://gethyper.ai/blog/ai-onboarding-agent
- Skippr: https://www.skippr.ai/
- Vivun Hero launch: https://www.prnewswire.com/news-releases/vivun-launches-hero-the-ai-teammate-built-to-help-sellers-win-302762215.html
- Koala shutdown: https://getkoala.com/blog/a-new-chapter
- Apollo acquires Pocus: https://www.apollo.io/newsroom/announcements/apollo-acquires-pocus-to-advance-its-vision-for-an-ai-native-gtm-operating-system
- Zoom to acquire Common Room: https://investors.zoom.us/news-releases/news-release-details/zoom-acquire-common-room-bringing-buyer-intelligence-its-ai
- HubSpot acquires Warmly: https://www.cmswire.com/digital-experience/hubspot-acquires-warmly-to-boost-ai-sales-agents/
- Userpilot on onboarding: https://userpilot.com/blog/user-onboarding/
- ProductLed on AI onboarding: https://productled.com/blog/ai-onboarding
- India DPDP Rules (PIB): https://www.pib.gov.in/PressReleasePage.aspx?PRID=2190655&reg=48&lang=2
