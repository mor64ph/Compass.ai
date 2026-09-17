# Compass — Phase 2 (shareable)

An AI-native job search copilot. Runs on one machine, SQLite on disk,
**invite-only** with per-user data isolation and per-user AI spend caps.

> **First run:** start it and open <http://localhost:8000>. With no account yet
> it sends you to `/setup` to create the owner account. Everyone else joins by
> an invite link you generate in Settings — there is no public signup form.

Built from the Compass PRD (`job-search-copilot-prd.md` — drop a copy in `docs/`
to keep the project self-contained). The thesis in one line: 2026's market has a
volume problem, not a tooling problem — so Compass optimises for **fewer,
sharper, human-reviewed applications**, and treats application quality as a
first-class metric rather than an afterthought.

Two consequences run through the whole codebase:

- **No scraping, no session automation.** Not of LinkedIn, Naukri, Indeed,
  Foundit, Wellfound, Glassdoor, Fishbowl, or Reddit. Job postings arrive by
  public API (Phase 2) or by you pasting a JD you found yourself.
- **No autonomous submission.** Compass produces the files; you click submit.
  There is no submit endpoint, and `tests/test_constraints.py` fails the build if
  one appears.

The reasoning is in [docs/CONSTRAINTS.md](docs/CONSTRAINTS.md). It is the product
thesis, not a compliance footnote.

---

## Quick start

Verified on Python 3.14.4 / Windows 11.

**Double-click these, in order.** No terminal, no paths to remember:

| File | What it does | Costs |
|---|---|---|
| `setup.cmd` | Creates the environment, installs everything, writes `.env`. Run once. Safe to re-run. | free |
| `check.cmd` | Runs all 154 tests plus the smoke walk. | free |
| `run.cmd` | Starts the app, then open <http://localhost:8000>. Ctrl+C to stop. | free |
| `verify-llm.cmd` | Exercises every Claude-backed feature and reports what works. | ~$1 |

`run.cmd 8001` starts it on a different port. It refuses to start if the port is
already taken, rather than dying with a traceback and leaving you looking at a
*stale* Compass served by an older process.

<details>
<summary>Doing it by hand instead</summary>

Every command below names the venv's interpreter by full path, so it behaves
identically in cmd.exe and PowerShell with no activation step:

```
cd "c:\Users\hrisit.biswas\personal projects\compass"

python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt   # pulls torch, ~2 GB

copy .env.example .env
.venv\Scripts\python.exe -m uvicorn app.main:app
```

**Don't use a bare `uvicorn app.main:app`** unless the venv is activated — it
resolves to whatever `uvicorn` is first on PATH, which may be a global install
missing Compass's dependencies. Activation differs by shell: PowerShell
`.\.venv\Scripts\Activate.ps1`, cmd.exe `.venv\Scripts\activate.bat`.

Add `--reload` only while editing code: it restarts the server on every file
change, and each restart re-imports torch.

</details>

The database is created on first start. There is no migration tool in Phase 1 —
the schema is created from the models and evolved by hand until Phase 2.

### Troubleshooting startup

| Symptom | Cause |
|---|---|
| `ModuleNotFoundError: No module named 'itsdangerous'` (or `sqlalchemy`, `pdfplumber`, `anthropic`) and the traceback shows paths under `AppData\Local\Programs\Python` | You're running the **global** Python, not the venv. Either the venv wasn't activated, or `Activate.ps1` was run from cmd.exe, where it does nothing. Use the `.venv\Scripts\python.exe -m uvicorn ...` form above. |
| `Address already in use` on port 8000 | An earlier server is still running. `netstat -ano \| findstr :8000` to find the PID, then `taskkill /PID <pid> /F`. Or pass `--port 8001`. |
| Startup logs nothing for minutes the very first time | Windows Defender scanning the freshly installed torch DLLs. One-off — see Startup cost below. |
| Every code edit triggers a slow restart | `--reload` restarts the worker on any file change, and each restart re-imports torch. Drop `--reload` for normal use. |
| Amber "No Anthropic credentials" banner | Expected without a key. The deterministic half still works — see below. |

