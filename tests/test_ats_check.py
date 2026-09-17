"""Tests for the deterministic ATS checker.

These exist because the checker is the one part of Compass that must give the
same answer every time — it is the gate that runs before a human ever sees the
résumé, and a rule that silently stops firing is invisible in normal use.
"""

from __future__ import annotations

import pytest

from app.services import ats_check
from app.services.text_extract import ExtractedDoc, PageFacts

# Deliberately realistic in length (~300 words). The `too_short` rule fires
# below 250, which is correct for a real résumé - so a toy fixture would trip a
# rule that has nothing to do with what each test is checking.
CLEAN_RESUME = """\
Hrisit Biswas
Senior Power BI Developer - semantic modelling and DAX
Bangalore, India | +91 98765 43210 | hrisit@example.com | github.com/example

Summary
Senior BI developer who owns the semantic layer for a manufacturing analytics
platform. Moved from building reports to owning the model that everything else
sits on, and now sets the standards other analysts build against.

Skills
Power BI, DAX, Power Query, Databricks, PySpark, SQL, dimensional modelling,
row-level security, VertiPaq Analyzer, DAX Studio, Tabular Editor, Delta Lake

Experience
Senior Power BI Analyst, Accenture
Apr 2023 - Present
- Cut model refresh time from 42 minutes to 9 minutes by removing 6 calculated
  columns and pushing the grain down to the fact table
- Consolidated 14 legacy reports into 3 governed semantic models used by
  400 people across 5 business units
- Rebuilt the KPI layer so 22 metrics are defined once instead of restated in
  every report, cutting reconciliation queries from the finance team by half
- Mentored 2 analysts on DAX performance patterns and set up the review process
  the team now uses before any model ships
- Implemented row-level security covering 3 regional groups

Data Engineer, Previous Co
Jun 2020 - Mar 2023
- Built PySpark pipelines on Databricks processing 1.2 TB per day
- Reduced pipeline failures by 60% by adding schema validation at ingestion
- Migrated 9 legacy SSIS packages to Delta Lake without downtime
- Documented the medallion layout that became the team standard

Projects
Ashvale
A fan-made, IP-safe game built in Godot 4. Act 1 shipped, with its own design
document and a save system written from scratch.

Temple commerce prototype
React and Tailwind storefront for Tamil Nadu temple goods. Shipped a working
catalogue and cart.

Certifications
- Microsoft Certified: Power BI Data Analyst Associate - Microsoft

Education
B.Tech, Computer Science - Example University (2020)
"""


def make_doc(text: str, **kwargs) -> ExtractedDoc:
    return ExtractedDoc(text=text, kind=kwargs.pop("kind", "txt"), filename="cv.txt", **kwargs)


def rules(report: ats_check.ATSReport) -> set[str]:
    return {finding.rule for finding in report.findings}


# --------------------------------------------------------------------------
# Happy path
# --------------------------------------------------------------------------


def test_clean_resume_scores_well():
    report = ats_check.check(make_doc(CLEAN_RESUME))
    assert report.score >= 85, report.as_dict()["findings"]
    assert report.grade in {"Clean", "Minor issues"}


def test_clean_resume_finds_all_required_sections():
    report = ats_check.check(make_doc(CLEAN_RESUME))
    for section in ats_check.REQUIRED_SECTIONS:
        assert section in report.sections_found


def test_clean_resume_has_no_critical_findings():
    report = ats_check.check(make_doc(CLEAN_RESUME))
    assert not [f for f in report.findings if f.severity == "critical"]


# --------------------------------------------------------------------------
# Contact details
# --------------------------------------------------------------------------


def test_missing_email_is_critical():
    text = CLEAN_RESUME.replace("hrisit@example.com", "")
    report = ats_check.check(make_doc(text))
    assert "no_email" in rules(report)
    assert any(f.severity == "critical" for f in report.findings if f.rule == "no_email")


def test_missing_phone_is_flagged():
    text = CLEAN_RESUME.replace("+91 98765 43210", "")
    assert "no_phone" in rules(ats_check.check(make_doc(text)))


@pytest.mark.parametrize(
    "phone",
    [
        "+91 98765 43210",   # Indian mobile, 5+5 grouping
        "+91-98765-43210",
        "9876543210",
        "+1 (555) 123-4567",
        "555-123-4567",
        "+44 20 7946 0958",
    ],
)
def test_phone_formats_are_recognised(phone: str):
    """A single shape-matching regex cannot cover national formats; detection is
    by digit count instead. Regression: the 5+5 Indian grouping was missed."""
    from app.services.textutil import find_phones

    assert find_phones(f"Bangalore | {phone} | jane@example.com"), phone


@pytest.mark.parametrize("not_a_phone", ["2020 - 2023", "1.2 TB per day", "42 minutes to 9"])
def test_non_phones_are_not_matched(not_a_phone: str):
    from app.services.textutil import find_phones

    assert not find_phones(not_a_phone), not_a_phone


