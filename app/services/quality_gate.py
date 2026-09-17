"""Application quality gate (PRD Epic D, Section 0.1).

This is the feature the PRD's thesis rests on. Every other tool in the market
optimises for throughput; this one is a brake. Before an application can be
marked ready, it is scored against the last N applications on six signals, and
if it looks like the same letter with the company name swapped, it says so and
blocks the "ready" flag until the user either fixes it or overrides with a
written reason.

Entirely deterministic - no LLM call. The point of a gate is that it cannot be
talked around, and asking a model to grade output from the same family of models
would be exactly that.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Application
from app.services import embeddings
from app.services.textutil import has_metrics, normalise_company, word_count

# verdict thresholds on the composite score
PASS_AT = 75.0
REVIEW_AT = 55.0

COVER_LETTER_MIN_WORDS = 120
COVER_LETTER_MAX_WORDS = 400


@dataclass
class Signal:
    name: str
    label: str
    score: float  # 0-100, higher is better
    weight: float
    detail: str
    verdict: str  # good | warn | bad

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class QualityReport:
    score: float
    verdict: str  # pass | review | templated
    compared_against: int
    signals: list[Signal] = field(default_factory=list)
    blocking_reason: str = ""
    backend: str = ""

    @property
    def blocks_ready(self) -> bool:
        return self.verdict == "templated"

    def as_dict(self) -> dict:
        return {
            "score": round(self.score, 1),
            "verdict": self.verdict,
            "compared_against": self.compared_against,
            "blocking_reason": self.blocking_reason,
            "backend": self.backend,
            "signals": [s.as_dict() for s in self.signals],
        }


def evaluate(session: Session, application: Application) -> QualityReport:
    settings = get_settings()
    window = settings.compass_quality_window

    letter = application.cover_letter_text or ""
    meta = application.tailoring_meta or {}
    posting = application.job_posting
    company = posting.company if posting else ""
    jd_text = posting.jd_text if posting else ""

    previous = _recent_applications(
        session, user_id=application.user_id, exclude_id=application.id, limit=window
    )
    prev_letters = [a.cover_letter_text for a in previous if (a.cover_letter_text or "").strip()]
    prev_bullets = [
        b.get("tailored", "")
        for a in previous
        for b in (a.tailoring_meta or {}).get("bullets", [])
        if b.get("tailored")
    ]

    # Everything the user would actually send. The gate re-runs after a manual
    # edit, so signals must score the current documents rather than the
    # generator's record of what it once produced - otherwise hand-written
    # improvements could never move the score.
    live_text = "\n".join([letter, application.tailored_resume_md or ""])

    signals: list[Signal] = [
        _company_specificity(letter, company, meta),
        _letter_divergence(letter, prev_letters, settings.compass_templated_threshold),
        _bullet_divergence(meta, prev_bullets, bool(application.tailored_resume_md)),
        _jd_uptake(meta, application.match_report or {}, live_text),
        _honesty(meta, application.gap_report or {}, letter),
        _substance(letter, meta),
    ]

    total_weight = sum(s.weight for s in signals) or 1.0
    composite = sum(s.score * s.weight for s in signals) / total_weight

    blocking = ""
    # Two hard failures override the composite. A letter that is textually the
    # same as the last few is templated no matter how many keywords it hit, and
    # a letter with nothing company-specific in it cannot be "tailored" by
    # definition.
    similarity_signal = next(s for s in signals if s.name == "letter_divergence")
    specificity_signal = next(s for s in signals if s.name == "company_specificity")
    if similarity_signal.verdict == "bad":
        blocking = similarity_signal.detail
    elif specificity_signal.verdict == "bad":
        blocking = specificity_signal.detail

    if blocking:
        verdict = "templated"
    elif composite >= PASS_AT:
        verdict = "pass"
    elif composite >= REVIEW_AT:
        verdict = "review"
    else:
        verdict = "templated"
        blocking = (
            f"Composite quality score is {composite:.0f}, below the {REVIEW_AT:.0f} floor. "
            "Check the individual signals below."
        )

    return QualityReport(
        score=composite,
        verdict=verdict,
        compared_against=len(prev_letters),
        signals=signals,
        blocking_reason=blocking,
        backend=embeddings.get_backend().name,
    )


def _recent_applications(
    session: Session, *, user_id: int, exclude_id: int | None, limit: int
) -> list[Application]:
    """Only this user's history. Comparing across accounts would both leak
    another person's letters and make the divergence signal meaningless."""
    stmt = (
        select(Application)
        .where(
            Application.user_id == user_id,
            Application.cover_letter_text != "",
        )
        .order_by(Application.created_at.desc())
        .limit(limit + 1)
    )
    rows = list(session.scalars(stmt))
    return [a for a in rows if a.id != exclude_id][:limit]