### Minimum configuration

```ini
COMPASS_SECRET_KEY=<random>      # signs the session cookie
GEMINI_API_KEY=...               # free tier, no card: aistudio.google.com/apikey
```

**Compass is not tied to one AI provider.** `COMPASS_LLM_PROVIDER` selects
`gemini` (free tier, the default), `ollama` (fully local, no key at all),
`anthropic` (best quality, paid), or `auto` — which uses whichever is configured,
preferring free and keyless. Full setup and the honest trade-offs are in
**[docs/LLM_PROVIDERS.md](docs/LLM_PROVIDERS.md)**.

The short version: Gemini for daily use, Ollama so the tool never dies when a
key expires, Anthropic as the quality benchmark to compare the others against.

**Compass is useful with no API key at all.** The ATS parseability checker, the
JD keyword/semantic scorer, the tracker, the funnel analytics and the quality
gate are all deterministic. Only résumé parsing, gap reports, tailoring, email
classification and prep briefs need Claude.

### If the install is too heavy

`sentence-transformers` is the only large dependency (it pulls torch). Skip it
and the app still runs — `app/services/embeddings.py` falls back to a
pure-Python lexical similarity, and every score that used it says so in the UI.
Semantic scores and the gate's divergence signals become approximate; nothing
breaks.

With the real model loaded, the gate's divergence signal separates cleanly:
~0.99 similarity for two near-identical letters against ~0.28 for genuinely
different ones, with the `COMPASS_TEMPLATED_THRESHOLD` default of 0.85 sitting
between them.

### Startup cost of the embedding model

Measured on this machine (Python 3.14, torch 2.13 CPU, Windows 11):

| Step | Time |
|---|---|
| `import torch` | 6–8s |
| `import sentence_transformers` (pulls in transformers) | 20–27s |
| `SentenceTransformer(...)` from the local cache | **0.6s** |
| First `encode()` | ~1s |
| **Total** | **~30–45s** |

Three things worth knowing:

- **The very first load takes minutes, not seconds.** That is Windows Defender
  scanning the ~2 GB of freshly installed torch DLLs, plus a cold file cache.
  It does not repeat.
- **It does not block you.** The load runs on a daemon thread, so the app serves
  requests about 13 seconds after launch. You only ever wait if you score a JD
  inside the first half-minute. Settings shows whether the model is loaded yet.
- **`SentenceTransformer(...)` is loaded cache-first** (`local_files_only=True`).
  Left to itself it revalidates every file against the Hugging Face API on
  construction, which measured **49.9s versus 0.6s** — an 80× difference for
  identical behaviour. A cache miss raises immediately and falls back to a normal
  download, so first run still works.

What remains is import time for torch and transformers, which no amount of
tuning avoids. If that trade isn't worth it, set
`COMPASS_PRELOAD_EMBEDDINGS=false` to defer it to first use, or skip
`sentence-transformers` entirely and take the instant lexical fallback.

### Google (optional, Epic E)

Gmail + Calendar sync needs a one-time OAuth setup:
**[docs/GOOGLE_OAUTH_SETUP.md](docs/GOOGLE_OAUTH_SETUP.md)**. Two scopes only —
`gmail.readonly` and `calendar.events`.

---

## The workflow

| Step | Where | What happens |
|---|---|---|
| 1 | **Profile** | Conversational intake that pushes for numbers and asks what changed about your scope, plus a form for everything structured. Compile → the Career Profile every other epic reads. |
| 2 | **Résumés** | Upload a PDF/DOCX → deterministic parseability report. Or generate a clean single-column variant straight from the profile. |
| 3 | **Applications** | Paste a JD → weighted keyword + semantic match → gap report → tailored résumé and cover letter → **quality gate** → export DOCX/PDF. |
| 4 | **Tracker** | Kanban board. Sync Gmail to move cards automatically; place interview slots on Calendar. |
| 5 | **Prep** | Per-application brief: cited company research + questions grounded in this JD and your real gaps + STAR bank + rehearsal mode. |