@pytest.mark.parametrize(
    "link", ["github.com/jane", "https://github.com/jane", "www.example.dev", "jane.me/cv"]
)
def test_bare_domains_count_as_links(link: str):
    """Résumés routinely write a domain with no scheme and no www."""
    assert ats_check.URL_RE.search(link), link


def test_an_email_domain_is_not_counted_as_a_link():
    text = "Jane Doe\njane@example.com\n+91 98765 43210"
    assert "no_links" in rules(ats_check.check(make_doc(text)))


def test_years_alone_do_not_count_as_quantification():
    from app.services.textutil import has_metrics

    assert not has_metrics("Analyst, Somewhere. Jan 2020 - Present. +91 90000 00000")
    assert has_metrics("Cut refresh from 42 minutes to 9 minutes")
    assert has_metrics("Reduced failures by 60%")


def test_contact_in_pdf_header_is_flagged():
    """A parser that treats the top band as a repeating header drops it - which
    leaves an application with no way to reach you."""
    page = PageFacts(
        index=0, width=595, height=842, char_count=2000, word_count=400,
        ruled_table_count=0, image_count=0,
        header_text="hrisit@example.com +91 98765 43210",
    )
    report = ats_check.check(make_doc(CLEAN_RESUME, kind="pdf", pages=[page]))
    assert "contact_in_header" in rules(report)


# --------------------------------------------------------------------------
# Layout
# --------------------------------------------------------------------------


def test_two_column_pdf_is_detected():
    page = PageFacts(
        index=0, width=595, height=842, char_count=2000, word_count=400,
        ruled_table_count=0, image_count=0,
        gutter_width_ratio=0.12, gutter_center_ratio=0.35,
    )
    report = ats_check.check(make_doc(CLEAN_RESUME, kind="pdf", pages=[page]))
    assert "multi_column" in rules(report)


def test_narrow_gutter_is_not_treated_as_two_columns():
    """Ordinary ragged-right whitespace must not trip the column rule."""
    page = PageFacts(
        index=0, width=595, height=842, char_count=2000, word_count=400,
        ruled_table_count=0, image_count=0,
        gutter_width_ratio=0.04, gutter_center_ratio=0.5,
    )
    report = ats_check.check(make_doc(CLEAN_RESUME, kind="pdf", pages=[page]))
    assert "multi_column" not in rules(report)


def test_gutter_at_page_edge_is_not_a_column():
    page = PageFacts(
        index=0, width=595, height=842, char_count=2000, word_count=400,
        ruled_table_count=0, image_count=0,
        gutter_width_ratio=0.15, gutter_center_ratio=0.9,  # right margin, not a gutter
    )
    report = ats_check.check(make_doc(CLEAN_RESUME, kind="pdf", pages=[page]))
    assert "multi_column" not in rules(report)


def test_docx_textbox_is_critical():
    report = ats_check.check(
        make_doc(CLEAN_RESUME, kind="docx", docx_textbox_count=2, docx_uses_heading_styles=True)
    )
    findings = {f.rule: f for f in report.findings}
    assert "docx_textbox" in findings
    assert findings["docx_textbox"].severity == "critical"


def test_docx_tables_and_columns_are_flagged():
    report = ats_check.check(
        make_doc(
            CLEAN_RESUME, kind="docx",
            docx_table_count=3, docx_column_count=2, docx_uses_heading_styles=True,
        )
    )
    assert {"docx_tables", "docx_columns"} <= rules(report)


def test_pdf_tables_and_images_are_flagged():
    page = PageFacts(
        index=0, width=595, height=842, char_count=2000, word_count=400,
        ruled_table_count=2, image_count=1,
    )
    report = ats_check.check(make_doc(CLEAN_RESUME, kind="pdf", pages=[page]))
    assert {"pdf_tables", "pdf_images"} <= rules(report)


# --------------------------------------------------------------------------
# Text layer
# --------------------------------------------------------------------------


def test_scanned_pdf_scores_zero_ish():
    page = PageFacts(
        index=0, width=595, height=842, char_count=12, word_count=2,
        ruled_table_count=0, image_count=1,
    )
    report = ats_check.check(make_doc("Hrisit", kind="pdf", pages=[page]))
    assert "no_text_layer" in rules(report)
    assert report.score < 40
    assert report.grade == "High rejection risk"


def test_unreadable_file_scores_zero():
    doc = ExtractedDoc(
        text="", kind="pages", filename="cv.pages", ok=False, error="Unsupported file type"
    )
    report = ats_check.check(doc)
    assert report.score == 0.0
    assert rules(report) == {"file_unreadable"}


# --------------------------------------------------------------------------
# Glyphs
# --------------------------------------------------------------------------


def test_icon_font_glyphs_are_flagged():
    text = CLEAN_RESUME.replace("hrisit@example.com", " hrisit@example.com")
    assert "icon_font_glyphs" in rules(ats_check.check(make_doc(text)))


def test_ligatures_are_flagged():
    """'identiﬁed' is one glyph, so no keyword search will ever match it."""
    text = CLEAN_RESUME + "\n- Identiﬁed and ﬁxed 12 broken measures"
    assert "ligatures" in rules(ats_check.check(make_doc(text)))


