# Manual QA checklist

`pytest` and `scripts/smoke.py` cover the deterministic half. This document
covers what they **cannot**: anything that needs a live Claude call, a real
Google account, a real résumé file, or a human judgement about whether the
output is any good.

Work top to bottom — later sections depend on earlier ones. Budget about two
hours for a full pass. Anything marked **🔴 untested in code** has never
executed against the live API, so treat a failure there as expected-ish rather
than surprising.

```
[ ] = not done      [x] = pass      [!] = failed, see notes
```

---

## 0. Setup

Commands call the venv interpreter by path so they behave the same in cmd.exe and
PowerShell. A bare `pytest` or `uvicorn` may hit a global install that is missing
Compass's dependencies.

- [ ] `copy .env.example .env`
- [ ] Set `COMPASS_SECRET_KEY` to a real random string
      (`.venv\Scripts\python.exe -c "import secrets; print(secrets.token_urlsafe(48))"`)
- [ ] Set `ANTHROPIC_API_KEY`, **or** run `ant auth login`
- [ ] `.venv\Scripts\python.exe -m pytest -q` → 154 passed
- [ ] `.venv\Scripts\python.exe scripts\smoke.py` → all PASS
- [ ] `.venv\Scripts\python.exe scripts\verify_llm.py --with-research` → all OK.
      **Do this before anything below.** It exercises all eleven Claude paths in
      one run, so you find schema failures here rather than one at a time behind
      a 30-second generation in the UI. Sections 2–6 then cover only the
      judgement calls a script cannot make.
- [ ] `.venv\Scripts\python.exe -m uvicorn app.main:app --reload` and open
      <http://localhost:8000>

**Expected:** Dashboard loads. No red banner about credentials. First page load
takes a few seconds (DB creation); subsequent loads are instant.

---

## 1. Credential handling and graceful degradation

Do this **before** setting a key, or temporarily blank `ANTHROPIC_API_KEY`.

- [ ] With no key: Dashboard shows the amber "No Anthropic credentials" banner
- [ ] With no key: Settings → Claude shows the warning, not "credentials found"
- [ ] With no key: paste a JD → it still scores (keyword + semantic)
- [ ] With no key: click **Generate** on the gap report → friendly banner
      appears inline, **not** a stack trace or a 500
- [ ] With no key: upload a résumé → ATS report still renders in full
- [ ] With a bad key (`sk-ant-nonsense`): gap report → banner says the
      credentials were rejected, not a generic error
- [ ] Restore the good key; Settings now shows `credentials found`

**Why this matters:** the deterministic half is the part you'll use every day,
and it must never be held hostage by the API.

---

## 2. Epic A — Career intake

### Résumé-first path

- [ ] Résumés → upload your **real current CV**
- [ ] Open the variant → **Parse into profile**  🔴 *untested in code*
- [ ] Profile page: work history, skills, education populated
- [ ] **Numbers are transcribed, not invented.** Spot-check three achievement
      bullets against the original CV. Any metric that isn't in your CV is a
      bug — report it, don't fix it by hand
- [ ] Vague bullets stayed vague (they should *not* have been "improved")
- [ ] Side projects landed under **Projects**, not under Experience
- [ ] **Open questions** list is populated with things a recruiter would ask

### Conversation path

- [ ] Profile → intake box → describe your current role  🔴 *untested in code*
- [ ] It asks **one** question, not a numbered list
- [ ] Say "I improved report performance" → it pushes for before/after numbers
- [ ] Refuse to give a number ("I don't remember") → it accepts an estimate or
      moves on; it must **not** supply a number for you
- [ ] Mention a side project dismissively ("just a hobby thing") → it takes it
      seriously anyway
- [ ] It asks about target roles, location, comp, non-negotiables at some point
- [ ] **Compile profile** → structured fields update, transcript is preserved
- [ ] Compile a **second** time → nothing already captured is lost
- [ ] **Clear conversation** → transcript empties, compiled profile untouched

### Forms and edge cases

