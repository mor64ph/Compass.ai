"""Rank a user's discovered jobs against their résumé and career profile.

## Two stages, because the hardware demands it

The semantic half of `jd_match` embeds every requirement sentence in a posting.
A single employer's board can be 650 roles, and on a 4-core laptop with no usable
GPU embedding all of them is minutes-to-hours. So:

1. **Prefilter** - a lexical pass over the whole corpus using nothing but
   substring checks. Fast enough for thousands of postings, and used purely to
   choose what is worth the expensive pass. It is a recall device, not a ranking.
2. **Deep score** - the real `jd_match.score()` on the top `deep_limit` only.

A job that has only been prefiltered carries `is_deep_scored = False`, and the UI
says so rather than presenting a cheap score as a real one.

## Closeable fit - the metric worth having

Everyone else ranks your résumé as it stands. That answers "what do I look like
today", which is the wrong question when the next step is to tailor the thing.

Compass knows something a résumé-only matcher cannot: the career profile holds
skills and projects that never made it onto the page. `jd_match.score()` already
accepts that as `extra_evidence` and tags anything it matches there as
`"profile only - not on the résumé"`. So two numbers come out of one pass:

* **current** - coverage from the résumé alone. Comparable to what a job matcher
  shows you.
* **closeable** - coverage once the résumé says what the profile already knows.
  No invention involved; this is evidence you have and did not mention.

The list sorts on closeable, and the gap between the two is its own signal: a
role at 58 that becomes 79 after honest tailoring is a better use of an evening
than one that is flat at 72.

One free consequence of how `jd_match` is built: `extra_evidence` feeds only the
keyword pass, and the semantic score depends on the JD and résumé alone. Both
numbers therefore come from a *single* scoring call rather than two, which halves
the embedding work.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import CareerProfile, DiscoveredJob, ResumeVariant, utcnow
from app.services import jd_match, profile_service
from app.services.textutil import clip

logger = logging.getLogger(__name__)

# How many prefiltered jobs get the embedding pass per run. 40 is roughly a
# minute of CPU on the machine this was written for; raise it if you have a GPU.
DEEP_LIMIT = 40

MAX_JD_CHARS = 18000

# The marker `jd_match` writes into `TermHit.matched_as` for a term found only in
# the profile. Partitioning the hits on it is what separates current from
# closeable fit, so the two must stay in step.
PROFILE_ONLY_MARKER = "profile only"


@dataclass
class RankResult:
    considered: int = 0
    deep_scored: int = 0
    backend: str = ""

    def as_dict(self) -> dict:
        return {
            "considered": self.considered,
            "deep_scored": self.deep_scored,
            "backend": self.backend,
        }


# --------------------------------------------------------------------------
# Stage 1: the cheap pass
# --------------------------------------------------------------------------


def _user_vocabulary(profile: CareerProfile | None, resume_text: str) -> set[str]:
    """Lower-cased terms this candidate can plausibly claim.

    Drawn from the lexicon rather than from raw résumé tokens, so the prefilter
    is matching canonical skills instead of incidental English.
    """
    haystack = (resume_text or "").lower()
    if profile is not None:
        haystack += " " + " ".join(
            filter(
                None,
                [
                    profile.headline or "",
                    profile.summary or "",
                    " ".join(s.name for s in profile.skills),
                    " ".join(
                        f"{e.title} {e.company}" for e in profile.experiences
                    ),
                    " ".join(
                        f"{p.name} {p.highlight} {' '.join(p.tech or [])}"
                        for p in profile.projects
                    ),
                ],
            )
        ).lower()

    lexicon = jd_match.load_lexicon()
    vocabulary: set[str] = set()
    for canonical, variants in lexicon["aliases"].items():
        for form in (canonical, *variants):
            if form.lower() in haystack:
                vocabulary.add(canonical.lower())
                break
    return vocabulary


def _target_titles(profile: CareerProfile | None) -> set[str]:
    if profile is None or profile.aspiration is None:
        return set()
    return {t.strip().lower() for t in (profile.aspiration.target_titles or []) if t.strip()}


def _prefilter_score(
    job: DiscoveredJob, vocabulary: set[str], titles: set[str]
) -> float:
    """A rough 0-100 that only has to order things well enough to choose what to
    score properly.

    Substring containment, not regex: this runs over the whole corpus, and
    `"power bi" in text` is a C-level scan where a compiled alternation over two
    hundred terms is not.
    """
    if not job.jd_text and not job.title:
        return 0.0
    blob = f"{job.title}\n{job.department}\n{job.jd_text}".lower()

    if vocabulary:
        hits = sum(1 for term in vocabulary if term in blob)
        coverage = hits / len(vocabulary)
    else:
        coverage = 0.0

    title = (job.title or "").lower()
    title_bonus = 0.0
    for target in titles:
        if target and target in title:
            title_bonus = 0.35
            break
    else:
        # Partial credit when the words of a target title appear out of order,
        # which is most real postings ("Senior Developer, Power BI").
        for target in titles:
            words = [w for w in target.split() if len(w) > 3]
            if words and all(w in title for w in words):
                title_bonus = 0.2
                break

    return round(min(1.0, coverage + title_bonus) * 100.0, 1)


# --------------------------------------------------------------------------
# Stage 2: the real pass
# --------------------------------------------------------------------------


def _split_keyword_scores(report: jd_match.MatchReport) -> tuple[float, float]:
    """`(current, closeable)` keyword coverage from one report's hits.

    `closeable` is what the report already computed. `current` re-runs the same
    arithmetic while treating a profile-only match as missing, which is exactly
    what a résumé-only matcher would have reported.
    """
    total = sum(h.weight for h in report.hits)
    if not total:
        return 0.0, 0.0
    on_resume = sum(
        h.weight
        for h in report.hits
        if h.found and PROFILE_ONLY_MARKER not in h.matched_as
    )
    everything = sum(h.weight for h in report.hits if h.found)
    return (
        round(on_resume / total * 100.0, 1),
        round(everything / total * 100.0, 1),
    )


def score_job(
    job: DiscoveredJob,
    *,
    resume_text: str,
    evidence: str,
    has_real_resume: bool = True,
) -> jd_match.MatchReport:
    report = jd_match.score(
        resume_text=resume_text,
        jd_text=clip(job.jd_text, MAX_JD_CHARS),
        jd_title=job.title,
        extra_evidence=evidence,
    )

    keyword_current, keyword_closeable = _split_keyword_scores(report)
    current = (
        jd_match.KEYWORD_WEIGHT * keyword_current
        + jd_match.SEMANTIC_WEIGHT * report.semantic_score
    )
    closeable = (
        jd_match.KEYWORD_WEIGHT * keyword_closeable
        + jd_match.SEMANTIC_WEIGHT * report.semantic_score
    )

    job.keyword_score = keyword_current
    job.semantic_score = report.semantic_score
    job.match_score = round(current, 1)
    job.closeable_score = round(closeable, 1)
    job.is_deep_scored = True
    job.scored_at = utcnow()
    job.match_report = {
        "backend": report.backend,
        "semantic_backend_is_model": report.semantic_backend_is_model,
        "keyword_current": keyword_current,
        "keyword_closeable": keyword_closeable,
        "semantic": report.semantic_score,
        "uplift": round(closeable - current, 1),
        # Without an uploaded résumé the two numbers are the same by
        # construction, so the UI must say why rather than showing +0.0.
        "has_real_resume": has_real_resume,
        "seniority": report.seniority,
        "top_missing": report.top_missing(10),
        "matched_on_resume": [
            h.term
            for h in sorted(report.found, key=lambda h: -h.weight)
            if PROFILE_ONLY_MARKER not in h.matched_as
        ][:14],
        # The actionable list: evidence you have, on a JD that wants it, absent
        # from the document you would send.
        "matched_profile_only": [
            h.term
            for h in sorted(report.found, key=lambda h: -h.weight)
            if PROFILE_ONLY_MARKER in h.matched_as
        ][:14],
    }
    return report


def rank(
    session: Session,
    user_id: int,
    *,
    deep_limit: int = DEEP_LIMIT,
    rescore_all: bool = False,
) -> RankResult:
    """Prefilter the whole corpus, then deep-score the most promising slice.

    `rescore_all` throws away existing deep scores, which is what you want after
    editing the profile - the closeable numbers are derived from it.
    """
    profile = profile_service.get_active_profile(session, user_id)
    resume_text, has_real_resume = _master_resume_text(session, user_id, profile)
    evidence = profile_service.profile_evidence_text(profile)

    vocabulary = _user_vocabulary(profile, resume_text)
    titles = _target_titles(profile)

    jobs = list(
        session.scalars(
            select(DiscoveredJob).where(
                DiscoveredJob.user_id == user_id,
                DiscoveredJob.is_open == True,  # noqa: E712
                DiscoveredJob.dismissed == False,  # noqa: E712
            )
        )
    )
    result = RankResult(considered=len(jobs))
    if not jobs:
        return result

    # Cheap pass over everything. A job that has never been scored keeps its
    # prefilter number so the list can still be ordered sensibly.
    prefiltered: list[tuple[float, DiscoveredJob]] = []
    for job in jobs:
        rough = _prefilter_score(job, vocabulary, titles)
        if not job.is_deep_scored:
            job.match_score = rough
            job.closeable_score = rough
        prefiltered.append((rough, job))

    needs_deep = [
        (rough, job)
        for rough, job in prefiltered
        if rescore_all or not job.is_deep_scored
    ]
    needs_deep.sort(key=lambda pair: -pair[0])

    backend = ""
    for _, job in needs_deep[:deep_limit]:
        if not job.jd_text.strip():
            continue
        try:
            report = score_job(
                job,
                resume_text=resume_text,
                evidence=evidence,
                has_real_resume=has_real_resume,
            )
        except Exception:
            # One malformed posting must not abandon the run. Left un-deep-scored
            # so the next pass retries it.
            logger.exception("Scoring discovered job %s failed", job.id)
            continue
        backend = report.backend
        result.deep_scored += 1

    result.backend = backend
    session.commit()
    return result


def _master_resume_text(
    session: Session, user_id: int, profile: CareerProfile | None
) -> tuple[str, bool]:
    """`(text, is_real_resume)`.

    The flag matters more than it looks. With no uploaded résumé the fallback is
    generated *from the profile*, so it already contains every skill the profile
    knows - which leaves nothing for `extra_evidence` to add and pins the uplift
    at exactly zero for every role. Reporting that as "tailoring gains you
    nothing" would be a lie about the feature rather than a fact about the job,
    so the caller uses this to say "upload a résumé" instead.

    Ranking against an empty string is the other trap: it reports every role as a
    total mismatch, which also reads as a broken feature.
    """
    variant = session.scalars(
        select(ResumeVariant)
        .where(
            ResumeVariant.user_id == user_id,
            ResumeVariant.is_master == True,  # noqa: E712
        )
        .limit(1)
    ).first()
    if variant is None:
        variant = session.scalars(
            select(ResumeVariant)
            .where(ResumeVariant.user_id == user_id)
            .order_by(ResumeVariant.ats_score.desc())
            .limit(1)
        ).first()
    if variant is not None:
        return (variant.content_md or variant.raw_text or ""), True
    return (profile_service.profile_to_markdown(profile) if profile else ""), False
