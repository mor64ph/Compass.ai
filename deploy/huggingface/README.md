---
title: Compass
emoji: 🧭
colorFrom: blue
colorTo: gray
sdk: docker
app_port: 7860
pinned: false
license: mit
short_description: An AI job-search copilot built as a brake, not an accelerator
---

# Compass

An AI copilot for the whole job-search pipeline — and a deliberate brake on it.

Compass checks a résumé for ATS parseability, scores it against a job
description, separates the gaps that are real from the ones that are only
wording, tailors an application against the ones you can honestly close, then
**scores its own output and refuses to export work that reads like a template**.

**This is a public demo.** The credentials are on the sign-in page. Data resets
whenever the Space restarts.

## What to try

1. **Discover** — roles pulled from employers' own ATS job boards, ranked by
   *fit after honest tailoring* rather than by how the résumé reads today. Two
   numbers per role, and the gap between them is the point: a role at 58 that
   becomes 79 once the résumé mentions work you already did is a better use of
   an evening than one flat at 72. The list names the terms to add.
2. **Résumés → ATS check** — deterministic, no model involved. Same answer every
   time, and it runs before anything else because a résumé an ATS cannot read
   never reaches a human.
3. **Applications → Quality gate** — six signals, no LLM anywhere near it,
   including similarity against your own recent letters. It blocks the "ready to
   submit" flag on work that looks templated until you either fix it or record a
   written override.

## Two things it will not do

- **No scraping.** Not LinkedIn, Naukri, Indeed, Foundit, Wellfound, Glassdoor,
  Fishbowl or Reddit. Postings arrive from employers' own ATS feeds — the same
  JSON that renders their careers page — or you paste one you found yourself.
- **No autonomous submission.** There is no submit endpoint and there never will
  be. Compass produces the files; you upload them.

Both are enforced by tests, not policy: the build fails on a banned library, a
restricted hostname, a submit-shaped route, or an LLM reference inside the
quality gate.

The reasoning is that a saturated market has a volume problem, not a tooling
problem. Adding throughput makes it worse.

## Demo limitations

- **Data is ephemeral.** Free Spaces have no persistent disk, so the database is
  rebuilt on every restart.
- **The demo account is not an administrator** and carries a small monthly AI
  budget. When it is spent, the deterministic half — ATS checks, JD scoring, the
  quality gate, the tracker, discovery — keeps working, because none of it ever
  called a model.
- **Cited company research is unavailable.** It needs Google Search grounding,
  which is not on Gemini's free tier. Compass degrades gracefully: prep briefs
  and tailoring both treat research as optional.

## Stack

FastAPI · HTMX · SQLite · sentence-transformers (local, so résumé text never
leaves the container) · Gemini / Ollama / Anthropic behind one provider
interface.

308 tests, including 32 HTTP-layer tenant-isolation checks and an architecture
guard that fails the build if the constraints above are violated.

**Source:** <https://github.com/mor64ph/ai-jobsearch-engine>
