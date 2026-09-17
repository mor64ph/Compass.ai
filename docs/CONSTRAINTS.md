# Architecture constraints

These are not style preferences. They come from PRD Sections 4, 5 and 8, and
`tests/test_constraints.py` enforces the mechanical parts of them on every test
run — so a violation fails CI rather than getting discovered after launch.

## 1. No scraping, no session automation

Nothing in this codebase may log into, crawl, scrape, or automate actions
against **LinkedIn, Naukri, Indeed, Foundit, Wellfound, Glassdoor, Fishbowl, or
Reddit**, under any framing.

Concretely, banned from the dependency tree and the source:

- `playwright`, `selenium`, `puppeteer`, `undetected_chromedriver`, `seleniumwire`
- `scrapy`, `mechanize`, `mechanicalsoup`, `requests_html`
- Any HTTP client call whose URL targets one of the platforms above

The enforcing test greps `app/` for both the library names and the platform
hostnames. If you have a legitimate reason to name a platform in source — a
comment like this one, or a list of ATS sender domains — it belongs in a
docstring or a documented allowlist, not in a URL.

**Why the line sits here** (PRD §5.1), in descending order of how much it would
actually cost you:

1. **It backfires.** Recruiters are already filtering out bot-shaped, mass-sent
   applications and cold outreach. That fatigue is the problem Compass exists to
   route around; adding to it makes the tool actively counterproductive.
2. **Third-party personal data.** Harvesting employee profile IDs to target them
   builds a database of identifiable people who never opted in. Once that exists,
   India's DPDP Act — and GDPR, if any EU-based person is ever in scope — applies
   to you as the operator, regardless of how manual the messaging step is.
3. **Contract violation.** Naukri's T&Cs prohibit automated crawling explicitly
   (clause 17) and it publishes no API. LinkedIn's User Agreement §8.2 bars bots,
   scrapers, and automated methods for accessing the service or adding contacts,
   and it is actively enforced. Not a crime; still a contract, and the basis for
   account bans and cease-and-desist letters at any scale resembling a product.

### What replaces each capability

| Wanted | Built instead |
|---|---|
| Job postings from those platforms | **Built:** ATS job-board APIs — see §1.1 — plus "paste the JD" for anything you found yourself |
| Referral leads | You supply the contact; Compass researches the company and drafts a personalised message for *you* to send |
| Salary data | Public sources with real datasets, cited |
| Interview questions | LLM-generated, grounded in cited web search; Codeforces' official public API where competitive programming genuinely applies |
| Application tracking | Gmail + Calendar under your own OAuth — your data, your consent, no third party |

### 1.1 The line between an ATS feed and a job board

`app/services/discovery/` fetches postings. That makes it the place where §1
could quietly erode, so the distinction is worth stating precisely.

**Allowed: an employer's own ATS job board.** Greenhouse, Ashby,
SmartRecruiters, Lever and Workable each serve a company's vacancies
unauthenticated, in JSON, documented, for the express purpose of syndication —
the same feed renders that company's careers page. Reading it is the intended
use. Weigh it against the three reasons in §1: it does not add bot-shaped
applications to anyone's pile, it touches no third party's personal data, and
there is no contract to breach because publication is the point.

**Still forbidden: the eight platforms named above**, by any route, including a
third-party aggregator that reaches them by scraping on your behalf.

Enforced rather than intended:

- `ats.ALLOWED_HOSTS` is the complete set of hosts the module may contact, and
  `ats._get` checks it **at call time** — a board token holding a full URL cannot
  redirect a fetch.
- `tests/test_constraints.py` pins that set exactly, asserts no restricted
  platform appears in it, and asserts the module has **exactly one outbound call
  site** so the guard cannot be bypassed by a second helper.
- Adopted postings record `source_type=api` and the ATS name.
  `create_from_api` is separate from `create_from_jd` precisely so provenance
  stays honest; a test asserts the API path never claims `user_pasted`.

Adding a host is therefore a deliberate act that fails the build until the test
is updated too, which is the intended friction.

## 2. No autonomous submission

There is no submit endpoint, and there must never be one. `export.py` produces a
DOCX or PDF; the user uploads it to the employer's own form. Grep `app/routers/`
for "submit" — the only occurrences are UI copy stating that the click is the
user's.

This is the mechanism behind the application-flood crisis described in PRD §0.1.
Building it would make Compass part of the problem it was written to solve.

## 3. The quality gate is deterministic

`app/services/quality_gate.py` contains no LLM call, and must not acquire one.
A gate you can argue your way past is not a gate, and asking a model to grade
output from the same family of models is exactly that. The signals are embedding
similarity and rule checks — reproducible, testable, and unbribable.

The same reasoning applies to `ats_check.py`: parseability is a set of mechanical
facts about a file, so it gets mechanical rules.

## 4. Prompts are versioned files

Prompt text lives in `app/llm/prompts/<name>.v<N>.md` and is registered in
`PROMPT_VERSIONS`. No prompt strings inline in Python. The version is part of the
response cache key, so bumping it invalidates stale cached responses instead of
silently serving output from an older prompt.

## 5. Nothing invents facts about the candidate

Every prompt that writes résumé or letter content is told that numbers may come
only from the Career Profile, and each has a structured accountability field
(`claims_avoided`, `needs_user_input`, `honest_gaps`) that the UI surfaces. The
quality gate's `honesty` signal flags a package that identified real gaps but
recorded nothing it avoided claiming.

An invented metric here is not a cosmetic bug: it ends up in a real application,
and then in an interview the candidate cannot defend.
