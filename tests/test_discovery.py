"""Job discovery: ATS parsing, ingest lifecycle, ranking, and adoption.

Network calls are stubbed throughout. The adapters were verified against live
Greenhouse, Ashby and SmartRecruiters boards during development, but a test suite
that needs the internet fails for the wrong reasons.
"""

from __future__ import annotations

import pytest

from app.models import DiscoveredJob, JobSource
from app.services.discovery import ats, ingest, rank


# --------------------------------------------------------------------------
# HTML flattening
# --------------------------------------------------------------------------


def test_greenhouse_content_is_double_escaped():
    """Greenhouse sends `&lt;h2&gt;` in its JSON, so one unescape pass leaves
    literal tags behind - which `jd_match` would then score as keywords."""
    raw = "&amp;lt;h2&amp;gt;About us&amp;lt;/h2&amp;gt;&amp;lt;p&amp;gt;We build things.&amp;lt;/p&amp;gt;"
    text = ats.html_to_text(raw)
    assert "<" not in text and ">" not in text
    assert "About us" in text
    assert "We build things." in text


def test_list_markup_becomes_line_breaks():
    """A requirements list collapsed onto one line would destroy the section
    weighting that jd_match relies on."""
    text = ats.html_to_text("<ul><li>Power BI</li><li>DAX</li><li>SQL</li></ul>")
    assert [line for line in text.splitlines() if line.strip()] == ["Power BI", "DAX", "SQL"]


def test_nbsp_becomes_a_real_space():
    assert ats.html_to_text("Senior&nbsp;Developer") == "Senior Developer"


def test_blank_runs_are_collapsed():
    assert "\n\n\n" not in ats.html_to_text("<p>a</p><p></p><p></p><p></p><p>b</p>")


# --------------------------------------------------------------------------
# Adapters, against recorded payloads
# --------------------------------------------------------------------------

GREENHOUSE_PAYLOAD = {
    "jobs": [
        {
            "id": 8172508,
            "title": "Senior Power BI Developer",
            "absolute_url": "https://example.com/jobs/8172508",
            "company_name": "Acme",
            "location": {"name": "Remote - India"},
            "departments": [{"name": "Data"}],
            "first_published": "2026-09-03T13:32:53-04:00",
            "content": "&amp;lt;p&amp;gt;You will own the semantic model.&amp;lt;/p&amp;gt;",
        }
    ]
}


def test_greenhouse_adapter_normalises_a_posting(monkeypatch):
    monkeypatch.setattr(ats, "_get", lambda *a, **k: GREENHOUSE_PAYLOAD)
    posting = ats.fetch_greenhouse("acme")[0]

    assert posting.external_id == "8172508"
    assert posting.title == "Senior Power BI Developer"
    assert posting.company == "Acme"
    assert posting.department == "Data"
    assert posting.is_remote is True
    assert "semantic model" in posting.jd_text
    assert "<p>" not in posting.jd_text
    # Naive UTC, to match app.models.utcnow.
    assert posting.posted_at is not None and posting.posted_at.tzinfo is None


def test_ashby_adapter_prefers_plain_description(monkeypatch):
    payload = {
        "jobs": [
            {
                "id": "abc-123",
                "title": "Analytics Engineer",
                "jobUrl": "https://jobs.ashbyhq.com/acme/abc-123",
                "descriptionPlain": "dbt and Snowflake.",
                "descriptionHtml": "<p>should not be used</p>",
                "location": "Berlin",
                "department": "Data",
                "team": "Platform",
                "employmentType": "FullTime",
                "isRemote": False,
                "publishedAt": "2026-08-01T10:00:00Z",
            }
        ]
    }
    monkeypatch.setattr(ats, "_get", lambda *a, **k: payload)
    posting = ats.fetch_ashby("acme")[0]
    assert posting.jd_text == "dbt and Snowflake."
    assert posting.department == "Data / Platform"
    assert posting.is_remote is False


def test_ashby_skips_unlisted_postings(monkeypatch):
    payload = {"jobs": [{"id": "x", "title": "Hidden", "isListed": False}]}
    monkeypatch.setattr(ats, "_get", lambda *a, **k: payload)
    assert ats.fetch_ashby("acme") == []


