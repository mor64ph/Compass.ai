"""Architecture guard for the PRD's non-negotiable constraints.

PRD Section 8 asks for this to be called out explicitly so the scraping pattern
is not reached for when Epic C or F "feels like a good excuse". A prose
instruction is easy to forget across sessions; a failing test is not.

See docs/CONSTRAINTS.md for the reasoning.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
APP_DIR = PROJECT_ROOT / "app"

BANNED_LIBRARIES = (
    "playwright",
    "selenium",
    "seleniumwire",
    "undetected_chromedriver",
    "puppeteer",
    "pyppeteer",
    "scrapy",
    "mechanize",
    "mechanicalsoup",
    "requests_html",
    "cloudscraper",
)

RESTRICTED_PLATFORMS = (
    "linkedin",
    "naukri",
    "indeed",
    "foundit",
    "wellfound",
    "glassdoor",
    "fishbowl",
    "reddit",
)


def python_sources() -> list[Path]:
    return [p for p in APP_DIR.rglob("*.py") if "__pycache__" not in p.parts]


def code_lines(path: Path) -> list[tuple[int, str]]:
    """Lines with comments stripped and docstring bodies skipped.

    The constraint is about *behaviour*, not vocabulary: naming a platform while
    explaining why Compass will not touch it has to stay legal, or the docs and
    the prompts could not exist.
    """
    out: list[tuple[int, str]] = []
    in_docstring = False
    delimiter = ""
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw
        if in_docstring:
            if delimiter in line:
                in_docstring = False
                line = line.split(delimiter, 1)[1]
            else:
                continue
        # Opening a docstring/triple-quoted block.
        for candidate in ('"""', "'''"):
            if candidate in line:
                before, _, after = line.partition(candidate)
                if candidate in after:  # single-line, closes immediately
                    line = before + after.split(candidate, 1)[1]
                else:
                    in_docstring = True
                    delimiter = candidate
                    line = before
                break
        line = line.split("#", 1)[0]
        if line.strip():
            out.append((number, line))
    return out


@pytest.mark.parametrize("library", BANNED_LIBRARIES)
def test_no_browser_automation_or_scraping_libraries(library: str) -> None:
    pattern = re.compile(rf"(?:^|[^\w.]){re.escape(library)}(?:[^\w]|$)", re.I)
    offenders: list[str] = []
    for path in python_sources():
        for number, line in code_lines(path):
            if pattern.search(line):
                offenders.append(f"{path.relative_to(PROJECT_ROOT)}:{number}: {line.strip()}")
    assert not offenders, (
        f"{library!r} must not appear in executable code. See docs/CONSTRAINTS.md.\n"
        + "\n".join(offenders)
    )


def test_requirements_declare_no_scraping_stack() -> None:
    text = (PROJECT_ROOT / "requirements.txt").read_text(encoding="utf-8").lower()
    found = [library for library in BANNED_LIBRARIES if library in text]
    assert not found, f"requirements.txt must not depend on: {found}"


@pytest.mark.parametrize("platform", RESTRICTED_PLATFORMS)
def test_no_requests_to_restricted_platforms(platform: str) -> None:
    """No URL in executable code may point at a restricted platform."""
    pattern = re.compile(rf"https?://[^\s\"']*{re.escape(platform)}", re.I)
    offenders: list[str] = []
    for path in python_sources():
        for number, line in code_lines(path):
            if pattern.search(line):
                offenders.append(f"{path.relative_to(PROJECT_ROOT)}:{number}: {line.strip()}")
    assert not offenders, (
        f"Executable code must not build URLs targeting {platform!r}.\n" + "\n".join(offenders)
    )


def test_no_submission_endpoint() -> None:
    """Compass must never submit a job application on the user's behalf.

    The pattern names the specific shapes rather than the bare word "submit":
    once there are login and invite forms, `login_submit` is an ordinary
    form-post handler and matching it made this test cry wolf. A rule that fires
    on innocent code gets suppressed, and then it is not protecting anything.
    """
    router_dir = APP_DIR / "routers"
    name_pattern = re.compile(
        r"def\s+\w*(?:"
        r"submit_application|application_submit|submit_to|apply_to|apply_job|"
        r"auto_apply|autoapply|mass_apply|bulk_apply|easy_apply|quick_apply"
        r")\w*\s*\(",
        re.I,
    )
    # Also catch a route *path* that offers submission, whatever the handler is
    # called - e.g. @router.post("/{id}/submit").
    route_pattern = re.compile(
        r"@router\.(?:get|post)\(\s*[\"'][^\"']*(?:/submit|/apply|auto-apply)", re.I
    )

    offenders: list[str] = []
    for path in router_dir.rglob("*.py"):
        for number, line in code_lines(path):
            if name_pattern.search(line) or route_pattern.search(line):
                offenders.append(f"{path.relative_to(PROJECT_ROOT)}:{number}: {line.strip()}")
    assert not offenders, (
        "Every application ends with the user clicking submit. See docs/CONSTRAINTS.md.\n"
        + "\n".join(offenders)
    )