# --------------------------------------------------------------------------
# Signals
# --------------------------------------------------------------------------


def _company_specificity(letter: str, company: str, meta: dict) -> Signal:
    """Would this letter survive a find-and-replace of the company name?"""
    details = [d for d in meta.get("company_specific_details_used", []) if d.strip()]
    canonical = normalise_company(company)
    mentions = 0
    if canonical:
        # Count mentions of any distinctive word from the company name.
        tokens = [t for t in canonical.split() if len(t) > 2]
        for token in tokens:
            mentions += len(re.findall(rf"(?<![a-z]){re.escape(token)}(?![a-z])", letter, re.I))

    if not details and mentions == 0:
        return Signal(
            name="company_specificity",
            label="Company specificity",
            score=0.0,
            weight=2.5,
            detail=(
                "The cover letter never names the company and the generator recorded no "
                "company-specific details. This letter would read identically for any "
                "employer - which is precisely what recruiters are filtering out."
            ),
            verdict="bad",
        )
    if not details:
        return Signal(
            name="company_specificity",
            label="Company specificity",
            score=35.0,
            weight=2.5,
            detail=(
                f"The company is named {mentions} time(s), but no concrete company or role "
                "detail was used. Naming an employer is not the same as knowing anything "
                "about them. Run company research and regenerate."
            ),
            verdict="warn",
        )
    score = min(100.0, 55.0 + 15.0 * len(details) + min(mentions, 3) * 5.0)
    return Signal(
        name="company_specificity",
        label="Company specificity",
        score=score,
        weight=2.5,
        detail=f"{len(details)} company-specific detail(s) used: " + "; ".join(details[:3]),
        verdict="good" if score >= 70 else "warn",
    )


def _letter_divergence(letter: str, previous: list[str], threshold: float) -> Signal:
    """How different is this letter from the recent ones? The headline signal."""
    if not letter.strip():
        return Signal(
            name="letter_divergence",
            label="Divergence from recent letters",
            score=0.0,
            weight=3.0,
            detail="No cover letter has been generated yet.",
            verdict="bad",
        )
    if not previous:
        return Signal(
            name="letter_divergence",
            label="Divergence from recent letters",
            score=80.0,
            weight=3.0,
            detail=(
                "No earlier applications to compare against yet. This signal becomes "
                "meaningful from the second application onward."
            ),
            verdict="good",
        )

    sims = embeddings.best_match_scores([letter], previous)
    peak = sims[0] if sims else 0.0
    matrix = embeddings.get_backend().similarity_matrix([letter], previous)[0]
    mean = sum(matrix) / len(matrix) if matrix else 0.0

    if peak >= threshold:
        return Signal(
            name="letter_divergence",
            label="Divergence from recent letters",
            score=max(0.0, (1.0 - peak) * 100.0),
            weight=3.0,
            detail=(
                f"This letter is {peak:.0%} similar to one of your last "
                f"{len(previous)} letters (threshold {threshold:.0%}). Your applications "
                "are trending templated - slow down and rewrite this one, or reconsider "
                "whether this role is worth applying to."
            ),
            verdict="bad",
        )
    # Map similarity onto a score: identical = 0, unrelated = 100.
    score = max(0.0, min(100.0, (1.0 - peak) / max(threshold, 0.01) * 100.0))
    return Signal(
        name="letter_divergence",
        label="Divergence from recent letters",
        score=score,
        weight=3.0,
        detail=(
            f"Peak similarity to your last {len(previous)} letters is {peak:.0%} "
            f"(mean {mean:.0%}). Below the {threshold:.0%} threshold."
        ),
        verdict="good" if peak < threshold * 0.8 else "warn",
    )


