"""Tests for the quality gate - the feature the PRD's thesis rests on.

The behaviour that matters: a letter that would read identically for any
employer must be blocked, and it must be blocked *regardless* of how well it
scores on everything else.
"""

from __future__ import annotations

import pytest

from app.models import Application, JobPosting
from app.services import embeddings, quality_gate

SPECIFIC_LETTER = """\
Your engineering blog post on moving the commercial semantic model off
DirectQuery is the reason I'm writing. I spent the last year doing the same
migration on a manufacturing dataset, and cut refresh from 42 minutes to 9 by
removing 6 calculated columns and pushing the grain down to the fact table.

At Accenture I own the semantic layer for a platform serving 400 users, and
consolidated 14 legacy reports into 3 governed models. I mentor 2 analysts on
DAX performance patterns.

Outside work I shipped Act 1 of a game in Godot 4 and a React storefront - not
BI work, but it is where I learned to own a product end to end rather than a
report.

I would like to talk about the DirectQuery migration specifically.
"""

GENERIC_LETTER = """\
I am writing to apply for this exciting opportunity. I am a passionate data
professional with strong experience in business intelligence and analytics. I
thrive in fast-paced environments and enjoy leveraging data to drive business
outcomes.

I have strong stakeholder management skills and a proven track record of
delivering high-quality dashboards. I am confident I would be a great fit for
your dynamic team.

I would welcome the opportunity to discuss my application further.
"""


@pytest.fixture(autouse=True)
def lexical_backend(monkeypatch):
    backend = embeddings.LexicalBackend()
    monkeypatch.setattr(embeddings, "get_backend", lambda: backend)
    return backend


def make_application(
    session,
    owner,
    *,
    company="Acme",
    title="Senior Power BI Developer",
    letter="",
    meta=None,
    gap=None,
    match=None,
) -> Application:
    posting = JobPosting(
        user_id=owner.id,
        company=company,
        title=title,
        jd_text="Requirements:\nPower BI, DAX",
    )
    session.add(posting)
    session.flush()
    application = Application(
        user_id=owner.id,
        job_posting_id=posting.id,
        cover_letter_text=letter,
        tailoring_meta=meta or {},
        gap_report=gap or {},
        match_report=match or {},
    )
    session.add(application)
    session.commit()
    session.refresh(application)
    return application


GOOD_META = {
    "company_specific_details_used": [
        "engineering blog post on the DirectQuery migration",
        "commercial semantic model",
    ],
    "claims_avoided": ["Did not claim Fabric experience - the profile has none"],
    "keywords_incorporated": ["power bi", "dax", "semantic model"],
    "talking_points": [{"point": "Ran the same migration", "evidence": "42 -> 9 min refresh"}],
    "bullets": [
        {"company": "Accenture", "tailored": "Cut refresh from 42 to 9 minutes", "original": "x"},
        {"company": "Accenture", "tailored": "Consolidated 14 reports into 3 models", "original": "y"},
    ],
    "summary": "Owns the semantic layer",
    "cover_letter": SPECIFIC_LETTER,
}


# --------------------------------------------------------------------------
# Hard blocks
# --------------------------------------------------------------------------


def test_generic_letter_with_no_company_detail_is_blocked(session, user):
    application = make_application(
        session,
        user,
        letter=GENERIC_LETTER,
        meta={"company_specific_details_used": [], "claims_avoided": [], "bullets": []},
    )
    report = quality_gate.evaluate(session, application)
    assert report.verdict == "templated"
    assert report.blocks_ready
    assert "identically" in report.blocking_reason or "never names" in report.blocking_reason


def test_near_duplicate_letter_is_blocked_even_with_good_other_signals(session, user):
    """The headline behaviour: reusing a letter cannot be bought off with keywords."""
    make_application(session, user, company="Globex", letter=SPECIFIC_LETTER, meta=GOOD_META)
    second = make_application(
        session, user, company="Initech", letter=SPECIFIC_LETTER, meta=GOOD_META
    )
    report = quality_gate.evaluate(session, second)
    assert report.verdict == "templated"
    divergence = next(s for s in report.signals if s.name == "letter_divergence")
    assert divergence.verdict == "bad"
    assert "trending templated" in divergence.detail


def test_specific_letter_passes_on_the_first_application(session, user):
    application = make_application(session, user, letter=SPECIFIC_LETTER, meta=GOOD_META)
    report = quality_gate.evaluate(session, application)
    assert report.verdict in {"pass", "review"}
    assert not report.blocks_ready


def test_distinct_letters_are_not_flagged(session, user):
    make_application(session, user, company="Globex", letter=GENERIC_LETTER, meta=GOOD_META)
    second = make_application(session, user, company="Acme", letter=SPECIFIC_LETTER, meta=GOOD_META)
    report = quality_gate.evaluate(session, second)
    divergence = next(s for s in report.signals if s.name == "letter_divergence")
    assert divergence.verdict != "bad"


