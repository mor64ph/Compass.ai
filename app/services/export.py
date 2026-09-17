"""ATS-safe résumé and cover-letter export (PRD Epic D).

Everything written here is deliberately boring: single column, no tables, no
text boxes, no images, real Heading styles, a common font, plain hyphen
bullets. The export path is the one place Compass fully controls the artefact,
so it should be impossible to produce a file that its own ATS checker would
fail.
"""

from __future__ import annotations

import re
from pathlib import Path

from app.config import get_settings

BODY_FONT = "Calibri"
BODY_FONT_FALLBACK = "Helvetica"  # reportlab built-in
BODY_SIZE = 10.5

_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")


# --------------------------------------------------------------------------
# Markdown parsing (a deliberately tiny subset)
# --------------------------------------------------------------------------


def parse_blocks(markdown: str) -> list[tuple[str, str]]:
    """`[(kind, text)]` where kind is h1 | h2 | h3 | bullet | para."""
    blocks: list[tuple[str, str]] = []
    for raw in (markdown or "").splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        if line.startswith("### "):
            blocks.append(("h3", line[4:].strip()))
        elif line.startswith("## "):
            blocks.append(("h2", line[3:].strip()))
        elif line.startswith("# "):
            blocks.append(("h1", line[2:].strip()))
        elif line.lstrip().startswith(("- ", "* ")):
            blocks.append(("bullet", line.lstrip()[2:].strip()))
        else:
            blocks.append(("para", line.strip()))
    return blocks


def safe_filename(*parts: str) -> str:
    joined = "_".join(p.strip() for p in parts if p and p.strip())
    cleaned = re.sub(r"[^\w\-. ]+", "", joined).strip().replace(" ", "_")
    return (cleaned or "compass_export")[:120]


# --------------------------------------------------------------------------
# DOCX
# --------------------------------------------------------------------------


def to_docx(markdown: str, filename: str) -> Path:
    import docx
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Pt

    document = docx.Document()

    # Base style. A single, widely-installed font avoids the substitution
    # artefacts that occasionally corrupt extracted text.
    normal = document.styles["Normal"]
    normal.font.name = BODY_FONT
    normal.font.size = Pt(BODY_SIZE)
    normal.paragraph_format.space_after = Pt(2)

    for section in document.sections:
        section.left_margin = section.right_margin = Pt(54)  # 0.75"
        section.top_margin = section.bottom_margin = Pt(54)

    for kind, text in parse_blocks(markdown):
        if kind == "h1":
            para = document.add_heading(text, level=0)
            para.alignment = WD_ALIGN_PARAGRAPH.LEFT
        elif kind == "h2":
            document.add_heading(text, level=1)
        elif kind == "h3":
            document.add_heading(text, level=2)
        elif kind == "bullet":
            _add_rich(document.add_paragraph(style="List Bullet"), text)
        else:
            _add_rich(document.add_paragraph(), text)

    path = get_settings().export_dir / f"{filename}.docx"
    document.save(str(path))
    return path


def _add_rich(paragraph, text: str) -> None:
    """Render `**bold**` as real runs rather than leaving literal asterisks."""
    pos = 0
    for match in _BOLD_RE.finditer(text):
        if match.start() > pos:
            paragraph.add_run(text[pos : match.start()])
        paragraph.add_run(match.group(1)).bold = True
        pos = match.end()
    if pos < len(text):
        paragraph.add_run(text[pos:])


# --------------------------------------------------------------------------
# PDF
# --------------------------------------------------------------------------


def to_pdf(markdown: str, filename: str) -> Path:
    from reportlab.lib.enums import TA_LEFT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate

    path = get_settings().export_dir / f"{filename}.pdf"
    base = getSampleStyleSheet()

    styles = {
        "h1": ParagraphStyle(
            "CompassH1", parent=base["Title"], fontName=f"{BODY_FONT_FALLBACK}-Bold",
            fontSize=17, leading=21, alignment=TA_LEFT, spaceAfter=2,
        ),
        "h2": ParagraphStyle(
            "CompassH2", parent=base["Heading2"], fontName=f"{BODY_FONT_FALLBACK}-Bold",
            fontSize=12, leading=15, spaceBefore=10, spaceAfter=3,
        ),
        "h3": ParagraphStyle(
            "CompassH3", parent=base["Heading3"], fontName=f"{BODY_FONT_FALLBACK}-Bold",
            fontSize=10.5, leading=13, spaceBefore=7, spaceAfter=1,
        ),
        "para": ParagraphStyle(
            "CompassBody", parent=base["BodyText"], fontName=BODY_FONT_FALLBACK,
            fontSize=BODY_SIZE, leading=13.5, spaceAfter=3,
        ),
    }
    # The hyphen is part of the paragraph text, not a separately drawn bullet
    # glyph. reportlab's ListFlowable renders the marker outside the text run, so
    # extraction loses it entirely - which made Compass's own PDF export trip its
    # own "written as prose, no bullets" rule, and would hide the list structure
    # from a real ATS parser too.
    styles["bullet"] = ParagraphStyle(
        "CompassBullet", parent=styles["para"], leftIndent=11, firstLineIndent=-11,
    )

    story: list = []
    for kind, text in parse_blocks(markdown):
        if kind == "bullet":
            story.append(Paragraph("- " + _inline(text), styles["bullet"]))
        else:
            story.append(Paragraph(_inline(text), styles[kind]))

    if not story:
        story.append(Paragraph("(empty document)", styles["para"]))

    SimpleDocTemplate(
        str(path),
        pagesize=A4,
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=16 * mm,
        bottomMargin=16 * mm,
        title=filename,
        # No page header or footer: an ATS may discard both, so nothing that
        # matters is allowed to live there.
    ).build(story)
    return path


def _inline(text: str) -> str:
    escaped = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return _BOLD_RE.sub(r"<b>\1</b>", escaped)


# --------------------------------------------------------------------------
# Cover letter
# --------------------------------------------------------------------------


def cover_letter_markdown(*, body: str, profile: dict, company: str, title: str) -> str:
    lines: list[str] = []
    if profile.get("full_name"):
        lines.append(f"# {profile['full_name']}")
    contact = [profile.get("location", ""), profile.get("phone", ""), profile.get("email", "")]
    contact_line = " | ".join(c for c in contact if c)
    if contact_line:
        lines.append(contact_line)
    lines.append("")
    header = " - ".join(p for p in [title, company] if p)
    if header:
        lines.append(f"## Application: {header}")
    lines.append("")
    lines.extend(p.strip() for p in (body or "").split("\n") if p.strip())
    return "\n".join(lines)