def _bullet_divergence(
    meta: dict, prev_bullets: list[str], has_tailored_resume: bool
) -> Signal:
    bullets = [b.get("tailored", "") for b in meta.get("bullets", []) if b.get("tailored")]
    if not bullets:
        # A hand-written résumé has no generator metadata to compare, which is
        # not the same thing as an untailored one - say which case this is.
        return Signal(
            name="bullet_divergence",
            label="Résumé bullets re-tailored",
            score=65.0 if has_tailored_resume else 40.0,
            weight=1.5,
            detail=(
                "The résumé for this role was written or edited by hand, so there are no "
                "generated bullets to compare against recent applications."
                if has_tailored_resume
                else "No tailored résumé for this role yet - it is unchanged from the base."
            ),
            verdict="warn",
        )
    if not prev_bullets:
        return Signal(
            name="bullet_divergence",
            label="Résumé bullets re-tailored",
            score=85.0,
            weight=1.5,
            detail=f"{len(bullets)} bullets tailored. No earlier set to compare against.",
            verdict="good",
        )

    sims = embeddings.best_match_scores(bullets, prev_bullets)
    reused = sum(1 for s in sims if s >= 0.93)
    ratio = reused / len(bullets)
    score = max(0.0, 100.0 - ratio * 110.0)
    return Signal(
        name="bullet_divergence",
        label="Résumé bullets re-tailored",
        score=score,
        weight=1.5,
        detail=(
            f"{reused} of {len(bullets)} tailored bullets are near-identical to bullets "
            "used in a recent application."
            + (
                " Some carry-over is fine - your best quantified bullets should recur."
                if ratio <= 0.5
                else " Most of this résumé was not actually re-tailored."
            )
        ),
        verdict="good" if ratio <= 0.5 else ("warn" if ratio <= 0.75 else "bad"),
    )


def _jd_uptake(meta: dict, match_report: dict, live_text: str) -> Signal:
    """Did the tailoring actually close the keyword gaps the scorer found?"""
    targets = [t.lower() for t in match_report.get("top_missing", [])][:15]
    if not targets:
        return Signal(
            name="jd_uptake",
            label="JD gaps addressed",
            score=90.0,
            weight=1.5,
            detail="The scorer found no significant missing terms to close.",
            verdict="good",
        )
    incorporated = {k.lower() for k in meta.get("keywords_incorporated", [])}
    # `live_text` is the current résumé and letter; the meta fields are added so
    # a term the generator worked into a section still counts after an edit.
    haystack = " ".join(
        [live_text, meta.get("summary", "")]
        + [b.get("tailored", "") for b in meta.get("bullets", [])]
        + list(meta.get("skills_to_surface", []))
    ).lower()

    closed = [t for t in targets if t in incorporated or t in haystack]
    ratio = len(closed) / len(targets)
    return Signal(
        name="jd_uptake",
        label="JD gaps addressed",
        score=ratio * 100.0,
        weight=1.5,
        detail=(
            f"{len(closed)} of {len(targets)} high-value missing terms now appear in the "
            "tailored package."
            + ("" if ratio >= 0.4 else " Check whether the rest are genuine gaps or just wording.")
        ),
        verdict="good" if ratio >= 0.5 else ("warn" if ratio >= 0.25 else "bad"),
    )


def _honesty(meta: dict, gap_report: dict, letter: str) -> Signal:
    """Guard against the failure mode that costs most: a package that papers
    over a real gap and gets exposed in the interview."""
    honest_gaps = [g for g in gap_report.get("honest_gaps", []) if g.strip()]
    avoided = [c for c in meta.get("claims_avoided", []) if c.strip()]

    if not honest_gaps:
        return Signal(
            name="honesty",
            label="Overclaim guard",
            score=90.0,
            weight=1.0,
            detail="The gap report identified no hard gaps for this role.",
            verdict="good",
        )
    if not avoided:
        return Signal(
            name="honesty",
            label="Overclaim guard",
            score=30.0,
            weight=1.0,
            detail=(
                f"{len(honest_gaps)} genuine gap(s) were identified, but the generator "
                "recorded nothing it avoided claiming. Read the package against the gap "
                "list before sending - the risk is a claim you cannot defend in a screen."
            ),
            verdict="warn",
        )
    return Signal(
        name="honesty",
        label="Overclaim guard",
        score=90.0,
        weight=1.0,
        detail=(
            f"{len(honest_gaps)} gap(s) identified, {len(avoided)} claim(s) explicitly "
            "avoided: " + "; ".join(avoided[:2])
        ),
        verdict="good",
    )


def _substance(letter: str, meta: dict) -> Signal:
    words = word_count(letter)
    issues: list[str] = []
    score = 100.0

    if words < COVER_LETTER_MIN_WORDS:
        issues.append(f"only {words} words")
        score -= 40
    elif words > COVER_LETTER_MAX_WORDS:
        issues.append(f"{words} words is long enough to get skimmed")
        score -= 15

    if not has_metrics(letter):
        issues.append("no numbers anywhere in the letter")
        score -= 30

    if not meta.get("talking_points"):
        issues.append("no talking points generated")
        score -= 15

    score = max(0.0, score)
    return Signal(
        name="substance",
        label="Substance",
        score=score,
        weight=1.0,
        detail=(
            "; ".join(issues).capitalize()
            if issues
            else f"{words} words, includes concrete figures, talking points present."
        ),
        verdict="good" if score >= 75 else ("warn" if score >= 50 else "bad"),
    )