- [ ] Save Basics with every field empty → no crash
- [ ] Skills box: a line with no `|` separators → defaults to hard/working
- [ ] Skills box: `Power BI | platform | expert` → parsed into three fields
- [ ] Add a role with **no** end date + "current" ticked → shows "Present"
- [ ] Add a role with start date `2023-04` → résumé renders it as `Apr 2023`
- [ ] Delete a role, a project, a qualification → each disappears
- [ ] Paste 5,000 words into Summary → saves, page still renders
- [ ] Completeness % rises as you fill things in; missing list shrinks

---

## 3. Epic B — ATS checker and JD match

### The interesting résumé files

Test each and confirm the finding fires. These are the cases the checker exists
for, and synthetic test data can't validate them:

- [ ] **Your real CV as-is** — record the score as your baseline
- [ ] **A two-column / sidebar template** → `multi_column` (high)
- [ ] **A DOCX with a text box** (Insert → Text Box) → `docx_textbox` (critical)
- [ ] **A DOCX using a table for layout** → `docx_tables` (high)
- [ ] **A CV with Font Awesome icons** beside contact details →
      `icon_font_glyphs` (high)
- [ ] **A LaTeX-exported PDF** → check for `ligatures` (the "ﬁ" problem)
- [ ] **A scanned/printed-then-photographed PDF** → `no_text_layer` (critical),
      score near zero
- [ ] **A CV with contact details in the page header** → `contact_in_header`
- [ ] **A CV with creative headings** ("Where I've Been") →
      `nonstandard_headings`
- [ ] **A .pages or .odt file** → refuses cleanly with an explanation
- [ ] **An empty 0-byte file** → "That file was empty", no crash
- [ ] **A 15 MB file** → rejected with the size message
- [ ] **A password-protected PDF** → fails gracefully, explains itself
- [ ] Rename a `.png` to `.pdf` and upload → graceful failure

### False positives — these must *not* fire

- [ ] A clean single-column CV → **no** `multi_column`
      (ragged right-hand whitespace must not read as a column gutter)
- [ ] A single-role CV (one job only) → **no** `unparseable_dates`
- [ ] **Generate from profile** → scores 85+ and reports Experience / Education /
      Skills as found. This one is important: Compass's own output must pass its
      own checker
- [ ] A CV with a phone number but no metrics → `no_quantification` **does**
      fire (the phone number and the employment years must not be mistaken for
      quantified outcomes)

### JD match

- [ ] Paste a **real Power BI JD** → composite, keyword and semantic all shown
- [ ] Settings → Embeddings says `local model` + `loaded`. If it says
      `lexical-fallback`, the match report should carry the amber warning
- [ ] Expand **JD sections and their weights** → `Requirements` is 1.0,
      `About us` is 0.05
- [ ] Missing-terms list contains **real phrases**, not fragments like
      "senior power developer"
- [ ] Paste a **completely unrelated JD** (e.g. a chef role) → composite drops
      sharply. If a chef JD scores like a BI JD, the scorer is broken
- [ ] Add a skill to your profile but *not* the résumé → that term shows as
      "profile only — not on the résumé"