# --------------------------------------------------------------------------
# Individual signals
# --------------------------------------------------------------------------


def test_missing_letter_is_blocked(session, user):
    application = make_application(session, user, letter="", meta={})
    report = quality_gate.evaluate(session, application)
    assert report.verdict == "templated"


def test_honesty_signal_warns_when_gaps_are_unacknowledged(session, user):
    application = make_application(
        session,
        user,
        letter=SPECIFIC_LETTER,
        meta={**GOOD_META, "claims_avoided": []},
        gap={"honest_gaps": ["No Microsoft Fabric experience", "No people-management experience"]},
    )
    report = quality_gate.evaluate(session, application)
    honesty = next(s for s in report.signals if s.name == "honesty")
    assert honesty.verdict == "warn"
    assert "avoided" in honesty.detail


def test_honesty_signal_is_satisfied_when_claims_are_recorded(session, user):
    application = make_application(
        session,
        user,
        letter=SPECIFIC_LETTER,
        meta=GOOD_META,
        gap={"honest_gaps": ["No Microsoft Fabric experience"]},
    )
    report = quality_gate.evaluate(session, application)
    honesty = next(s for s in report.signals if s.name == "honesty")
    assert honesty.verdict == "good"


def test_jd_uptake_rewards_closing_gaps(session, user):
    application = make_application(
        session,
        user,
        letter=SPECIFIC_LETTER,
        meta=GOOD_META,
        match={"top_missing": ["dax", "semantic model", "row level security"]},
    )
    report = quality_gate.evaluate(session, application)
    uptake = next(s for s in report.signals if s.name == "jd_uptake")
    # Two of the three appear in the package.
    assert uptake.score > 50


def test_hand_edited_documents_are_scored_not_the_stale_metadata(session, user):
    """The gate re-runs after a manual edit. If it scored only the generator's
    record, hand-written improvements could never move the score."""
    application = make_application(
        session,
        user,
        letter="Acme's move to a governed semantic model is the migration I ran last year.",
        meta={},  # nothing generated - the user wrote this themselves
        match={"top_missing": ["semantic model", "dax", "row level security"]},
    )
    application.tailored_resume_md = (
        "## Experience\n- Built row level security across 3 regions\n"
        "- Optimised DAX measures, cutting refresh from 42 to 9 minutes\n"
    )
    session.commit()

    report = quality_gate.evaluate(session, application)
    uptake = next(s for s in report.signals if s.name == "jd_uptake")
    assert uptake.score > 60, uptake.detail

    bullets = next(s for s in report.signals if s.name == "bullet_divergence")
    assert "by hand" in bullets.detail


def test_substance_flags_a_letter_with_no_numbers(session, user):
    application = make_application(
        session, user, letter=GENERIC_LETTER, meta={**GOOD_META, "talking_points": []}
    )
    report = quality_gate.evaluate(session, application)
    substance = next(s for s in report.signals if s.name == "substance")
    assert substance.verdict in {"warn", "bad"}
    assert "no numbers" in substance.detail.lower()


def test_reused_bullets_are_detected(session, user):
    make_application(session, user, company="Globex", letter=GENERIC_LETTER, meta=GOOD_META)
    second = make_application(session, user, company="Acme", letter=SPECIFIC_LETTER, meta=GOOD_META)
    report = quality_gate.evaluate(session, second)
    bullets = next(s for s in report.signals if s.name == "bullet_divergence")
    # Identical bullets across both applications - flagged, but not fatal: your
    # best quantified bullets are *supposed* to recur.
    assert "near-identical" in bullets.detail


# --------------------------------------------------------------------------
# Shape
# --------------------------------------------------------------------------


def test_all_signals_are_present_and_weighted(session, user):
    application = make_application(session, user, letter=SPECIFIC_LETTER, meta=GOOD_META)
    report = quality_gate.evaluate(session, application)
    names = {s.name for s in report.signals}
    assert names == {
        "company_specificity",
        "letter_divergence",
        "bullet_divergence",
        "jd_uptake",
        "honesty",
        "substance",
    }
    assert all(s.weight > 0 for s in report.signals)
    assert all(0.0 <= s.score <= 100.0 for s in report.signals)


def test_report_serialises(session, user):
    application = make_application(session, user, letter=SPECIFIC_LETTER, meta=GOOD_META)
    payload = quality_gate.evaluate(session, application).as_dict()
    assert set(payload) >= {"score", "verdict", "signals", "compared_against", "blocking_reason"}


def test_score_stays_in_range(session, user):
    for letter, meta in ((SPECIFIC_LETTER, GOOD_META), (GENERIC_LETTER, {}), ("", {})):
        application = make_application(session, user, letter=letter, meta=meta)
        report = quality_gate.evaluate(session, application)
        assert 0.0 <= report.score <= 100.0