def test_lever_rejects_an_unknown_board(monkeypatch):
    """Lever answers 200 with `{ok: false}` for a bad slug rather than a 404, so
    the shape has to be checked or the error surfaces as 'no jobs found'."""
    monkeypatch.setattr(ats, "_get", lambda *a, **k: {"ok": False, "error": "Document not found"})
    with pytest.raises(ats.ATSError, match="no public board"):
        ats.fetch_lever("nope")


def test_smartrecruiters_links_to_the_posting_not_the_api(monkeypatch):
    """`ref` is the API's self-link; using it sends the user to raw JSON."""
    payload = {
        "content": [
            {
                "id": "744000148454651",
                "name": "Data Consultant",
                "ref": "https://api.smartrecruiters.com/v1/companies/acme/postings/744000148454651",
                "location": {"city": "Warsaw", "country": "pl", "remote": True},
                "company": {"name": "Acme"},
            }
        ]
    }
    monkeypatch.setattr(ats, "_get", lambda *a, **k: payload)
    posting = ats.fetch_smartrecruiters("acme")[0]
    assert posting.url == "https://jobs.smartrecruiters.com/acme/744000148454651"
    assert "api.smartrecruiters.com" not in posting.url
    assert posting.is_remote is True


def test_unknown_ats_is_refused():
    with pytest.raises(ats.ATSError, match="Unknown ATS"):
        ats.fetch("monster", "acme")


def test_empty_token_is_refused():
    with pytest.raises(ats.ATSError, match="token is required"):
        ats.fetch("greenhouse", "   ")


# --------------------------------------------------------------------------
# Ingest lifecycle
# --------------------------------------------------------------------------


def _posting(external_id="1", title="Data Engineer", jd="Build pipelines with SQL."):
    return ats.Posting(
        external_id=external_id,
        title=title,
        url=f"https://example.com/{external_id}",
        jd_text=jd,
        company="Acme",
        location="Remote",
    )


@pytest.fixture
def source(session, user, monkeypatch):
    monkeypatch.setattr(ats, "fetch", lambda a, t: [_posting()])
    return ingest.add_source(session, user.id, ats_name="greenhouse", token="acme")


def test_adding_a_source_pulls_its_postings(source, session):
    assert source.company_name == "Acme"
    jobs = session.scalars(select_jobs(source)).all()
    assert len(jobs) == 1
    assert jobs[0].title == "Data Engineer"


def test_a_bad_token_is_rejected_before_the_source_is_saved(session, user, monkeypatch):
    """Otherwise a typo sits in the list looking healthy and never returns
    anything."""
    def boom(a, t):
        raise ats.ATSError("Greenhouse has no board with that token.")

    monkeypatch.setattr(ats, "fetch", boom)
    with pytest.raises(ats.ATSError):
        ingest.add_source(session, user.id, ats_name="greenhouse", token="typo")
    assert session.scalars(select_sources(user.id)).all() == []


def test_duplicate_source_is_refused(source, session, user, monkeypatch):
    monkeypatch.setattr(ats, "fetch", lambda a, t: [_posting()])
    with pytest.raises(ats.ATSError, match="already on your list"):
        ingest.add_source(session, user.id, ats_name="greenhouse", token="acme")


def test_refresh_adds_new_and_closes_vanished(source, session, user, monkeypatch):
    monkeypatch.setattr(ats, "fetch", lambda a, t: [_posting("2", "Analytics Engineer")])
    result = ingest.refresh(session, user.id)

    assert result.added == 1
    assert result.closed == 1
    jobs = {j.external_id: j for j in session.scalars(select_jobs(source))}
    assert jobs["1"].is_open is False  # retained, not deleted
    assert jobs["2"].is_open is True


def test_a_closed_posting_is_kept_not_deleted(source, session, user, monkeypatch):
    """An adopted role must not vanish from your history because the employer
    filled it."""
    monkeypatch.setattr(ats, "fetch", lambda a, t: [])
    ingest.refresh(session, user.id)
    assert len(session.scalars(select_jobs(source)).all()) == 1