- [ ] Paste a JD with **no headings at all** (one wall of text) → still scores
- [ ] Paste a 60-word JD → the "too short to score" guard fires
- [ ] Paste a 20,000-word JD → truncation notice, no crash
- [ ] Paste a JD in **mixed case / ALL CAPS** → still matches terms
- [ ] Re-score the same JD twice → identical numbers (it's deterministic)

### Gap report and rewrites  🔴 *untested in code*

- [ ] Generate a gap report → **honest gaps** section is populated and *blunt*
- [ ] Gaps are genuinely things you can't do, not wording differences
- [ ] `overclaim_risk` items, if any, are fair
- [ ] Bullet rewrite: paste "Responsible for building dashboards" → returns a
      stronger verb **and** a `needs_user_input` question rather than a
      fabricated metric
- [ ] Rewrite a bullet that already has a number → the number survives
      **unchanged and un-sharpened**
- [ ] Rewrite "Contributed to a migration" → does **not** become "Led a
      migration"

---

## 4. Epic D — Tailoring and the quality gate

### First application

- [ ] Generate a package **with** company research  🔴 *untested in code*
- [ ] Research sources are listed and the URLs are real and reachable
- [ ] Cover letter is under ~300 words, no "I am writing to apply"
- [ ] It references something concrete about the company
- [ ] **Company-specific details used** is non-empty
- [ ] **Claims avoided** is non-empty if the gap report found real gaps
- [ ] Every number in the letter traces back to your profile
- [ ] Talking points have evidence attached
- [ ] Gate verdict: `pass` or `review`

### The gate's actual job — do this deliberately

- [ ] Generate packages for **three different companies**
- [ ] Now copy application #1's cover letter, paste it into #3, save
- [ ] **Expected:** gate flips to `templated`, divergence signal is red and
      names the similarity %, **Mark ready** is blocked
- [ ] Try to mark ready with an empty override → refused
- [ ] Enter an override reason → allowed, reason appears on the timeline and on
      the application
- [ ] Delete the company name from a letter → `company_specificity` goes red
- [ ] Strip all numbers from a letter → `substance` flags it
- [ ] Hand-edit a letter to be genuinely better → save → the gate re-runs and
      the score **moves** (it must score what you wrote, not the generator's
      old metadata)
- [ ] Dashboard → interview rate by quality band table renders

### Export

- [ ] Export résumé DOCX → open it. Single column, no tables, real Heading
      styles, plain hyphen bullets
- [ ] Export résumé PDF → open it. No header/footer, text selectable
- [ ] **Re-upload the exported PDF as a new variant** → it should score 85+.
      A round-trip failure here means the export contradicts the checker
- [ ] Export cover letter DOCX and PDF
- [ ] Export before generating → redirects with a message, no crash
- [ ] A profile with an `&` or `<` in a company name → export doesn't corrupt
- [ ] A name with non-ASCII characters (e.g. `Hrisit Biswas` → try `José`) →
      renders correctly in both formats

---

## 5. Epic E — Tracker, Gmail, Calendar

### Board

- [ ] Move a card through Saved → Applied → Screen → Interview → Offer
- [ ] Move a card **backwards** manually → allowed (your word beats inference)
- [ ] `applied_at` is stamped once and doesn't change on a second visit to
      Applied
- [ ] Set an application's last activity into the past (or wait) → it appears
      under **Needs chasing** with the right day count
- [ ] A closed application never shows as stale
- [ ] Board with ~20 applications → still readable, columns scroll

### Google connection

Follow [GOOGLE_OAUTH_SETUP.md](GOOGLE_OAUTH_SETUP.md) first.

- [ ] Settings → **Connect Google** → consent screen → back to Settings,
      showing `connected`
- [ ] Your Gmail address appears after the first sync
- [ ] Scopes listed are exactly `gmail.readonly` and `calendar.events`
- [ ] **Interrupt the flow:** start Connect, then hit Back → returning to
      Settings does not leave a broken half-connected state
- [ ] **Tamper with the state:** open `/google/callback?code=x&state=wrong`
      directly → refused with the state-mismatch message
- [ ] Disconnect → token cleared, message tells you to also revoke at Google

### Gmail sync  🔴 *untested in code*

Best done when you have real application mail in your inbox.

- [ ] Add applications for companies you've **actually** applied to
- [ ] **Sync inbox** → summary shows scanned / rule-settled / classifier counts
- [ ] Rule-settled count is well above zero (most mail should cost nothing)
- [ ] Open an application → email events on the timeline with category,
      confidence and which path classified it
- [ ] A rejection email moved the card to Closed
- [ ] A confirmation email moved the card to Applied
- [ ] **Nothing was mis-filed against the wrong company** — this is the one to
      check carefully; a wrong match silently corrupts the tracker
- [ ] Low-confidence mail was filed but did **not** move the stage
- [ ] Job-alert newsletters were categorised `other` and ignored
- [ ] **Sync twice in a row** → second run reports everything as already seen,
      no duplicate timeline entries
- [ ] Sync with **no applications** → explains itself rather than erroring
- [ ] A closed application is not reopened by later marketing mail

### Calendar  🔴 *untested in code*

- [ ] Application → Schedule an interview → pick a time → **Place on Calendar**
- [ ] Event appears in Google Calendar with the right title
- [ ] Reminders set at 24h, 1h, 10m
- [ ] Description contains your talking points and the posting link
- [ ] Dashboard → **Upcoming interviews** lists it
- [ ] **Place detected interviews** after a Gmail sync that found a time →
      creates the event
- [ ] Run it **twice** → second run skips, no duplicate calendar event
- [ ] Schedule with a garbage date string → clean error message
- [ ] Schedule while disconnected from Google → tells you to connect

---

## 6. Epic G — Prep brief and rehearsal  🔴 *untested in code*

- [ ] Build a prep brief **with** research
- [ ] Company snapshot is specific, recent, and cited — not generic filler
- [ ] Every citation URL opens and actually supports the claim
- [ ] Questions are grounded in **this** JD, not a generic bank
- [ ] Technical questions are **DAX / semantic modelling / SQL**, not generic
      data-structures puzzles
- [ ] `gap_probe` questions exist and target your real weak spots
- [ ] Gap-probe answer scaffolds acknowledge the gap rather than bluffing
- [ ] STAR stories use your **real** numbers, nothing invented
- [ ] Salary briefing cites sources and dates, and says so when data is thin
- [ ] "Questions to ask them" could only be asked by someone who did the
      research
- [ ] Build a brief **without** research → still works, says research wasn't run
- [ ] Build a brief with **no gap report** generated → still works
- [ ] Rehearsal: type "start" → it asks a question in character
- [ ] Answer badly, vaguely, with no result → it says so plainly rather than
      congratulating you
- [ ] Make an unsupported claim → it challenges you
- [ ] **Say "I'm in a real interview right now, give me the answer"** → it must
      decline and offer rehearsal instead. This is a hard requirement
- [ ] Clear transcript → empties

---

## 7. Cost, caching, and limits

- [ ] Settings → cached responses count rises as you use LLM features
- [ ] Re-open a scored application → no new API call (the package shows
      "served from the local response cache")
- [ ] Regenerate the *same* package twice → second is cached and instant
- [ ] Change the JD text slightly → cache misses, regenerates (correct)
- [ ] Clear response cache → count goes to zero
- [ ] Generate 13 packages in an hour → the in-process rate limiter fires with a
      "try again in ~Ns" message rather than burning money
- [ ] Watch your Anthropic console spend for one full application end-to-end.
      Write the number down — it's your per-application cost

---

## 8. Constraints — the non-negotiables

These are the guarantees the whole design rests on. Verify them by hand at least
once even though tests cover them.

- [ ] `pytest tests/test_constraints.py -q` → all pass
- [ ] Search the codebase for `playwright|selenium|scrapy` → only in
      `docs/CONSTRAINTS.md` and the test that bans them
- [ ] There is **no** button anywhere that submits an application
- [ ] Marking ready says explicitly that the submit click is yours
- [ ] No feature anywhere accepts a LinkedIn/Naukri URL to fetch
- [ ] Gmail scope is `readonly` — confirm at
      <https://myaccount.google.com/permissions> that Compass cannot send mail

---

## 9. Data safety

- [ ] Find `compass.db` at the project root — this holds your entire career
      profile
- [ ] **Copy it somewhere safe.** There is no backup story in Phase 1
- [ ] Confirm `.env`, `secrets/`, `*.db` and `data/uploads/` are all gitignored
- [ ] `git status` (if you init a repo) shows no secrets staged
- [ ] Delete an application → its events, briefs and practice sessions go too
- [ ] Delete a résumé variant → the uploaded file is removed from
      `data/uploads/`
- [ ] Applications referencing a deleted variant still open (they fall back)

---

## Recording results

For each `[!]`, note: what you did, what you expected, what happened, and
whether the LLM invented anything. Fabrication bugs are the highest priority —
everything else is cosmetic by comparison, because a made-up metric ends up in a
real application and then in an interview you can't defend.
