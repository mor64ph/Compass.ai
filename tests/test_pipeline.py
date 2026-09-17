"""Tests for the deterministic half of the application pipeline: creation, the
stage machine, funnel analytics, export, and the schema hardener.

Nothing here calls the Anthropic API - these all have to work with no API key.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.llm.schema import strict_json_schema
from app.models import Application, ApplicationEvent, JobPosting, SourceType, Stage
from app.schemas import JDInput, LLMCareerProfile, LLMGapReport, LLMTailoredPackage
from app.services import application_service, embeddings, export, profile_service

JD_TEXT = """\
Senior Power BI Developer

Requirements
- 5+ years with Power BI and strong DAX
- Dimensional modelling and star schema design
- Advanced SQL

Responsibilities
- Own the semantic model
- Mentor junior analysts
"""


@pytest.fixture(autouse=True)
def lexical_backend(monkeypatch):
    backend = embeddings.LexicalBackend()
    monkeypatch.setattr(embeddings, "get_backend", lambda: backend)
    return backend


# --------------------------------------------------------------------------
# Creation
# --------------------------------------------------------------------------


def test_pasted_jd_is_always_marked_user_pasted(session, user):
    """Provenance must be honest: this path is the user reading a page in their
    own browser, and nothing else."""
    application = application_service.create_from_jd(
        session, user.id, JDInput(company="Acme", title="Senior Power BI Developer", jd_text=JD_TEXT)
    )
    assert application.job_posting.source_type == SourceType.USER_PASTED.value
    assert application.job_posting.source_name == "user"
    assert application.stage == Stage.SAVED.value


def test_creation_logs_an_event(session, user):
    application = application_service.create_from_jd(
        session, user.id, JDInput(company="Acme", jd_text=JD_TEXT)
    )
    assert len(application.events) == 1
    assert application.events[0].source == "manual"


def test_scoring_works_without_a_resume_upload(session, user):
    """Falls back to the profile-generated résumé, so a new user can score a JD
    before uploading anything."""
    profile = profile_service.get_or_create_profile(session, user.id)
    profile.full_name = "Test User"
    profile.summary = "Power BI developer working on DAX and semantic models"
    session.commit()

    application = application_service.create_from_jd(
        session, user.id, JDInput(company="Acme", jd_text=JD_TEXT)
    )
    report = application_service.run_match(session, application)
    assert report.composite >= 0.0
    assert application.match_report["keyword_score"] >= 0.0


# --------------------------------------------------------------------------
# Stage machine
# --------------------------------------------------------------------------


def make_application(session, user, *, company="Acme", stage=Stage.SAVED.value) -> Application:
    posting = JobPosting(
        user_id=user.id, company=company, title="BI Dev", jd_text=JD_TEXT
    )
    session.add(posting)
    session.flush()
    application = Application(
        user_id=user.id, job_posting_id=posting.id, stage=stage
    )
    session.add(application)
    session.commit()
    session.refresh(application)
    return application


def test_manual_stage_change_always_applies(session, user):
    application = make_application(session, user, stage=Stage.INTERVIEW.value)
    # Backwards is allowed manually - the user's word beats inference.
    assert application_service.set_stage(session, application, Stage.APPLIED.value)
    assert application.stage == Stage.APPLIED.value


def test_applied_at_is_stamped_once(session, user):
    application = make_application(session, user)
    application_service.set_stage(session, application, Stage.APPLIED.value)
    first = application.applied_at
    assert first is not None
    application_service.set_stage(session, application, Stage.SCREEN.value)
    application_service.set_stage(session, application, Stage.APPLIED.value)
    assert application.applied_at == first


def test_inferred_stage_change_is_forward_only(session, user):
    application = make_application(session, user, stage=Stage.INTERVIEW.value)
    moved = application_service.advance_stage(
        session, application, Stage.APPLIED.value, source="gmail", summary="confirmation"
    )
    assert not moved
    assert application.stage == Stage.INTERVIEW.value


def test_inferred_stage_change_moves_forward(session, user):
    application = make_application(session, user, stage=Stage.APPLIED.value)
    assert application_service.advance_stage(
        session, application, Stage.INTERVIEW.value, source="gmail", summary="invite"
    )
    assert application.stage == Stage.INTERVIEW.value


def test_rejection_closes_from_any_stage(session, user):
    application = make_application(session, user, stage=Stage.SCREEN.value)
    assert application_service.advance_stage(
        session, application, Stage.CLOSED.value, source="gmail", summary="rejection"
    )
    assert application.stage == Stage.CLOSED.value


def test_closed_applications_are_not_reopened_by_inference(session, user):
    """A stray marketing email must not resurrect a rejection."""
    application = make_application(session, user, stage=Stage.CLOSED.value)
    moved = application_service.advance_stage(
        session, application, Stage.INTERVIEW.value, source="gmail", summary="alert"
    )
    assert not moved
    assert application.stage == Stage.CLOSED.value


def test_closed_is_not_in_the_progression_ranking(session, user):
    """If `closed` ranked highest it would win every forward comparison."""
    assert Stage.CLOSED.value not in application_service.STAGE_RANK


def test_unknown_stage_is_rejected(session, user):
    application = make_application(session, user)
    assert not application_service.set_stage(session, application, "interviewing")
    assert application.stage == Stage.SAVED.value


# --------------------------------------------------------------------------
# Ready flag / gate interaction
# --------------------------------------------------------------------------


def test_templated_verdict_blocks_ready_without_a_reason(session, user):
    application = make_application(session, user)
    application.quality_verdict = "templated"
    session.commit()
    assert not application_service.mark_ready(session, application)
    assert not application.is_ready


def test_override_requires_a_written_reason_and_is_recorded(session, user):
    application = make_application(session, user)
    application.quality_verdict = "templated"
    session.commit()

    assert not application_service.mark_ready(session, application, override_reason="   ")
    assert application_service.mark_ready(
        session, application, override_reason="Sister company, same role"
    )
    assert application.is_ready
    assert application.gate_override_reason == "Sister company, same role"
    assert any("overridden" in e.summary for e in application.events)


def test_passing_verdict_needs_no_reason(session, user):
    application = make_application(session, user)
    application.quality_verdict = "pass"
    session.commit()
    assert application_service.mark_ready(session, application)


# --------------------------------------------------------------------------
# Analytics
# --------------------------------------------------------------------------


def test_staleness_uses_per_stage_thresholds(session, user):
    fresh = make_application(session, user, company="Fresh", stage=Stage.APPLIED.value)
    stale = make_application(session, user, company="Stale", stage=Stage.OFFER.value)

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    fresh.last_activity_at = now - timedelta(days=5)   # applied threshold is 14
    stale.last_activity_at = now - timedelta(days=5)   # offer threshold is 3
    session.commit()

    stale_ids = {a.id for a, _ in application_service.stale_applications(session, user.id)}
    assert stale.id in stale_ids
    assert fresh.id not in stale_ids


def test_closed_applications_are_never_stale(session, user):
    application = make_application(session, user, stage=Stage.CLOSED.value)
    application.last_activity_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=400)
    session.commit()
    assert application.id not in {a.id for a, _ in application_service.stale_applications(session, user.id)}


def test_funnel_counts_stages_reached_not_just_current(session, user):
    """A rejection after an interview must still count as an interview reached,
    otherwise the interview rate silently under-reports."""
    application = make_application(session, user, stage=Stage.APPLIED.value)
    application_service.set_stage(session, application, Stage.INTERVIEW.value)
    application_service.set_stage(session, application, Stage.CLOSED.value)

    funnel = application_service.funnel(session, user.id)
    assert funnel["interview"] == 1
    assert funnel["applied"] == 1


def test_funnel_segments_by_quality_band(session, user):
    high = make_application(session, user, company="High", stage=Stage.INTERVIEW.value)
    low = make_application(session, user, company="Low", stage=Stage.APPLIED.value)
    high.quality_score = 88.0
    low.quality_score = 40.0
    session.commit()

    bands = application_service.funnel(session, user.id)["by_quality_band"]
    assert bands["high (75+)"]["count"] == 1
    assert bands["low (<55)"]["count"] == 1


def test_funnel_on_empty_db_does_not_divide_by_zero(session, user):
    funnel = application_service.funnel(session, user.id)
    assert funnel["total"] == 0
    assert funnel["interview_rate"] == 0.0


def test_gmail_events_are_unique_per_message(session, user):
    """The (source, external_id) constraint is what makes re-running a sync safe."""
    from sqlalchemy.exc import IntegrityError

    application = make_application(session, user)
    session.add(
        ApplicationEvent(
            application_id=application.id, kind="email", source="gmail", external_id="msg-1"
        )
    )
    session.commit()
    session.add(
        ApplicationEvent(
            application_id=application.id, kind="email", source="gmail", external_id="msg-1"
        )
    )
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


# --------------------------------------------------------------------------
# Export
# --------------------------------------------------------------------------


def test_markdown_parses_into_blocks():
    blocks = export.parse_blocks("# Name\n## Summary\nText here\n- bullet one\n- bullet two")
    assert blocks == [
        ("h1", "Name"),
        ("h2", "Summary"),
        ("para", "Text here"),
        ("bullet", "bullet one"),
        ("bullet", "bullet two"),
    ]


def test_safe_filename_strips_path_characters():
    name = export.safe_filename("Hrisit Biswas", "Acme/Corp: BI", "..\\..\\etc")
    assert "/" not in name and "\\" not in name and ":" not in name
    assert name


def test_docx_export_round_trips(session, tmp_path, monkeypatch):
    pytest.importorskip("docx")
    from app.config import get_settings

    monkeypatch.setattr(type(get_settings()), "export_dir", property(lambda self: tmp_path))
    path = export.to_docx("# Jane\n## Summary\nBI developer\n- Did a thing", "test_cv")
    assert path.is_file() and path.suffix == ".docx"

    import docx

    text = "\n".join(p.text for p in docx.Document(str(path)).paragraphs)
    assert "Jane" in text and "Did a thing" in text


def test_pdf_export_produces_a_file(session, tmp_path, monkeypatch):
    pytest.importorskip("reportlab")
    from app.config import get_settings

    monkeypatch.setattr(type(get_settings()), "export_dir", property(lambda self: tmp_path))
    path = export.to_pdf("# Jane\n## Summary\nBI developer\n- Did a thing", "test_cv")
    assert path.is_file()
    assert path.read_bytes().startswith(b"%PDF")


def test_pdf_export_escapes_markup():
    """A résumé containing '<' must not corrupt the reportlab paragraph markup."""
    assert export._inline("C++ & <script>") == "C++ &amp; &lt;script&gt;"


ROUND_TRIP_MARKDOWN = """\
# Hrisit Biswas
Senior Power BI Developer - semantic modelling and DAX
Bangalore, India | +91 98765 43210 | hrisit@example.com | github.com/example