---

## Accounts, isolation and spend (Phase 2)

Phase 1 had no login: fine for one person on one machine, unacceptable the moment
anyone else can reach it. Phase 2 adds the minimum that makes sharing defensible.

- **Invite-only.** No self-service signup. `/setup` creates the owner account and
  then refuses to work again; everyone else needs a token link that expires in 14
  days and works once.
- **Passwords** use `hashlib.scrypt` from the standard library — memory-hard, no
  dependency to keep patched, ~100 ms per verification. Login reports one message
  for both a wrong password and an unknown address, and runs the KDF either way so
  timing doesn't reveal which addresses exist.
- **Every aggregate root carries `user_id`.** Path ids go through
  `app/scoping.py::require_owned`, which returns **404 rather than 403** for
  someone else's record — a 403 still confirms the id is real.
- **Per-user AI budgets.** Invitees default to $5/month and the owner is
  uncapped. The owning user rides in a `ContextVar` bound per request, so the
  cap is enforced inside the LLM client rather than at each of the dozen call
  sites. Spend is recorded *before* the refusal check, so a user cannot burn
  budget invisibly by repeatedly tripping a refusal.
- **The response cache is scoped per user** — the user id is mixed into the hash.
  That forgoes some cost saving, but cached output is derived from someone's
  résumé and serving it to another account is not a trade worth making.

`tests/test_tenancy.py` (32 tests) is what earns this the right to be shared. It
tests through HTTP rather than the service layer, because the failure being
guarded against is *a route that forgot to scope its query* — a service-level
test would pass while the route leaked.

**Still not Phase 3.** No encryption at rest, no privacy policy, no hosted
deployment. Inviting a few people you trust onto your own machine is a different
risk from publishing; PRD §9 and the DPDP Act still apply before this goes
public.

## The parts worth understanding

### The quality gate (`app/services/quality_gate.py`)

The feature the thesis rests on, and the reason this is not just another
tailoring tool. Before an application can be marked ready it is scored against
your last N applications on six signals:

| Signal | Weight | Asks |
|---|---|---|
| `letter_divergence` | 3.0 | Is this the same letter with the company name swapped? |
| `company_specificity` | 2.5 | Does it reference anything only this employer would recognise? |
| `bullet_divergence` | 1.5 | Were the résumé bullets actually re-tailored? |
| `jd_uptake` | 1.5 | Did tailoring close the gaps the scorer found? |
| `honesty` | 1.0 | Real gaps identified — did it record what it avoided claiming? |
| `substance` | 1.0 | Any numbers? Reasonable length? Talking points present? |

Two signals hard-block regardless of the composite: a letter too similar to a
recent one, and a letter with nothing company-specific in it. Blocking is
overridable — you are an adult, and sometimes a genuinely similar role deserves a
genuinely similar letter — but the reason is written down and stored on the
record.

**No LLM is involved.** A gate you can argue your way past is not a gate, and
asking a model to grade output from the same family of models would be exactly
that.

The dashboard segments interview rate by quality band. If the high band does not
outperform the low band in your own data, the thesis is wrong and you should be
the first to know.

### The ATS checker (`app/services/ats_check.py`)

Rule-based, ~20 rules across five groups: text layer, layout, contact fields,
section vocabulary, and content. The layout rules are the interesting ones —
`text_extract.py` measures the widest vertical whitespace channel on each PDF
page to catch two-column layouts that have no ruled table to give them away, and
detects DOCX text boxes by walking the raw XML, because text in a text box is
invisible to `document.paragraphs` and to most parsers.

Deterministic on purpose: same answer every time, no API key, unit-testable.