def test_unchanged_posting_does_not_invalidate_its_score(source, session, user, monkeypatch):
    """Employers re-publish constantly with no real edit; re-embedding the board
    every refresh would make ranking unaffordable."""
    job = session.scalars(select_jobs(source)).one()
    job.is_deep_scored = True
    job.scored_at = job.first_seen_at
    session.commit()

    monkeypatch.setattr(ats, "fetch", lambda a, t: [_posting()])
    result = ingest.refresh(session, user.id)

    session.refresh(job)
    assert result.updated == 0
    assert job.is_deep_scored is True


def test_edited_posting_does_invalidate_its_score(source, session, user, monkeypatch):
    job = session.scalars(select_jobs(source)).one()
    job.is_deep_scored = True
    session.commit()

    monkeypatch.setattr(
        ats, "fetch", lambda a, t: [_posting(jd="Now wants Power BI and DAX too.")]
    )
    result = ingest.refresh(session, user.id)

    session.refresh(job)
    assert result.updated == 1
    assert job.is_deep_scored is False


def test_one_failing_board_does_not_abandon_the_others(session, user, monkeypatch):
    monkeypatch.setattr(ats, "fetch", lambda a, t: [_posting()])
    ingest.add_source(session, user.id, ats_name="greenhouse", token="good")
    ingest.add_source(session, user.id, ats_name="ashby", token="bad")

    def selective(ats_name, token):
        if token == "bad":
            raise ats.ATSError("board gone")
        return [_posting("9", "New Role")]

    monkeypatch.setattr(ats, "fetch", selective)
    result = ingest.refresh(session, user.id)

    assert result.sources_polled == 2
    assert result.added == 1
    assert len(result.errors) == 1
    assert not result.ok


def test_deleting_a_source_removes_its_postings(source, session, user):
    ingest.delete_source(session, source)
    assert session.scalars(select_sources(user.id)).all() == []
    assert session.query(DiscoveredJob).count() == 0


# --------------------------------------------------------------------------
# Ranking - the closeable-fit metric
# --------------------------------------------------------------------------


def test_profile_only_evidence_raises_closeable_above_current(session, user):
    """The metric nobody else computes: what the fit becomes once the résumé
    says what the profile already knows. No invention - this is evidence the
    candidate has and simply did not put on the page."""
    from app.services import jd_match

    report = jd_match.score(
        resume_text="Built reports in Excel for the finance team.",
        jd_text=(
            "Requirements\n"
            "- Strong Power BI and DAX skills\n"
            "- SQL for data modelling\n"
            "- Experience with semantic models\n"
        ),
        jd_title="Power BI Developer",
        extra_evidence="Power BI, DAX, SQL, semantic model design",
    )
    current, closeable = rank._split_keyword_scores(report)
    assert closeable > current, "profile evidence must lift the closeable score"


def test_current_score_ignores_profile_only_matches(session, user):
    from app.services import jd_match

    report = jd_match.score(
        resume_text="Nothing relevant here.",
        jd_text="Requirements\n- Power BI\n- DAX\n",
        extra_evidence="Power BI, DAX",
    )
    current, closeable = rank._split_keyword_scores(report)
    assert current == 0.0
    assert closeable > 0.0


def test_split_scores_handle_a_jd_with_no_terms():
    from app.services import jd_match

    report = jd_match.score(resume_text="x", jd_text="")
    assert rank._split_keyword_scores(report) == (0.0, 0.0)


def test_prefilter_rewards_a_matching_title():
    job = DiscoveredJob(
        title="Senior Power BI Developer", jd_text="Own the semantic layer.", department=""
    )
    # A vocabulary the posting only partly covers, so the title bonus has
    # somewhere to go. With a single term that the title already contains,
    # coverage is pegged at 100 and the clamp hides the effect.
    vocabulary = {"power bi", "airflow", "kubernetes", "terraform"}
    with_title = rank._prefilter_score(job, vocabulary, {"senior power bi developer"})
    without = rank._prefilter_score(job, vocabulary, set())
    assert with_title > without


def test_prefilter_is_clamped_to_100():
    job = DiscoveredJob(title="Power BI Developer", jd_text="Power BI", department="")
    assert rank._prefilter_score(job, {"power bi"}, {"power bi developer"}) == 100.0