## Summary
Senior BI developer who owns the semantic layer for a manufacturing analytics
platform, having moved from building reports to owning the model beneath them.

## Skills
**Hard:** DAX, SQL, dimensional modelling, row level security, PySpark

## Experience
### Senior Power BI Analyst, Accenture (Bangalore)
Apr 2023 - Present
- Cut model refresh time from 42 minutes to 9 minutes by removing 6 calculated columns
- Consolidated 14 legacy reports into 3 governed semantic models used by 400 people
- Rebuilt the KPI layer so 22 metrics are defined once instead of restated per report
- Mentored 2 analysts on DAX performance patterns and set up the model review process
- Implemented row-level security covering 3 regional groups

### Data Engineer, Previous Co (Bangalore)
Jun 2020 - Mar 2023
- Built PySpark pipelines on Databricks processing 1.2 TB per day
- Reduced pipeline failures by 60% by adding schema validation at ingestion
- Migrated 9 legacy SSIS packages to Delta Lake without downtime

## Projects
### Ashvale
A fan-made, IP-safe game built in Godot 4. Act 1 shipped with a save system.

## Certifications
- Microsoft Certified: Power BI Data Analyst Associate - Microsoft

## Education
- B.Tech, Computer Science - Example University (2020)
"""


@pytest.mark.parametrize("fmt", ["pdf", "docx"])
def test_exported_file_passes_our_own_ats_checker(fmt, tmp_path, monkeypatch):
    """Export, re-read the written file, re-check it.

    Compass controls this artefact end to end, so it must be impossible to
    produce a file its own checker would fail. This caught two real bugs: PDF
    bullets drawn as separate glyphs vanished from the extracted text, and DOCX
    list-styled bullets are invisible to `paragraph.text` - both made the export
    trip the "written as prose, no bullets" rule.
    """
    pytest.importorskip("reportlab" if fmt == "pdf" else "docx")
    pytest.importorskip("pdfplumber" if fmt == "pdf" else "docx")

    from app.config import get_settings
    from app.services import ats_check, text_extract

    monkeypatch.setattr(type(get_settings()), "export_dir", property(lambda self: tmp_path))

    baseline = ats_check.check(
        text_extract.ExtractedDoc(text=ROUND_TRIP_MARKDOWN, kind="txt", filename="src.md")
    )

    writer = export.to_pdf if fmt == "pdf" else export.to_docx
    path = writer(ROUND_TRIP_MARKDOWN, "round_trip")
    doc = text_extract.extract(path, original_filename=path.name)
    assert doc.ok, doc.error

    report = ats_check.check(doc)
    severe = [f.rule for f in report.findings if f.severity in {"critical", "high"}]
    assert not severe, f"{fmt} export produced severe findings: {severe}"

    # All the structural sections must survive the round trip.
    for section in ("experience", "education", "skills", "summary", "projects"):
        assert section in report.sections_found, (fmt, report.sections_found)

    # And it must not score materially worse than the markdown it came from.
    assert report.score >= baseline.score - 3, (
        f"{fmt}: {report.score} vs source {baseline.score}; "
        f"findings={[f.rule for f in report.findings]}"
    )


# --------------------------------------------------------------------------
# Structured-output schema hardening
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "model", [LLMCareerProfile, LLMGapReport, LLMTailoredPackage]
)
def test_llm_schemas_harden_cleanly(model):
    schema = strict_json_schema(model)
    _assert_strict(schema)
    assert "$defs" not in schema
    assert "$ref" not in repr(schema)


def test_every_model_field_survives_hardening():
    """Regression: the metadata strip ran over *every* dict key at every depth,
    so a field genuinely named `title` was deleted from the schema. The model
    never saw it, omitted it, and validation then rejected the response - on
    every provider. The old consistency check passed because `required` was
    derived from the already-stripped properties, so both were wrong together.
    """
    from pydantic import BaseModel

    class Inner(BaseModel):
        title: str
        default: str
        examples: str
        normal: str

    class Outer(BaseModel):
        title: str
        rows: list[Inner]

    schema = strict_json_schema(Outer)
    assert set(schema["properties"]) == {"title", "rows"}
    assert set(schema["required"]) == {"title", "rows"}

    inner = schema["properties"]["rows"]["items"]
    # All four survive, including the three whose names collide with JSON Schema
    # keywords.
    assert set(inner["properties"]) == {"title", "default", "examples", "normal"}
    assert set(inner["required"]) == {"title", "default", "examples", "normal"}


def test_schema_metadata_is_still_stripped():
    """The strip must still do its job in keyword position - `title` as Pydantic
    field metadata is noise, `title` as a field name is data."""
    from pydantic import BaseModel, Field

    class Thing(BaseModel):
        name: str = Field(description="a name")

    schema = strict_json_schema(Thing)
    # Pydantic emits a model-level "title": "Thing" and per-field titles.
    assert "title" not in schema
    assert "title" not in schema["properties"]["name"]
    # Descriptions are useful to the model, so they are kept.
    assert schema["properties"]["name"]["description"] == "a name"


@pytest.mark.parametrize(
    "model_name,field_path",
    [
        ("LLMCareerProfile", ("experiences", "title")),
        ("LLMPrepBrief", ("star_stories", "title")),
    ],
)
def test_real_models_keep_their_title_fields(model_name, field_path):
    """The two that actually broke against a live provider."""
    import app.schemas as schemas

    model = getattr(schemas, model_name)
    schema = strict_json_schema(model)
    array_field, nested_field = field_path
    item = schema["properties"][array_field]["items"]
    assert nested_field in item["properties"], f"{model_name}.{array_field}[].{nested_field}"
    assert nested_field in item["required"]


def _assert_strict(node) -> None:
    if isinstance(node, list):
        for item in node:
            _assert_strict(item)
        return
    if not isinstance(node, dict):
        return
    if node.get("type") == "object" or "properties" in node:
        props = node.get("properties", {})
        assert node.get("additionalProperties") is False
        assert set(node.get("required", [])) == set(props)
    for value in node.values():
        _assert_strict(value)


def test_recursive_models_are_rejected_loudly():
    """Better a clear error at build time than an infinite schema at request time."""
    from pydantic import BaseModel

    class Node(BaseModel):
        name: str
        children: list["Node"]

    Node.model_rebuild()
    with pytest.raises(ValueError, match="Recursive"):
        strict_json_schema(Node)


def test_prompt_rendering_survives_literal_braces():
    """Prompt files contain JSON examples; `.format` would choke on every brace."""
    from app.llm.client import render

    out = render('Schema: {"a": 1}\nProfile: {{profile}}', profile={"name": "x"})
    assert '{"a": 1}' in out
    assert '"name": "x"' in out
