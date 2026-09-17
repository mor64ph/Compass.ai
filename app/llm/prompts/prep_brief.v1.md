You are building an interview prep brief for one specific application.

## Career profile

{{profile_json}}

## Gaps and honest weak spots for this role

{{gap_json}}

## Company and role research (cited, may be empty)

{{research}}

## Sources available for citation — use only these URLs

{{citations_json}}

## Output

**`company_snapshot`** — 150–250 words the candidate can actually hold in their
head walking in: what the company does, what's happened recently, and what that
implies about the conversation they're about to have. Cite URLs inline from the
list above. If research was empty, say so rather than writing generic filler.

**`likely_questions`** — 10–15 questions, grounded in *this* JD and *this*
candidate rather than a generic bank:

- `behavioural` — from the JD's stated ways of working.
- `technical` — from the actual technologies in the JD. For a BI/data role that
  means DAX, semantic modelling, dimensional design, SQL performance, refresh
  strategy, and Power BI/Databricks specifics — not generic data-structures and
  algorithms puzzles, unless the JD genuinely calls for them.
- `domain` — the company's industry and data problems.
- `role_specific` — the scope and seniority the JD describes.
- `gap_probe` — the questions that will land on the honest gaps above. These are
  the most valuable questions in the brief. Do not soften them, and give an
  `answer_scaffold` that acknowledges the gap and pivots to adjacent real
  experience rather than bluffing.

`why_likely` ties the question to specific JD or research text.
`answer_scaffold` is the shape of a strong answer using this candidate's real
material — not a script to memorise.

**`star_stories`** — 4–6 stories built strictly from the profile's quantified
achievements. Keep the profile's real numbers; invent nothing. `maps_to` lists
the questions each story can answer, so one story can cover several.

**`technical_focus`** — a short, ordered revision list. Most valuable first.

**`salary_briefing`** — the range with its sources and dates, plus a note on
where this candidate's level sits in it. State it plainly when the data is thin.

**`questions_to_ask_them`** — 5 questions that only someone who did this
research could ask. No "what does a typical day look like".

Never omit a key. Unknown strings are `""`, unknown lists are `[]`.
