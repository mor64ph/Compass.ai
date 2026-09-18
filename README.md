# Compass

[![Python](https://img.shields.io/badge/python-3.10%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115%2B-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![HTMX](https://img.shields.io/badge/HTMX-2.0-3366CC)](https://htmx.org/)
![Tests](https://img.shields.io/badge/tests-357%20passing-brightgreen)

An AI-assisted job-search copilot. Scores job descriptions against your career
history, tailors résumés and cover letters, tracks applications, and blocks
low-quality submissions before they go out.

**Live demo:** <https://compass-9vgo.onrender.com>

![Discover](docs/screenshots/discover.png)

---

## Table of Contents

- [About](#about)
- [Features](#features)
- [Tech Stack](#tech-stack)
- [Screenshots](#screenshots)
- [Getting Started](#getting-started)
- [Usage](#usage)
- [Project Structure](#project-structure)
- [Testing](#testing)
- [Deployment](#deployment)
- [Documentation](#documentation)
- [Roadmap](#roadmap)
- [Contributing](#contributing)
- [License](#license)
- [Acknowledgements](#acknowledgements)

---

## About

Compass is a self-hosted web application that supports the full job-search
workflow: building a structured career profile, checking résumés for ATS
parseability, scoring job descriptions, generating tailored documents, and
tracking applications through to interview.

Three things distinguish it from a generic résumé generator:

- **A quality gate.** Applications are scored on six deterministic signals
  before they can be marked ready. Near-duplicate cover letters and letters with
  no employer-specific content are blocked outright. No language model is
  involved in the decision.
- **Fit-after-tailoring scores.** Each opening gets two numbers — coverage from
  the résumé as written, and coverage once the résumé reflects evidence already
  in the profile. The difference identifies which roles are worth the effort.
- **Provider-agnostic AI.** Google Gemini, local Ollama, or Anthropic, selected
  by configuration. The application runs without any API key; the deterministic
  features are unaffected.

---

## Features

**Profile & documents**
- Conversational intake plus structured forms, compiled into one career profile
- ATS parseability checker: ~30 checks across eight groups
- Two-column and DOCX text-box detection
- Résumé generation from profile data; DOCX and PDF export

**Matching & tailoring**
- Weighted keyword coverage (55%) and semantic similarity (45%), reported separately
- Section-aware JD parsing — terms in *Requirements* weighted above *About us*
- Curated skill lexicon for surface-form canonicalisation
- Gap reports separating missing skills from unlisted ones
- Bullet and cover-letter tailoring grounded in profile evidence

**Pipeline**
- Job discovery from Greenhouse, Ashby, SmartRecruiters, Lever and Workable feeds
- Kanban application tracker with forward-only stage transitions
- Optional Gmail sync and Google Calendar interview events
- Funnel analytics with interview rate segmented by quality band

**Interview prep**
- Per-application briefs with cited company research
- Questions generated from the actual identified gaps
- STAR answer bank and rehearsal mode

**Platform**
- Multi-user with per-account data isolation and per-account AI spend caps
- SQLite or PostgreSQL from the same codebase
- Responsive down to 320px

---

## Tech Stack

| Category | Technology |
|---|---|
| Backend | FastAPI, Python 3.10+ |
| Frontend | Jinja2 templates, HTMX 2.0 (no build step) |
| Database | SQLAlchemy 2.0 — SQLite or PostgreSQL |
| Migrations | Alembic |
| AI providers | Google Gemini, Ollama, Anthropic |
| Embeddings | sentence-transformers (all-MiniLM-L6-v2) |
| Documents | pdfplumber, python-docx, ReportLab |
| Auth | `hashlib.scrypt`, itsdangerous signed cookies |
| Integrations | Gmail API, Google Calendar API |
| Testing | pytest, Playwright (CDP) |
| Hosting | Render, Neon PostgreSQL |

---

## Screenshots

| Dashboard | Applications |
|---|---|
| ![Dashboard](docs/screenshots/dashboard.png) | ![Applications](docs/screenshots/applications.png) |

| Résumés | Tracker |
|---|---|
| ![Résumés](docs/screenshots/resumes.png) | ![Tracker](docs/screenshots/tracker.png) |

<details>
<summary>Mobile view (390px)</summary>

<img src="docs/screenshots/discover-mobile.png" width="390" alt="Discover on mobile">

</details>

---

## Getting Started

### Prerequisites

- Python 3.10 or higher (tested on 3.12 and 3.14)
- Git
- ~2.5 GB free disk space (the semantic layer pulls in PyTorch)

Optional:
- A [Google AI Studio](https://aistudio.google.com/apikey) API key — free, no card
- [Ollama](https://ollama.com/) for fully local inference
- PostgreSQL for multi-user or hosted deployments

### Installation

```bash
git clone https://github.com/mor64ph/ai-jobsearch-engine.git
cd ai-jobsearch-engine

python -m venv .venv
```

Install dependencies:

```bash
# Windows
.venv\Scripts\python.exe -m pip install -r requirements.txt

# macOS / Linux
.venv/bin/python -m pip install -r requirements.txt
```

To skip PyTorch, install everything except `sentence-transformers`. Semantic
scoring falls back to a lexical backend and the UI marks affected scores.

### Configuration

Copy the example file and set a secret key:

```bash
cp .env.example .env
```

| Variable | Default | Description |
|---|---|---|
| `COMPASS_SECRET_KEY` | — | **Required.** Signs session cookies |
| `GEMINI_API_KEY` | — | Google AI Studio key |
| `COMPASS_LLM_PROVIDER` | `auto` | `gemini`, `ollama`, `anthropic`, or `auto` |
| `COMPASS_GEMINI_MODEL` | `gemini-3.5-flash` | Model name |
| `COMPASS_DB_URL` | local SQLite | e.g. `postgresql+psycopg://user:pass@host/db` |
| `COMPASS_OPEN_SIGNUP` | `false` | Allow public registration |
| `COMPASS_MAX_ACCOUNTS` | `200` | Account ceiling (`0` = unlimited) |
| `COMPASS_SIGNUP_BUDGET_USD` | `1.0` | Monthly AI budget per new account |
| `COMPASS_HTTPS_ONLY` | `false` | Set `true` behind TLS |
| `COMPASS_MAX_UPLOAD_MB` | `10` | Upload size limit |

The application refuses to start if `COMPASS_SECRET_KEY` is left at its default
value while bound to a non-loopback address.

### Running

```bash
.venv\Scripts\python.exe -m uvicorn app.main:app
```

Open <http://localhost:8000>. On first run you are redirected to `/setup` to
create the owner account. The database and schema are created automatically.

Windows users can use the included scripts instead:

| Script | Purpose |
|---|---|
| `setup.cmd` | Create the virtual environment and install dependencies |
| `run.cmd` | Start the server (`run.cmd 8001` for another port) |
| `check.cmd` | Run the test suite and smoke test |
| `verify-llm.cmd` | Exercise every AI-backed feature |

---

## Usage

| Step | Page | Action |
|---|---|---|
| 1 | Profile | Complete the intake conversation and forms, then compile |
| 2 | Résumés | Upload a PDF/DOCX for an ATS report, or generate one from the profile |
| 3 | Discover | Add employer ATS boards; review roles ranked by fit after tailoring |
| 4 | Applications | Add a JD, review the match and gaps, tailor, pass the quality gate, export |
| 5 | Tracker | Move cards through the pipeline; optionally sync Gmail |
| 6 | Prep | Generate an interview brief and rehearse |

### Quality gate signals

| Signal | Weight | Blocks |
|---|---|---|
| `letter_divergence` | 3.0 | Yes |
| `company_specificity` | 2.5 | Yes |
| `bullet_divergence` | 1.5 | No |
| `jd_uptake` | 1.5 | No |
| `honesty` | 1.0 | No |
| `substance` | 1.0 | No |

Blocks are overridable, and the override reason is recorded on the application.

---

## Project Structure

```
app/
├── main.py             Application entry point and lifespan
├── config.py           Settings (pydantic-settings)
├── db.py, models.py    Engine, sessions, data model
├── security.py         CSP headers, login throttling
├── auth.py, scoping.py Registration and per-user record ownership
├── llm/
│   ├── client.py       Caching, budgets, structured output
│   ├── providers/      One adapter per AI provider
│   └── prompts/        Versioned prompt files
├── routers/            One module per page
├── services/           One module per capability
├── templates/          Jinja2 + HTMX
└── static/             CSS and JavaScript
docs/                   Architecture, security, deployment guides
migrations/             Alembic migrations
scripts/                Smoke test, responsive check, backup, AI verification
tests/                  13 test modules
```

---

## Testing

```bash
.venv\Scripts\python.exe -m pytest -q                  # 357 tests, no API key required
.venv\Scripts\python.exe scripts\smoke.py              # boot and walk every route
.venv\Scripts\python.exe scripts\check_responsive.py   # viewport overflow check
.venv\Scripts\python.exe scripts\verify_llm.py         # exercise AI paths (needs a key)
```

| Suite | Covers |
|---|---|
| `test_constraints.py` | Architecture rules — banned dependencies, no submit endpoint, no inline scripts |
| `test_tenancy.py` | Per-user isolation, tested over HTTP rather than the service layer |
| `test_ats_check.py` | Every ATS rule and its false-positive case |
| `test_jd_match.py` | Section weighting, alias canonicalisation, score ordering |
| `test_quality_gate.py` | Composite scoring and hard blocks |
| `test_security.py` | Headers, throttling, session handling |
| `test_migrations.py` | Alembic upgrade path and schema drift detection |

`scripts/smoke.py` runs the application in-process against a throwaway database
with no credentials configured, and asserts that AI-backed routes degrade to an
error banner rather than a 500.

See [docs/MANUAL_QA.md](docs/MANUAL_QA.md) for the manual checklist.

---

## Deployment

Deployable on free tiers without a credit card, using Render for the application
and Neon for PostgreSQL.

1. Fork this repository.
2. In Render: **New → Blueprint**, select the fork, and apply
   [`render.yaml`](render.yaml).
3. Create a Neon project and copy the connection string.
4. In the Render dashboard, set `COMPASS_DB_URL` and `GEMINI_API_KEY`.

Notes for the free tier:

- 512 MB RAM, so the build installs `deploy/render/requirements-slim.txt` and
  runs without the embedding model.
- No persistent disk, so PostgreSQL is required — SQLite does not survive a
  deploy or a wake from idle.
- Instances sleep after inactivity; the first request after sleep is slow while
  the instance restarts.

A `Dockerfile` is available in `deploy/render/`. Full instructions, alternative
hosts and a pre-flight checklist are in [docs/DEPLOY.md](docs/DEPLOY.md).

Back up with `scripts/backup.py --out <dir>`, which produces a self-verifying
logical export.

---

## Documentation

| Document | Contents |
|---|---|
| [docs/DEPLOY.md](docs/DEPLOY.md) | Deployment walkthrough and host comparison |
| [docs/SECURITY.md](docs/SECURITY.md) | Threat model, hardening, residual risks |
| [docs/CONSTRAINTS.md](docs/CONSTRAINTS.md) | Design boundaries and their rationale |
| [docs/LLM_PROVIDERS.md](docs/LLM_PROVIDERS.md) | Provider setup and trade-offs |
| [docs/GOOGLE_OAUTH_SETUP.md](docs/GOOGLE_OAUTH_SETUP.md) | Gmail and Calendar configuration |
| [docs/MANUAL_QA.md](docs/MANUAL_QA.md) | Manual test checklist |

### Design boundaries

Two limits are enforced by tests rather than convention, and are documented in
[docs/CONSTRAINTS.md](docs/CONSTRAINTS.md):

- **No scraping or session automation** of job boards. Listings come from
  employers' own public ATS APIs, or from a pasted job description.
- **No automated submission.** Compass produces the documents; the user submits
  them.

---

## Roadmap

- [ ] Outreach UI (data model exists, no interface yet)
- [ ] Scheduler for weekly digests
- [ ] Additional ATS source adapters
- [ ] Encryption at rest
- [ ] Privacy policy, required before hosting third-party data at scale

---

## Contributing

This is a personal project, but bug reports and suggestions are welcome — please
open an issue.

If you submit a pull request, run the test suite first:

```bash
.venv\Scripts\python.exe -m pytest -q
```

`tests/test_constraints.py` enforces the architectural rules above and will fail
the build if they are violated.

---

## License

No license has been chosen yet. Default copyright applies, and no permissions are
granted for reuse or distribution.

---

## Acknowledgements

- [FastAPI](https://fastapi.tiangolo.com/) and [HTMX](https://htmx.org/)
- [sentence-transformers](https://www.sbert.net/) for local embeddings
- [pdfplumber](https://github.com/jsvine/pdfplumber) for PDF layout introspection
- Greenhouse, Ashby, SmartRecruiters, Lever and Workable for public job APIs

Built by [@mor64ph](https://github.com/mor64ph).
