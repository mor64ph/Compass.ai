"""Tests for the JD-match scorer.

Written against the lexical fallback backend so they run without downloading a
sentence-transformer. The assertions are about *ordering and coverage* rather
than exact scores, which is the part that must hold whichever backend is loaded.
"""

from __future__ import annotations

import pytest

from app.services import embeddings, jd_match

JD = """\
Senior Power BI Developer

About us
We are a fast-paced, diverse and inclusive employer. We offer great benefits and a
flexible hybrid working culture in our Bangalore office.

Responsibilities
- Own the semantic model for our commercial analytics platform
- Develop and optimise DAX measures across large datasets
- Partner with stakeholders to define KPIs
- Mentor junior analysts

Requirements
- 5+ years with Power BI and strong DAX
- Deep experience with dimensional modelling and star schema design
- Advanced SQL and query optimisation
- Experience with row level security

Nice to have
- Exposure to Databricks
- Familiarity with Tableau
"""

STRONG_RESUME = """\
Senior Power BI Analyst

- Own the semantic layer for the commercial analytics platform
- Optimised DAX measures, cutting refresh from 42 to 9 minutes
- Designed star schema models with conformed dimensions
- Tuned SQL queries against a 400 GB warehouse
- Implemented row-level security for 3 regional teams
- Mentored 2 junior analysts on DAX performance
"""

WEAK_RESUME = """\
Marketing Coordinator

- Managed social media calendars and community engagement
- Coordinated event logistics for regional launches
- Produced monthly newsletters
- Liaised with agencies on creative briefs
"""


@pytest.fixture(autouse=True)
def lexical_backend(monkeypatch):
    """Pin the lexical backend so tests never trigger a model download."""
    backend = embeddings.LexicalBackend()
    monkeypatch.setattr(embeddings, "get_backend", lambda: backend)
    return backend


# --------------------------------------------------------------------------
# Section weighting
# --------------------------------------------------------------------------


def test_sections_are_split_and_weighted():
    sections = jd_match.split_jd_sections(JD, "Senior Power BI Developer")
    by_heading = {s.heading.lower(): s.weight for s in sections}

    assert by_heading["__title__"] == jd_match.TITLE_WEIGHT
    assert by_heading["requirements"] == 1.0
    assert by_heading["responsibilities"] == 0.9
    assert by_heading["nice to have"] == 0.4
    assert by_heading["about us"] == 0.05


def test_boilerplate_terms_do_not_dominate():
    """'diverse', 'inclusive' and 'benefits' live in About us at weight 0.05, so
    they must not outrank an actual requirement."""
    sections = jd_match.split_jd_sections(JD, "Senior Power BI Developer")
    terms = jd_match.extract_jd_terms(sections)
    dax_weight = terms.get("dax", (0, ""))[0]
    boilerplate = max(
        (weight for term, (weight, _) in terms.items() if term in {"inclusive", "benefits", "culture"}),
        default=0.0,
    )
    assert dax_weight > boilerplate


# --------------------------------------------------------------------------
# Term extraction and canonicalisation
# --------------------------------------------------------------------------


def test_lexicon_terms_are_extracted():
    sections = jd_match.split_jd_sections(JD, "Senior Power BI Developer")
    terms = jd_match.extract_jd_terms(sections)
    for expected in ("power bi", "dax", "dimensional modeling", "sql", "row level security"):
        assert expected in terms, f"{expected} missing from {sorted(terms)}"


def test_aliases_canonicalise_to_one_term():
    """'star schema design' and 'dimensional modelling' are the same requirement."""
    sections = jd_match.split_jd_sections("Requirements:\nstar schema design experience")
    terms = jd_match.extract_jd_terms(sections)
    assert "dimensional modeling" in terms


@pytest.mark.parametrize(
    "surface", ["Power BI", "power bi", "PowerBI", "power-bi", "Microsoft Power BI"]
)
def test_power_bi_surface_forms_all_match(surface: str):
    report = jd_match.score(
        resume_text=f"Built reports in {surface} for the finance team",
        jd_text="Requirements:\nStrong Power BI experience required",
    )
    matched = {hit.term for hit in report.found}
    assert "power bi" in matched


def test_terms_with_punctuation_match():
    """`ci/cd` and `c#`-style terms break naive \\b word-boundary matching."""
    pattern = jd_match._phrase_pattern("ci/cd")
    assert pattern.search("experience with CI/CD pipelines")
    assert not pattern.search("scientific")


def test_ngram_pass_catches_terms_outside_the_lexicon():
    jd = (
        "Requirements:\n"
        "- Experience in pharmaceutical cold chain logistics\n"
        "- Understanding of pharmaceutical cold chain compliance\n"
    )
    terms = jd_match.extract_jd_terms(jd_match.split_jd_sections(jd))
    assert any("pharmaceutical" in term for term in terms)


