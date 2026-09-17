"""Deterministic ATS parseability checker (PRD Epic B).

**No LLM is used here, by design.** These are mechanical facts about a file -
whether it has a text layer, whether the layout is two-column, whether the
phone number is stranded in a page header. Rules give the same answer every
time, cost nothing, and can be unit-tested, which is exactly what you want from
the check that runs before a human ever sees the résumé.

Judgement calls about *content* live in `gap_report.py`, which does use an LLM.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import asdict, dataclass, field

from app.services.text_extract import ExtractedDoc
from app.services.textutil import find_phones, has_metrics

# --------------------------------------------------------------------------
# Findings
# --------------------------------------------------------------------------

SEVERITY_PENALTY = {"critical": 45.0, "high": 15.0, "medium": 7.0, "low": 3.0}


@dataclass
class Finding:
    rule: str
    severity: str  # critical | high | medium | low
    title: str
    detail: str
    fix: str

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class ATSReport:
    score: float
    findings: list[Finding] = field(default_factory=list)
    stats: dict = field(default_factory=dict)
    sections_found: list[str] = field(default_factory=list)
    sections_missing: list[str] = field(default_factory=list)
    unrecognised_headings: list[str] = field(default_factory=list)

    @property
    def grade(self) -> str:
        if self.score >= 90:
            return "Clean"
        if self.score >= 75:
            return "Minor issues"
        if self.score >= 55:
            return "Needs work"
        return "High rejection risk"

    def as_dict(self) -> dict:
        return {
            "score": round(self.score, 1),
            "grade": self.grade,
            "findings": [f.as_dict() for f in self.findings],
            "stats": self.stats,
            "sections_found": self.sections_found,
            "sections_missing": self.sections_missing,
            "unrecognised_headings": self.unrecognised_headings,
        }


# --------------------------------------------------------------------------
# Section vocabulary
# --------------------------------------------------------------------------

# Canonical section -> headings an ATS reliably maps to it.
SECTION_SYNONYMS: dict[str, set[str]] = {
    "experience": {
        "experience", "work experience", "professional experience",
        "employment", "employment history", "work history", "career history",
        "professional background", "relevant experience",
    },
    "education": {"education", "academic background", "academics", "qualifications",
                  "educational qualifications"},
    "skills": {"skills", "technical skills", "core skills", "key skills",
               "skills & tools", "technical proficiencies", "competencies",
               "core competencies", "technologies"},
    "summary": {"summary", "professional summary", "profile", "professional profile",
                "objective", "career objective", "about", "about me", "overview"},
    "projects": {"projects", "personal projects", "side projects", "key projects",
                 "selected projects", "portfolio"},
    "certifications": {"certifications", "certificates", "licenses & certifications",
                       "certifications & training", "training"},
}

REQUIRED_SECTIONS = ("experience", "education", "skills")
RECOMMENDED_SECTIONS = ("summary", "projects", "certifications")

_ALL_KNOWN_HEADINGS = {h for group in SECTION_SYNONYMS.values() for h in group}

# --------------------------------------------------------------------------
# Patterns
# --------------------------------------------------------------------------

EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")

# Bare domains count. Résumés routinely write `github.com/name` with no scheme
# and no `www.`, and a rule that missed those would report "no links" on most
# real documents. The lookbehind keeps the domain half of an email address from
# matching as a URL.
URL_RE = re.compile(
    r"(?:https?://|www\.)[^\s)>\]]+"
    r"|(?<![\w@.])[\w-]{2,}\.(?:com|net|org|io|dev|ai|app|xyz|me)(?:/[^\s)>\]]*)?",
    re.I,
)

# One date endpoint: a month name with a year, a numeric month/year in either
# order, or a bare year. `YYYY-MM` is included because people do write ISO dates
# on résumés - and because Compass's own generator used to emit them.
_DATE_POINT = (
    r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s*'?\d{2,4}"
    r"|(?:19|20)\d{2}[-/](?:0[1-9]|1[0-2])"
    r"|\d{1,2}[/.]\d{4}"
    r"|\b(?:19|20)\d{2}\b"
)
DATE_RANGE_RE = re.compile(
    rf"(?:{_DATE_POINT})"
    r"\s*(?:-|–|—|to|until|through)\s*"
    rf"(?:present|current|now|today|ongoing|{_DATE_POINT})",
    re.I,
)

WEAK_VERBS = (
    "responsible for", "worked on", "involved in", "helped with",
    "participated in", "assisted with", "tasked with", "duties included",
)

# Private-use areas: where icon fonts (Font Awesome and friends) live. An ATS
# reads these as mojibake, and they usually sit right next to the contact
# details they decorate.
_PRIVATE_USE_RANGES = ((0xE000, 0xF8FF), (0xF0000, 0xFFFFD), (0x100000, 0x10FFFD))

LIGATURES = {"ﬀ", "ﬁ", "ﬂ", "ﬃ", "ﬄ", "ﬅ", "ﬆ"}

MIN_WORDS = 250
MAX_WORDS = 1100
MAX_PAGES = 3


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def check(doc: ExtractedDoc) -> ATSReport:
    findings: list[Finding] = []

    if not doc.ok:
        findings.append(
            Finding(
                rule="file_unreadable",
                severity="critical",
                title="Compass could not read this file",
                detail=doc.error,
                fix="Export the résumé as a standard PDF or DOCX and upload again.",
            )
        )
        return ATSReport(score=0.0, findings=findings, stats={"readable": False})

    text = doc.text
    lower = text.lower()
    headings = _candidate_headings(text)
    found, missing, unrecognised = _classify_sections(headings)

    findings += _check_text_layer(doc)
    findings += _check_layout(doc)
    findings += _check_contact(text)
    findings += _check_sections(found, missing, unrecognised)
    findings += _check_dates(text)
    findings += _check_glyphs(text)
    findings += _check_length(doc)
    findings += _check_bullets_and_verbs(doc, text, lower)

    score = 100.0 - sum(SEVERITY_PENALTY[f.severity] for f in findings)

    stats = {
        "readable": True,
        "kind": doc.kind,
        "pages": doc.page_count,
        "words": doc.word_count,
        "chars": doc.char_count,
        "emails_found": len(set(EMAIL_RE.findall(text))),
        "phones_found": len(set(find_phones(text))),
        "links_found": len(set(URL_RE.findall(text))),
        "date_ranges_found": len(DATE_RANGE_RE.findall(text)),
        "bullet_lines": _bullet_line_count(text),
    }
    if doc.kind == "pdf":
        stats["ruled_tables"] = sum(p.ruled_table_count for p in doc.pages)
        stats["images"] = sum(p.image_count for p in doc.pages)
        stats["max_gutter_ratio"] = round(
            max((p.gutter_width_ratio for p in doc.pages), default=0.0), 3
        )
    elif doc.kind == "docx":
        stats["tables"] = doc.docx_table_count
        stats["text_boxes"] = doc.docx_textbox_count
        stats["columns"] = doc.docx_column_count
        stats["inline_shapes"] = doc.docx_inline_shape_count
        stats["list_paragraphs"] = doc.docx_list_paragraphs

    return ATSReport(
        score=max(0.0, min(100.0, score)),
        findings=sorted(findings, key=lambda f: -SEVERITY_PENALTY[f.severity]),
        stats=stats,
        sections_found=found,
        sections_missing=missing,
        unrecognised_headings=unrecognised,
    )


# --------------------------------------------------------------------------
# Individual rule groups
# --------------------------------------------------------------------------


def _check_text_layer(doc: ExtractedDoc) -> list[Finding]:
    """A PDF with no text layer is a scan. Every keyword scorer sees an empty
    document, which is the single most expensive failure mode here."""
    if doc.char_count >= 400:
        return []
    if doc.kind == "pdf":
        return [
            Finding(
                rule="no_text_layer",
                severity="critical",
                title="Almost no machine-readable text",
                detail=(
                    f"Only {doc.char_count} characters could be extracted. This PDF is "
                    "most likely a scan or an exported image, so an ATS sees a blank "
                    "document no matter how good the content is."
                ),
                fix=(
                    "Re-export from the original document (Word, Google Docs, LaTeX) "
                    "rather than printing/scanning or exporting as an image."
                ),
            )
        ]
    return [
        Finding(
            rule="near_empty",
            severity="critical",
            title="Almost no text found",
            detail=f"Only {doc.char_count} characters were extracted from this file.",
            fix="Check the file is the right one and that the content is real text.",
        )
    ]


def _check_layout(doc: ExtractedDoc) -> list[Finding]:
    findings: list[Finding] = []

    if doc.kind == "pdf":
        tables = sum(p.ruled_table_count for p in doc.pages)
        if tables:
            findings.append(
                Finding(
                    rule="pdf_tables",
                    severity="high",
                    title=f"{tables} table-like structure(s) detected",
                    detail=(
                        "Tables are read cell-by-cell in an unpredictable order. A skills "
                        "grid usually survives; a table wrapping your job history does not."
                    ),
                    fix="Replace tables with plain paragraphs and simple bullet lists.",
                )
            )

        images = sum(p.image_count for p in doc.pages)
        if images:
            findings.append(
                Finding(
                    rule="pdf_images",
                    severity="medium",
                    title=f"{images} image(s) embedded",
                    detail=(
                        "Any text inside an image is invisible to a parser, and headshots "
                        "are actively discouraged for most markets."
                    ),
                    fix="Remove images, icons and logos; put the text in the document itself.",
                )
            )

        # A gutter needs to be genuinely wide and roughly central before it
        # implies two columns rather than ordinary ragged-right whitespace.
        worst = max(doc.pages, key=lambda p: p.gutter_width_ratio, default=None)
        if worst and worst.gutter_width_ratio >= 0.08 and 0.18 <= worst.gutter_center_ratio <= 0.82:
            findings.append(
                Finding(
                    rule="multi_column",
                    severity="high",
                    title="Multi-column layout detected",
                    detail=(
                        f"Page {worst.index + 1} has a vertical whitespace channel "
                        f"{worst.gutter_width_ratio:.0%} of the page wide, centred at "
                        f"{worst.gutter_center_ratio:.0%} across. That is the signature of a "
                        "two-column or sidebar layout, which many parsers flatten by reading "
                        "straight across - interleaving the two columns into nonsense."
                    ),
                    fix="Use a single-column layout for the version you submit.",
                )
            )

        header_text = " ".join(p.header_text for p in doc.pages)
        footer_text = " ".join(p.footer_text for p in doc.pages)
        if EMAIL_RE.search(header_text) or find_phones(header_text):
            findings.append(
                Finding(
                    rule="contact_in_header",
                    severity="high",
                    title="Contact details sit in the page header",
                    detail=(
                        "Contact information appears in the top 6% of the page, which many "
                        "parsers treat as a repeating header and discard - leaving an "
                        "application with no way to reach you."
                    ),
                    fix="Move name, email and phone into the body of the first page.",
                )
            )
        elif EMAIL_RE.search(footer_text) or find_phones(footer_text):
            findings.append(
                Finding(
                    rule="contact_in_footer",
                    severity="medium",
                    title="Contact details sit in the page footer",
                    detail="Footer content is dropped by some parsers.",
                    fix="Keep contact details in the body of the first page.",
                )
            )

    elif doc.kind == "docx":
        if doc.docx_textbox_count:
            findings.append(
                Finding(
                    rule="docx_textbox",
                    severity="critical",
                    title=f"{doc.docx_textbox_count} text box(es) detected",
                    detail=(
                        "Text boxes are the worst offender in DOCX résumés: the text is "
                        "not part of the document flow, so many parsers skip it entirely. "
                        "If your headline or skills sit in one, they simply do not exist."
                    ),
                    fix="Delete the text boxes and retype the content as normal paragraphs.",
                )
            )
        if doc.docx_column_count > 1:
            findings.append(
                Finding(
                    rule="docx_columns",
                    severity="high",
                    title=f"Page is set to {doc.docx_column_count} columns",
                    detail="Multi-column sections get flattened and interleaved on parse.",
                    fix="Set the layout to a single column.",
                )
            )
        if doc.docx_table_count:
            findings.append(
                Finding(
                    rule="docx_tables",
                    severity="high",
                    title=f"{doc.docx_table_count} table(s) in the document",
                    detail=(
                        "Table cells are read in an unpredictable order. Tables used for "
                        "page layout are the usual cause of scrambled parsed résumés."
                    ),
                    fix="Convert tables to plain paragraphs and bullet lists.",
                )
            )
        if doc.docx_inline_shape_count:
            findings.append(
                Finding(
                    rule="docx_shapes",
                    severity="medium",
                    title=f"{doc.docx_inline_shape_count} image/shape(s) embedded",
                    detail="Text inside images and shapes is invisible to a parser.",
                    fix="Remove decorative graphics, skill bars and icons.",
                )
            )
        if doc.docx_header_footer_text and (
            EMAIL_RE.search(doc.docx_header_footer_text)
            or find_phones(doc.docx_header_footer_text)
        ):
            findings.append(
                Finding(
                    rule="contact_in_header",
                    severity="high",
                    title="Contact details are in the header or footer",
                    detail="Header/footer content is discarded by many parsers.",
                    fix="Move contact details into the body of the first page.",
                )
            )
        if not doc.docx_uses_heading_styles:
            findings.append(
                Finding(
                    rule="no_heading_styles",
                    severity="low",
                    title="No heading styles used",
                    detail=(
                        "Section titles are formatted as bold body text rather than real "
                        "Heading styles. Parsers use style information as one signal for "
                        "section boundaries."
                    ),
                    fix="Apply Heading 1/Heading 2 styles to section titles.",
                )
            )

    return findings


def _check_contact(text: str) -> list[Finding]:
    findings: list[Finding] = []
    head = "\n".join(text.splitlines()[:12])

    if not EMAIL_RE.search(text):
        findings.append(
            Finding(
                rule="no_email",
                severity="critical",
                title="No email address found",
                detail="No parseable email address appears anywhere in the document.",
                fix="Add a plain-text email address near the top. Not as an image or a link label.",
            )
        )
    if not find_phones(text):
        findings.append(
            Finding(
                rule="no_phone",
                severity="high",
                title="No phone number found",
                detail="No parseable phone number was detected.",
                fix="Add a phone number in plain text, with the country code.",
            )
        )
    elif not (EMAIL_RE.search(head) or find_phones(head)):
        findings.append(
            Finding(
                rule="contact_buried",
                severity="medium",
                title="Contact details are not near the top",
                detail=(
                    "Nothing that looks like contact information appears in the first 12 "
                    "lines. Parsers weight the top of the document heavily when deciding "
                    "which values are yours."
                ),
                fix="Put name, location, phone and email in the first few lines.",
            )
        )
    if not URL_RE.search(text):
        findings.append(
            Finding(
                rule="no_links",
                severity="low",
                title="No links found",
                detail=(
                    "No portfolio, GitHub or professional-profile URL is present. For a "
                    "candidate with shipped side projects, that is a wasted differentiator."
                ),
                fix="Add a plain-text URL to a portfolio or profile.",
            )
        )
    return findings


def _check_sections(found: list[str], missing: list[str], unrecognised: list[str]) -> list[Finding]:
    findings: list[Finding] = []
    hard_missing = [s for s in missing if s in REQUIRED_SECTIONS]
    soft_missing = [s for s in missing if s in RECOMMENDED_SECTIONS]

    if hard_missing:
        findings.append(
            Finding(
                rule="missing_required_sections",
                severity="high",
                title=f"No recognisable {', '.join(hard_missing)} heading",
                detail=(
                    "Parsers segment a résumé by matching headings against a known "
                    "vocabulary. A section they cannot label often gets attributed to the "
                    "wrong field or dropped."
                ),
                fix=(
                    "Use conventional headings: "
                    + ", ".join(f"'{s.title()}'" for s in hard_missing)
                    + "."
                ),
            )
        )
    if soft_missing:
        findings.append(
            Finding(
                rule="missing_recommended_sections",
                severity="low",
                title=f"No {', '.join(soft_missing)} section",
                detail="These sections are not mandatory but are expected and searched for.",
                fix="Add them if you have the content - especially Projects.",
            )
        )
    if unrecognised:
        shown = ", ".join(f"'{h}'" for h in unrecognised[:5])
        findings.append(
            Finding(
                rule="nonstandard_headings",
                severity="medium",
                title=f"{len(unrecognised)} non-standard section heading(s)",
                detail=(
                    f"{shown} do not match headings parsers recognise. Creative headings "
                    "read well to a human and are invisible to the machine that sorts you "
                    "first."
                ),
                fix="Rename to conventional equivalents; keep the creative version for your site.",
            )
        )
    return findings


def _check_dates(text: str) -> list[Finding]:
    """Flag only when *no* date range parses.

    Requiring two would penalise a legitimately single-role résumé - a recent
    graduate, or someone who has been at one employer for a decade - which is a
    false positive on a high-severity rule. The trade is that a four-role résumé
    with one parseable date slips through; catching that reliably would need to
    count roles, and role detection is far less certain than date matching.
    """
    if DATE_RANGE_RE.search(text):
        return []
    return [
        Finding(
            rule="unparseable_dates",
            severity="high",
            title="Employment dates are not in a parseable format",
            detail=(
                "No recognisable date range was found. Parsers compute tenure and "
                "recency from these; without them, filters on 'years of experience' "
                "silently exclude you."
            ),
            fix=(
                "Use an unambiguous range per role, e.g. 'Apr 2023 - Present' or "
                "'04/2023 - 03/2024'."
            ),
        )
    ]


def _check_glyphs(text: str) -> list[Finding]:
    findings: list[Finding] = []

    private_use = {
        ch for ch in text if any(lo <= ord(ch) <= hi for lo, hi in _PRIVATE_USE_RANGES)
    }
    if private_use:
        findings.append(
            Finding(
                rule="icon_font_glyphs",
                severity="high",
                title="Icon-font characters detected",
                detail=(
                    f"{len(private_use)} character(s) from a Unicode private-use area are "
                    "present - the hallmark of an icon font. These decode to mojibake, and "
                    "they normally sit immediately beside the contact details they label."
                ),
                fix="Delete the icons and label fields in plain text ('Email:', 'Phone:').",
            )
        )

    ligatures = LIGATURES & set(text)
    if ligatures:
        findings.append(
            Finding(
                rule="ligatures",
                severity="medium",
                title="Typographic ligatures in the text layer",
                detail=(
                    "Characters like 'ﬁ' and 'ﬂ' are single glyphs, so 'identify' extracts "
                    "as a token no keyword search will match. Common in LaTeX and some PDF "
                    "exporters."
                ),
                fix="Disable ligatures before export, or export via a different route.",
            )
        )

    control = {
        ch for ch in text if unicodedata.category(ch) == "Cc" and ch not in "\n\t"
    }
    if control:
        findings.append(
            Finding(
                rule="control_chars",
                severity="low",
                title="Control characters in the text layer",
                detail="Stray control characters can split words during extraction.",
                fix="Re-export the document from its source.",
            )
        )
    return findings


def _check_length(doc: ExtractedDoc) -> list[Finding]:
    findings: list[Finding] = []
    words = doc.word_count

    if 0 < words < MIN_WORDS:
        findings.append(
            Finding(
                rule="too_short",
                severity="medium",
                title=f"Only {words} words",
                detail=(
                    "Below roughly 250 words there is usually not enough evidence for a "
                    "keyword-based scorer to rank you against anyone."
                ),
                fix="Expand achievements with specifics and numbers.",
            )
        )
    elif words > MAX_WORDS:
        findings.append(
            Finding(
                rule="too_long",
                severity="low",
                title=f"{words} words is long",
                detail="Long résumés get skimmed; the top third does the work.",
                fix="Cut older roles down to one or two lines each.",
            )
        )

    if doc.page_count > MAX_PAGES:
        findings.append(
            Finding(
                rule="too_many_pages",
                severity="low",
                title=f"{doc.page_count} pages",
                detail="Beyond three pages, later content is rarely read.",
                fix="Trim to two pages for most roles.",
            )
        )
    return findings


def _check_bullets_and_verbs(doc: ExtractedDoc, text: str, lower: str) -> list[Finding]:
    findings: list[Finding] = []

    # A DOCX using Word list styles has real bullets even though the glyph is
    # not in the extracted text, so trust the structural count when it exists.
    styled_bullets = doc.docx_list_paragraphs
    if styled_bullets < 5 and _bullet_line_count(text) < 5 and text:
        findings.append(
            Finding(
                rule="few_bullets",
                severity="low",
                title="Very few bullet points",
                detail=(
                    "Experience appears to be written as prose paragraphs. Bullets are "
                    "easier to segment and far easier for a human to skim."
                ),
                fix="Break responsibilities and achievements into single-line bullets.",
            )
        )

    hits = [phrase for phrase in WEAK_VERBS if phrase in lower]
    if hits:
        findings.append(
            Finding(
                rule="weak_verbs",
                severity="medium",
                title=f"{len(hits)} weak opener(s) in use",
                detail=(
                    "Found: " + ", ".join(f"'{h}'" for h in hits) + ". These describe a job "
                    "description rather than what you actually accomplished."
                ),
                fix="Open with a specific verb and end with an outcome. Epic B can rewrite these.",
            )
        )

    # A résumé where nothing is quantified reads as duties, not achievements.
    # `has_metrics` discounts phone numbers and calendar years, so an employment
    # date does not silently satisfy this rule.
    if not has_metrics(text):
        findings.append(
            Finding(
                rule="no_quantification",
                severity="medium",
                title="No quantified outcomes detected",
                detail=(
                    "No percentages, multiples, volumes or durations were found anywhere. "
                    "Unquantified bullets read as responsibilities."
                ),
                fix="Add before/after numbers to your strongest three or four bullets.",
            )
        )
    return findings


# --------------------------------------------------------------------------
# Heading detection
# --------------------------------------------------------------------------

_BULLET_CHARS = "•▪◦‣·-*–—>"


def _bullet_line_count(text: str) -> int:
    return sum(
        1
        for line in text.splitlines()
        if line.strip()[:1] in set(_BULLET_CHARS) and len(line.strip()) > 2
    )


def _candidate_headings(text: str) -> list[str]:
    """Lines that look like section headings: short, no terminal punctuation,
    either ALL CAPS or Title Case, and not a bullet."""
    headings: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        # Strip markdown heading markers. Compass's own generated variants are
        # markdown, so without this a `## Experience` heading is read as the
        # literal string "## Experience", matches no known section name, and the
        # cleanest possible résumé reports as having no Experience section.
        if line.startswith("#"):
            line = line.lstrip("#").strip()
        line = line.strip(":").strip()
        if not (2 <= len(line) <= 48):
            continue
        if line[:1] in set(_BULLET_CHARS):
            continue
        if line.endswith((".", ",", ";", "!", "?")):
            continue
        words = line.split()
        if not (1 <= len(words) <= 5):
            continue
        if EMAIL_RE.search(line) or URL_RE.search(line):
            continue
        # Reject lines that are mostly digits/punctuation (dates, phone numbers).
        letters = sum(1 for ch in line if ch.isalpha())
        if letters < max(3, len(line) * 0.5):
            continue
        is_upper = line.isupper()
        is_title = all(w[:1].isupper() or not w[:1].isalpha() for w in words)
        if is_upper or is_title:
            headings.append(line)
    return headings


def _classify_sections(headings: list[str]) -> tuple[list[str], list[str], list[str]]:
    normalised = [re.sub(r"\s+", " ", h).strip().lower() for h in headings]
    found: list[str] = []
    for canonical, synonyms in SECTION_SYNONYMS.items():
        if any(n in synonyms for n in normalised):
            found.append(canonical)

    missing = [
        s for s in (*REQUIRED_SECTIONS, *RECOMMENDED_SECTIONS) if s not in found
    ]

    # An unrecognised heading is only interesting if it is not a person's name,
    # a job title, or a company - so require it to look like a label: either
    # ALL CAPS, or a known-heading-adjacent word we still failed to match.
    unrecognised: list[str] = []
    for original, norm in zip(headings, normalised, strict=True):
        if norm in _ALL_KNOWN_HEADINGS:
            continue
        if original.isupper() and len(norm.split()) <= 4:
            unrecognised.append(original)
    return found, missing, unrecognised