### The JD scorer (`app/services/jd_match.py`)

Two signals, reported separately so the score is explainable:

- **Keyword coverage (55%)** — JD sections are detected and weighted, so a term
  in *Requirements* is worth 20× one in *About us*. A curated
  [skill lexicon](app/data/skill_lexicon.json) canonicalises surface forms, so
  `PowerBI` / `power-bi` / `Microsoft Power BI` are one term, and `star schema
  design` matches a JD asking for `dimensional modelling`. An n-gram pass catches
  domain language the lexicon has never heard of.
- **Semantic (45%)** — local sentence-transformer similarity between JD
  requirement sentences and résumé lines.

It also distinguishes *"you don't have this"* from *"you have this and it just
isn't on the page"*, which is the cheapest fix available and the one no keyword
scorer surfaces on its own.

### The Gmail sync (`app/services/google/gmail_sync.py`)

A rules pass runs first — ATS sender domains and unambiguous subject wording —
so most of a real inbox is dismissed without an LLM call. Only the ambiguous
remainder is classified. Confidence below 0.6 files the email against the
application but **does not move the card**: a wrong automatic stage change
silently corrupts the tracker, which is worse than doing nothing. Events are
unique on `(source, external_id)`, so re-running a sync cannot duplicate
anything.

---

## Layout

```
compass/
├── app/
│   ├── main.py               FastAPI app + lifespan
│   ├── config.py             pydantic-settings, reads .env
│   ├── db.py, models.py      SQLite + PRD §7 data model
│   ├── schemas.py            internal payloads + LLM output contracts
│   ├── web.py                Jinja env, flash messages, failure guard
│   ├── llm/
│   │   ├── client.py         Anthropic wrapper: cache, rate limits, structured output
│   │   ├── schema.py         Pydantic → strict JSON schema
│   │   └── prompts/*.v1.md   versioned prompt files, never inline
│   ├── services/             one module per capability
│   ├── routers/              one per surface
│   ├── templates/            Jinja + HTMX
│   └── data/skill_lexicon.json
├── docs/
│   ├── CONSTRAINTS.md        the non-negotiables and why
│   └── GOOGLE_OAUTH_SETUP.md
├── scripts/smoke.py          boots the app and walks every route
└── tests/
```

### Conventions

- **Prompts are files.** `app/llm/prompts/<name>.v<N>.md`, registered in
  `PROMPT_VERSIONS`. The version is part of the response cache key, so bumping it
  invalidates stale entries instead of silently serving output from an old
  prompt. A test fails if a registered prompt has no file.
- **LLM output models have no `Optional` fields.** Structured outputs emit a
  strict JSON schema; nullable unions make it fragile. Where a value may be
  unknown, the prompt says to emit `""` or `[]`.
- **Deterministic and LLM logic stay in separate modules.** Judgement goes to
  Claude; mechanical facts do not.

---

## Tests

```
.venv\Scripts\python.exe -m pytest -q          # 154 tests, no API key needed
.venv\Scripts\python.exe scripts\smoke.py      # boots the app, walks every route
.venv\Scripts\python.exe scripts\verify_llm.py # exercises every Claude path (costs ~$1)
```

`scripts/smoke.py` runs the whole app in-process against a throwaway database
with no credentials configured. It asserts the LLM-backed routes degrade into a
banner rather than a 500, then prints the ATS score, the JD match breakdown and
every quality-gate signal — the fastest way to see whether a scoring change did
what you intended.

[docs/MANUAL_QA.md](docs/MANUAL_QA.md) is the human checklist for everything
automation can't reach — live Claude calls, a real Google account, real résumé
files, and whether the output is any *good*.

Four pytest suites:

- `test_constraints.py` — the architecture guard: no scraping libraries, no URLs
  targeting restricted platforms, no submit endpoint, no LLM call inside the
  quality gate or ATS checker, no inline prompt strings, every registered prompt
  has a file. A prose instruction is easy to forget across sessions; a failing
  test is not.