def test_prefilter_matches_title_words_out_of_order():
    """Real postings say 'Senior Developer, Power BI', not the target title
    verbatim."""
    job = DiscoveredJob(title="Senior Developer, Power BI", jd_text="", department="")
    assert rank._prefilter_score(job, set(), {"power bi developer"}) > 0


def test_prefilter_is_zero_for_an_empty_posting():
    assert rank._prefilter_score(DiscoveredJob(title="", jd_text="", department=""), {"sql"}, set()) == 0.0


def test_ranking_marks_estimates_distinctly(session, user, source, monkeypatch):
    """A prefilter-only number must never be presented as a real score."""
    monkeypatch.setattr(rank, "DEEP_LIMIT", 0)
    result = rank.rank(session, user.id, deep_limit=0)
    job = session.scalars(select_jobs(source)).one()
    assert result.considered == 1
    assert result.deep_scored == 0
    assert job.is_deep_scored is False


def test_ranking_skips_dismissed_jobs(session, user, source):
    job = session.scalars(select_jobs(source)).one()
    job.dismissed = True
    session.commit()
    assert rank.rank(session, user.id).considered == 0


def test_ranking_an_empty_corpus_is_harmless(session, user):
    assert rank.rank(session, user.id).considered == 0


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def select_jobs(source):
    from sqlalchemy import select

    return select(DiscoveredJob).where(DiscoveredJob.source_id == source.id)


def select_sources(user_id):
    from sqlalchemy import select

    return select(JobSource).where(JobSource.user_id == user_id)


def test_no_uploaded_resume_is_flagged_rather_than_reported_as_zero_uplift(
    session, user, source
):
    """With no résumé the fallback is generated from the profile, so it already
    contains every skill and the uplift is pinned at zero by construction.
    Presenting that as "tailoring gains you nothing" would be a lie about the
    feature rather than a fact about the job.
    """
    text, is_real = rank._master_resume_text(session, user.id, None)
    assert is_real is False

    rank.rank(session, user.id, deep_limit=1)
    job = session.scalars(select_jobs(source)).one()
    assert (job.match_report or {}).get("has_real_resume") is False


def test_an_uploaded_resume_is_used_and_marked_real(session, user, source):
    from app.models import ResumeVariant

    session.add(
        ResumeVariant(
            user_id=user.id, label="Real CV", content_md="Power BI and DAX only.",
            raw_text="Power BI and DAX only.", is_master=True, ats_score=80.0,
        )
    )
    session.commit()

    text, is_real = rank._master_resume_text(session, user.id, None)
    assert is_real is True
    assert "Power BI and DAX only." in text


def test_uplift_appears_when_the_profile_knows_more_than_the_resume():
    """The differentiator, end to end through the scorer: evidence the candidate
    genuinely has but did not put on the page lifts the closeable score."""
    from app.services import jd_match

    report = jd_match.score(
        resume_text="Senior Power BI Developer. Dashboards in Power BI and DAX.",
        jd_text="Requirements\n- Power BI and DAX\n- dbt\n- Snowflake\n- Airflow\n",
        extra_evidence="Power BI, DAX, dbt, Snowflake, Airflow",
    )
    current, closeable = rank._split_keyword_scores(report)
    profile_only = [
        h.term for h in report.found if rank.PROFILE_ONLY_MARKER in h.matched_as
    ]

    assert closeable > current
    assert {"dbt", "snowflake", "airflow"} <= set(profile_only)


def test_the_profile_only_marker_matches_what_jd_match_writes():
    """`jd_match` writes 'profile only - not on the résumé'. The marker here is
    the ASCII prefix on purpose, so an encoding difference in the accented word
    cannot silently break the partition the whole metric rests on."""
    from app.services import jd_match

    report = jd_match.score(
        resume_text="nothing", jd_text="Requirements\n- Power BI\n", extra_evidence="Power BI"
    )
    hits = [h for h in report.found if h.matched_as]
    assert hits, "expected a profile-only hit"
    assert any(rank.PROFILE_ONLY_MARKER in h.matched_as for h in hits)
    assert rank.PROFILE_ONLY_MARKER.isascii()