def test_ngrams_are_contiguous():
    """Regression: filtering tokens before forming n-grams welded the survivors
    together, inventing phrases like 'senior power developer' that appear
    nowhere in the posting."""
    terms = jd_match.extract_jd_terms(
        jd_match.split_jd_sections(
            "Requirements:\n- Senior Power BI Developer with strong DAX\n"
            "- Senior Power BI Developer duties include reporting\n",
            "Senior Power BI Developer",
        )
    )
    assert "senior power developer" not in terms
    assert "power developer" not in terms


def test_lexicon_tokens_do_not_reappear_as_bare_unigrams():
    """'Power BI' must not also yield a standalone 'power' term - that would
    double-count one requirement."""
    terms = jd_match.extract_jd_terms(
        jd_match.split_jd_sections(
            "Requirements:\n- Strong Power BI skills\n- Power BI modelling\n- Power BI service\n"
        )
    )
    assert "power bi" in terms
    assert "power" not in terms


def test_overlapping_phrases_are_folded_into_the_longest():
    weights = {
        "semantic": 3.0,
        "semantic model": 3.0,
        "own the semantic model": 3.0,
        "mentoring": 2.0,
    }
    kept = jd_match._keep_maximal_phrases(weights)
    assert "own the semantic model" in kept
    assert "semantic" not in kept
    assert "semantic model" not in kept
    assert "mentoring" in kept


def test_phrase_containment_respects_word_boundaries():
    assert jd_match._contains_phrase("semantic model design", "model")
    assert not jd_match._contains_phrase("semantic modelling", "model")


# --------------------------------------------------------------------------
# Scoring behaviour
# --------------------------------------------------------------------------


def test_strong_resume_outscores_weak_one():
    strong = jd_match.score(resume_text=STRONG_RESUME, jd_text=JD, jd_title="Senior Power BI Developer")
    weak = jd_match.score(resume_text=WEAK_RESUME, jd_text=JD, jd_title="Senior Power BI Developer")
    assert strong.composite > weak.composite
    assert strong.keyword_score > weak.keyword_score
    assert strong.semantic_score > weak.semantic_score


def test_scores_stay_in_range():
    for resume in (STRONG_RESUME, WEAK_RESUME, ""):
        report = jd_match.score(resume_text=resume, jd_text=JD)
        assert 0.0 <= report.composite <= 100.0
        assert 0.0 <= report.keyword_score <= 100.0
        assert 0.0 <= report.semantic_score <= 100.0


def test_empty_jd_does_not_crash():
    report = jd_match.score(resume_text=STRONG_RESUME, jd_text="")
    assert report.composite >= 0.0


def test_composite_is_the_declared_blend():
    report = jd_match.score(resume_text=STRONG_RESUME, jd_text=JD)
    expected = (
        jd_match.KEYWORD_WEIGHT * report.keyword_score
        + jd_match.SEMANTIC_WEIGHT * report.semantic_score
    )
    assert report.composite == pytest.approx(expected, abs=0.15)


def test_profile_evidence_is_marked_as_not_on_the_resume():
    """The distinction between "you don't have this" and "you have it but it
    isn't on the page" is the whole point of extra_evidence."""
    report = jd_match.score(
        resume_text="Built reports for the finance team",
        jd_text="Requirements:\nStrong Databricks experience",
        extra_evidence="Databricks, PySpark, Delta Lake",
    )
    hit = next(h for h in report.hits if h.term == "databricks")
    assert hit.found
    assert "profile only" in hit.matched_as


def test_top_missing_is_ordered_by_weight():
    report = jd_match.score(resume_text=WEAK_RESUME, jd_text=JD)
    missing = sorted(report.missing, key=lambda h: -h.weight)
    assert report.top_missing(5) == [h.term for h in missing[:5]]


def test_seniority_gap_is_reported():
    report = jd_match.score(resume_text=WEAK_RESUME, jd_text=JD)
    assert report.seniority["jd_signals"]
    assert report.seniority["coverage_ratio"] < 1.0
    assert report.seniority["note"]


def test_report_serialises_for_the_db():
    payload = jd_match.score(resume_text=STRONG_RESUME, jd_text=JD).as_dict()
    for key in ("composite", "keyword_score", "semantic_score", "matched_terms", "top_missing"):
        assert key in payload


def test_fallback_backend_is_reported_as_not_a_model():
    report = jd_match.score(resume_text=STRONG_RESUME, jd_text=JD)
    assert report.semantic_backend_is_model is False
    assert report.backend == "lexical-fallback"