- `test_ats_check.py` — every rule, plus the false-positive cases (a narrow
  gutter is not a two-column layout).
- `test_jd_match.py` — section weighting, alias canonicalisation, punctuation
  terms like `ci/cd`, and score ordering. Pinned to the lexical backend so no
  model downloads.
- `test_quality_gate.py`, `test_pipeline.py` — the gate's hard blocks, the stage
  machine's forward-only rule, funnel maths, export, and schema hardening.

---

## Before starting Phase 2

Ranked by what actually blocks you.

1. **Exercise every LLM path once against the live API.** Run:

   ```
   .venv\Scripts\python.exe scripts\verify_llm.py
   .venv\Scripts\python.exe scripts\verify_llm.py --with-research   # adds web search
   ```

   Eleven prompt paths have never executed against Claude. The deterministic
   half is tested hard; this half is not tested at all. Known risks: the hardened
   JSON schema from `app/llm/schema.py` has never hit the wire, `output_config`
   carries both `effort` and `format` in one object, and the `web_search`
   result-block parsing is written from the docs rather than observed.

   The script hits every path against a fixture profile in a throwaway database,
   keeps going after a failure, and prints a table. It distinguishes **FAIL** (the
   call errored or the schema was rejected — a code problem) from a **WEAK**
   assertion (the call worked but the output wasn't useful — a prompt problem).
   Roughly $0.50–$1.00 per full run, and responses land in the normal cache so a
   re-run is free.

   Then work through §2–§6 of [MANUAL_QA.md](docs/MANUAL_QA.md) for the
   judgement calls a script can't make.
2. **Add Alembic before real data accumulates.** Phase 1 creates the schema with
   `create_all` and evolves it by hand. Phase 2 adds tables for Epic C sources
   and Epic F outreach, and by then your actual career profile is in that SQLite
   file. One column change during this build (`Certification.issued_on`, Date →
   String) is exactly the kind of edit that breaks a populated database.
3. **Decide how backups work.** `compass.db` holds your entire profile and there
   is no backup story. Even a scheduled file copy would do.
4. **Put one real résumé and one real inbox through it.** Synthetic fixtures
   cannot validate the two-column PDF detection or the fuzzy company matching in
   Gmail sync. Those are the parts most likely to be subtly wrong.
5. **Drop the PRD into `docs/PRD.md`** so the project is self-contained.

Two Phase 2 decisions that aren't specified in the PRD and shouldn't be guessed:
Epic C needs an Adzuna app ID/key and a source-adapter interface, and the
"weekly digest" implies a scheduler — Phase 1 has none.

## Phase 1 scope, and what is deliberately absent

**In:** Epic A (intake), Epic B (ATS + match + gaps + rewrites), Epic D
(tailoring + quality gate + export), Epic E (tracker + Gmail + Calendar),
lightweight Epic G (prep brief + rehearsal).

**Not built:** Epic C's API source adapters, Epic F's outreach UI (the `Contact`
and `OutreachMessage` tables exist; nothing writes to them from an automated
path), Epic H's public portfolio page, Epic I.

**Not built on purpose:** auth, multi-tenancy, encryption at rest, rate limiting
beyond the in-process limiter, a privacy policy. Those are Phase 3 requirements
(PRD §9) and half-building them would be worse than their absence. Once Compass
holds anyone else's résumé you become a data controller under India's DPDP Act —
that needs a lawyer, not a `TODO`.

**The Phase 3 gate is literal:** publish only after Phase 1 has actually produced
real offers.

## Open items from the PRD

| PRD §12 | Status |
|---|---|
| Stack | Resolved — FastAPI + HTMX + SQLite, local sentence-transformers |
| India-first + India-friendly remote | Assumed; confirm in the intake flow |
| Browser extension vs. paste box | Paste box for Phase 1 |
| Multi-tenant PII | Phase 3, flagged above |
