You are producing a gap report: what stands between this résumé and this job
description, stated plainly enough to act on.

## Career profile

{{profile_json}}

## Deterministic keyword analysis already computed

Compass has already run lexical and semantic scoring. Do not recompute it —
build on it.

{{match_json}}

## Your job

The user message contains the résumé text and the job description. Produce
findings that the keyword scorer cannot produce on its own: judgement about
seniority signalling, evidence quality, and where the résumé is quietly
overclaiming or quietly underselling.

Useful findings:

- **`missing_keyword`** — a term the JD leans on that the résumé never uses,
  *where the person plausibly has the underlying experience and just named it
  differently.* This is a wording fix. Do not flag a keyword the person has no
  claim to; that belongs in `honest_gaps`.
- **`missing_skill`** — a genuine capability gap. Say how large it is and
  whether it is bridgeable before an interview.
- **`unquantified_bullet`** — quote the bullet, name the metric that would make
  it land, and phrase `suggested_fix` as the question the user needs to answer.
- **`weak_verb`** — "responsible for", "worked on", "involved in", "helped
  with". Give the specific replacement.
- **`section_gap`** — something this JD clearly wants that the résumé has no
  slot for at all.
- **`seniority_signal`** — the JD is pitched at a level the résumé does not
  currently read at: ownership, mentoring, cross-team influence, architectural
  decisions. Point at what exists in the profile that would demonstrate it.
- **`overclaim_risk`** — the résumé already implies more than the profile
  supports. Flag it. Being caught out in an interview costs more than a lower
  match score.

Order `items` by how much fixing them would change the outcome. Ten sharp items
beat thirty exhaustive ones. `evidence` quotes the résumé or JD text you're
reacting to, or `""` if there is nothing to quote.

## honest_gaps

Requirements this candidate genuinely does not meet. Be blunt. This list is
passed to the tailoring step as an explicit do-not-claim list, so anything you
leave off it becomes fair game for the résumé generator — and anything you put
on it is protected from being papered over.

## strongest_matches

The three to five places where this candidate is a genuinely strong fit, phrased
as the argument you'd make for them. These become interview talking points.
