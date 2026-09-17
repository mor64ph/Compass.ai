"""Gap report (PRD Epic B).

Sits on top of the deterministic scorer: the keyword layer already knows which
terms are missing, so this step is only asked for what it cannot compute -
whether a missing term is a wording problem or a real capability gap, whether
the résumé reads at the JD's seniority, and where it is already overclaiming.
"""

from __future__ import annotations

import logging

from app.llm import LLMResult, get_llm
from app.schemas import LLMGapReport
from app.services.jd_match import MatchReport
from app.services.textutil import clip

logger = logging.getLogger(__name__)

MAX_JD_CHARS = 14000
MAX_RESUME_CHARS = 14000


def build(
    *,
    profile_dict: dict,
    resume_text: str,
    jd_text: str,
    jd_title: str,
    jd_company: str,
    match: MatchReport,
) -> dict:
    llm = get_llm()

    # Hand over the scorer's own findings rather than the full term table -
    # the whole point is not to re-litigate the keyword pass.
    match_summary = {
        "composite": match.composite,
        "keyword_score": match.keyword_score,
        "semantic_score": match.semantic_score,
        "semantic_backend_is_model": match.semantic_backend_is_model,
        "top_missing_terms": match.top_missing(20),
        "strongest_matched_terms": [h.term for h in sorted(match.found, key=lambda h: -h.weight)[:15]],
        "terms_present_in_profile_but_not_resume": [
            h.term for h in match.found if "profile only" in h.matched_as
        ],
        "seniority": match.seniority,
    }

    user = (
        f"# Role\n{jd_title} at {jd_company}\n\n"
        f"# Résumé text\n{clip(resume_text, MAX_RESUME_CHARS)}\n\n"
        f"# Job description\n{clip(jd_text, MAX_JD_CHARS)}"
    )

    result: LLMResult = llm.structured(
        "gap_report",
        user=user,
        output_model=LLMGapReport,
        prompt_vars={"profile_json": profile_dict, "match_json": match_summary},
        category="parse",
    )
    report: LLMGapReport = result.value

    return {
        "summary": report.summary,
        "items": [item.model_dump() for item in report.items],
        "honest_gaps": list(report.honest_gaps),
        "strongest_matches": list(report.strongest_matches),
        "cached": result.cached,
    }