def test_quality_gate_has_no_llm_call() -> None:
    """The gate stays deterministic - see docs/CONSTRAINTS.md §3."""
    source = (APP_DIR / "services" / "quality_gate.py").read_text(encoding="utf-8")
    for marker in ("get_llm", "anthropic", "messages.create"):
        assert marker not in source, (
            f"quality_gate.py must not reference {marker!r}. A gate you can argue "
            "your way past is not a gate."
        )


def test_ats_check_has_no_llm_call() -> None:
    source = (APP_DIR / "services" / "ats_check.py").read_text(encoding="utf-8")
    for marker in ("get_llm", "anthropic"):
        assert marker not in source, f"ats_check.py must stay rule-based; found {marker!r}."


def test_every_registered_prompt_has_a_file() -> None:
    from app.llm.client import PROMPT_DIR, PROMPT_VERSIONS

    missing = [
        f"{name}.v{version}.md"
        for name, version in PROMPT_VERSIONS.items()
        if not (PROMPT_DIR / f"{name}.v{version}.md").is_file()
    ]
    assert not missing, f"Registered prompts with no file on disk: {missing}"


def test_no_inline_prompt_strings_in_services() -> None:
    """Prompts live in versioned files, not in Python (docs/CONSTRAINTS.md §4)."""
    suspicious: list[str] = []
    pattern = re.compile(r"^\s*(?:system|prompt)\s*=\s*[\"'](?!\s*$).{40,}", re.I)
    for path in (APP_DIR / "services").rglob("*.py"):
        for number, line in code_lines(path):
            if pattern.search(line):
                suspicious.append(f"{path.relative_to(PROJECT_ROOT)}:{number}")
    assert not suspicious, (
        "Prompt text belongs in app/llm/prompts/ as a versioned file: " + ", ".join(suspicious)
    )


# --------------------------------------------------------------------------
# The CSP is only real if the templates stay free of inline script
# --------------------------------------------------------------------------

TEMPLATE_DIR = PROJECT_ROOT / "app" / "templates"

# `hx-on:` and `hx-on::` are included because htmx evaluates them with the
# Function constructor, which needs 'unsafe-eval' - the thing the CSP forbids.
FORBIDDEN_IN_TEMPLATES = (
    "<script>",
    "onclick=",
    "onsubmit=",
    "onchange=",
    "oninput=",
    "onload=",
    "onerror=",
    "hx-on:",
    "hx-on::",
    "javascript:",
)


def test_templates_contain_no_inline_javascript():
    """`script-src 'self'` with no unsafe-inline/unsafe-eval is load-bearing:
    Compass renders attacker-supplied résumé and JD text on nearly every page.
    Behaviour belongs in app/static/js/app.js, wired up with data- attributes.
    """
    offenders: list[str] = []
    for path in TEMPLATE_DIR.rglob("*.html"):
        text = path.read_text(encoding="utf-8").lower()
        for needle in FORBIDDEN_IN_TEMPLATES:
            if needle in text:
                offenders.append(f"{path.relative_to(PROJECT_ROOT)}: {needle}")
    assert not offenders, (
        "Inline JavaScript found. The Content-Security-Policy forbids it, so "
        "this would not run in a browser:\n  " + "\n  ".join(offenders)
    )


# The value of a src=/href= on a <script> or <link>, which is the only place a
# remote origin actually causes a fetch. Matching the whole line instead would
# flag the inline data: favicon, whose embedded `http://www.w3.org/2000/svg` is
# an XML namespace identifier and never requested.
_ASSET_REF_RE = re.compile(
    r"<(?:script|link)\b[^>]*?\b(?:src|href)\s*=\s*[\"']([^\"']+)[\"']", re.I | re.S
)


def test_no_third_party_script_or_style_origins():
    """Self-hosted, so the CSP needs no CDN exception and no third party can
    change the bytes served to a page holding career data."""
    offenders: list[str] = []
    for path in TEMPLATE_DIR.rglob("*.html"):
        for ref in _ASSET_REF_RE.findall(path.read_text(encoding="utf-8")):
            if ref.lower().startswith(("http://", "https://", "//")):
                offenders.append(f"{path.relative_to(PROJECT_ROOT)}: {ref}")
    assert not offenders, "Third-party asset origin:\n  " + "\n  ".join(offenders)


def test_the_shipped_secret_key_cannot_be_used_off_localhost():
    """Guards the one misconfiguration that hands over every account at once."""
    import pytest

    from app.security import DEFAULT_SECRET_KEY, assert_deployable

    with pytest.raises(RuntimeError):
        assert_deployable(secret_key=DEFAULT_SECRET_KEY, host="0.0.0.0")