# --------------------------------------------------------------------------
# Content
# --------------------------------------------------------------------------


def test_weak_verbs_are_flagged():
    text = CLEAN_RESUME.replace(
        "- Cut model refresh time", "- Responsible for model refresh time"
    )
    assert "weak_verbs" in rules(ats_check.check(make_doc(text)))


def test_no_quantification_is_flagged():
    """The bullets here are all duties with no outcome. The employment date and
    the phone number must not be mistaken for metrics - that is exactly the
    false negative that would stop this rule ever firing."""
    text = """\
Jane Doe
Business Intelligence Analyst
Bangalore, India | jane@example.com | +91 90000 00000 | github.com/jane

Summary
Business intelligence analyst working on reporting and dashboards for the sales
and finance functions. Comfortable across the reporting stack and used to
working directly with business stakeholders on their requirements.

Skills
Power BI, DAX, SQL, Excel, Power Query, data visualisation, stakeholder
management, documentation

Experience
Analyst, Somewhere
Jan 2020 - Present
- Built dashboards for the sales team
- Maintained the data model and the underlying dataset refreshes
- Worked with stakeholders on requirements and change requests
- Supported the month end reporting cycle
- Documented the semantic layer for the wider team
- Contributed to the reporting standards discussion
- Responded to ad hoc data requests from the commercial team
- Helped onboard new joiners onto the reporting stack

Junior Analyst, Elsewhere
Jul 2018 - Dec 2019
- Produced recurring operational reports for the logistics function
- Maintained spreadsheets used by the planning team
- Assisted with data quality checks before each reporting cycle
- Prepared slides summarising weekly performance for the leadership review
- Supported the migration of legacy reports onto the new platform

Education
B.Tech, Information Technology - Example University (2018)
"""
    report = ats_check.check(make_doc(text))
    assert "no_quantification" in rules(report)
    # And the noise rules must not fire - otherwise this test passes for the
    # wrong reason.
    assert "near_empty" not in rules(report)
    assert "no_phone" not in rules(report)


def test_unparseable_dates_are_flagged():
    text = CLEAN_RESUME.replace("Apr 2023 - Present", "recently").replace(
        "Jun 2020 - Mar 2023", "a while ago"
    )
    # The education line still carries "(2020)" but that is not a range.
    assert "unparseable_dates" in rules(ats_check.check(make_doc(text)))


@pytest.mark.parametrize(
    "span",
    [
        "Apr 2023 - Present",
        "April 2023 — Present",
        "04/2023 - 03/2024",
        "2020 – 2023",
        "Jan 2021 to Dec 2022",
        "2023-04 - Present",   # ISO month precision, as the profile stores it
        "2020-06 - 2023-03",
        "Sept 2019 - Aug 2021",
    ],
)
def test_date_ranges_survive_common_formats(span: str):
    assert ats_check.DATE_RANGE_RE.search(span), span


def test_a_single_role_resume_is_not_flagged_for_dates():
    """One parseable range is enough - a graduate or a ten-year tenure at one
    employer is not a formatting problem."""
    text = (
        "Jane Doe\njane@example.com | +91 98765 43210 | github.com/jane\n\n"
        "Experience\nAnalyst, Somewhere\nApr 2023 - Present\n"
        "- Cut refresh time from 40 to 8 minutes\n\n"
        "Skills\nPower BI, DAX\n\nEducation\nB.Tech - Example University\n"
    )
    assert "unparseable_dates" not in rules(ats_check.check(make_doc(text)))


def test_markdown_headings_are_recognised():
    """Compass's own generated variants are markdown; `## Experience` must read
    as an Experience section, not as the literal string '## Experience'."""
    text = (
        "# Jane Doe\nAnalyst\njane@example.com | +91 98765 43210 | github.com/jane\n\n"
        "## Summary\nBI developer.\n\n## Skills\nPower BI, DAX\n\n"
        "## Experience\n### Analyst, Somewhere\nApr 2023 - Present\n- Cut refresh by 40%\n\n"
        "## Education\n- B.Tech - Example University (2020)\n"
    )
    report = ats_check.check(make_doc(text))
    for section in ("experience", "education", "skills", "summary"):
        assert section in report.sections_found, report.sections_found


def test_nonstandard_headings_are_flagged():
    text = CLEAN_RESUME.replace("Experience", "WHERE I'VE BEEN").replace("Skills", "MY TOOLKIT")
    report = ats_check.check(make_doc(text))
    assert "nonstandard_headings" in rules(report) or "missing_required_sections" in rules(report)


def test_score_is_clamped_to_range():
    """Enough stacked findings must not push the score negative."""
    doc = make_doc(
        "x" * 500, kind="docx",
        docx_textbox_count=5, docx_table_count=9, docx_column_count=3,
        docx_inline_shape_count=4,
    )
    report = ats_check.check(doc)
    assert 0.0 <= report.score <= 100.0


@pytest.mark.parametrize("severity", list(ats_check.SEVERITY_PENALTY))
def test_every_severity_has_a_penalty(severity: str):
    assert ats_check.SEVERITY_PENALTY[severity] > 0
