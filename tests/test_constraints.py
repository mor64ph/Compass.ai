"""Architecture guard for the PRD's non-negotiable constraints.

PRD Section 8 asks for this to be called out explicitly so the scraping pattern
is not reached for when Epic C or F "feels like a good excuse". A prose
instruction is easy to forget across sessions; a failing test is not.

See docs/CONSTRAINTS.md for the reasoning.
"""

from __future__ import annotations

import inspect
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


# --------------------------------------------------------------------------
# Job discovery reads ATS boards, and only ATS boards
# --------------------------------------------------------------------------
#
# The discovery layer is the one place Compass fetches job postings, which makes
# it the place where "no scraping" could quietly erode. These tests pin the
# distinction docs/CONSTRAINTS.md §1 draws: an employer's own ATS feed exists to
# be syndicated; a job board that forbids crawling is off limits. Intent in a
# docstring is not enforcement.

EXPECTED_ATS_HOSTS = {
    "boards-api.greenhouse.io",
    "api.ashbyhq.com",
    "api.smartrecruiters.com",
    "api.lever.co",
    "apply.workable.com",
}


def test_ats_allowlist_is_exactly_the_reviewed_set():
    """Adding a host should be a deliberate act that updates this test too, not
    something that slips in with a feature."""
    from app.services.discovery.ats import ALLOWED_HOSTS

    assert ALLOWED_HOSTS == EXPECTED_ATS_HOSTS


def test_no_restricted_platform_is_on_the_ats_allowlist():
    from app.services.discovery.ats import ALLOWED_HOSTS

    for host in ALLOWED_HOSTS:
        for platform in RESTRICTED_PLATFORMS:
            assert platform not in host.lower(), f"{host} is a restricted platform"


def test_discovery_refuses_a_host_outside_the_allowlist():
    """The guard is enforced at call time, not only by review, so a board token
    that smuggles in a full URL cannot redirect the fetch."""
    from app.services.discovery import ats

    with pytest.raises(ats.ATSError, match="allowlist"):
        ats._get("https://www.linkedin.com/jobs/search", label="probe")


def test_discovery_has_exactly_one_outbound_call_site():
    """`_get` checks the allowlist before every fetch, so that check is only
    airtight while `_get` is the *only* way out of the module. A second helper
    added later would silently bypass it.

    Deliberately not a grep for hostname literals: `jobs.smartrecruiters.com`
    appears in this module as a link built for the user to click, and a test that
    conflates a link target with a fetch target teaches people to widen the
    allowlist to silence it.
    """
    import re as _re

    source = (APP_DIR / "services" / "discovery").rglob("*.py")
    call_sites: list[str] = []
    for path in source:
        for number, line in code_lines(path):
            if _re.search(r"\brequests\.(get|post|put|patch|request|delete)\s*\(", line):
                call_sites.append(f"{path.name}:{number}: {line.strip()}")
    assert len(call_sites) == 1, (
        "Discovery must route every outbound request through ats._get, which "
        "enforces ALLOWED_HOSTS. Found:\n  " + "\n  ".join(call_sites)
    )


def test_discovery_records_api_provenance_not_user_pasted():
    """An adopted posting must not claim the user pasted it. Provenance is the
    question the scraping constraint exists to answer."""
    from app.models import SourceType

    source = (APP_DIR / "services" / "application_service.py").read_text(encoding="utf-8")
    api_block = source.split("def create_from_api")[1].split("def _default_variant")[0]
    assert "SourceType.API.value" in api_block
    assert "USER_PASTED" not in api_block
    assert SourceType.API.value == "api"


# --------------------------------------------------------------------------
# The schema must stay portable to Postgres
# --------------------------------------------------------------------------


def test_every_table_compiles_for_postgres():
    """SQLite is the default, and it is the wrong choice on a host with no
    persistent disk - a free-tier service restarts and the file is gone, taking
    every account with it. `COMPASS_DB_URL` pointed at a managed Postgres is the
    answer, which only works while the schema stays portable.

    A SQLite-only type added to a model would not fail any other test in this
    suite; it would fail on the deployment that needs it most.
    """
    from sqlalchemy.dialects import postgresql
    from sqlalchemy.schema import CreateTable

    from app import models

    dialect = postgresql.dialect()
    failures: list[str] = []
    for table in models.Base.metadata.sorted_tables:
        try:
            CreateTable(table).compile(dialect=dialect)
        except Exception as exc:  # pragma: no cover - only on a regression
            failures.append(f"{table.name}: {exc}")

    assert not failures, "not portable to Postgres:\n  " + "\n  ".join(failures)
    assert len(models.Base.metadata.sorted_tables) >= 20


def test_sqlite_pragmas_are_guarded_by_the_url():
    """PRAGMA is SQLite-only syntax. Running it against Postgres is a startup
    crash, so the listener must never be registered for a non-sqlite URL."""
    source = (APP_DIR / "db.py").read_text(encoding="utf-8")
    pragma_at = source.index("PRAGMA")
    guard_at = source.index('_url.startswith("sqlite")')
    assert guard_at < pragma_at, "the PRAGMA block is not behind the sqlite guard"


# --------------------------------------------------------------------------
# A blocking LLM call must not run on the event loop
# --------------------------------------------------------------------------


def test_no_llm_route_is_declared_async():
    """Every `@guard` route makes a synchronous LLM call of 20-60 seconds.

    Declared `async def`, such a handler runs on the event loop and holds it for
    the whole call. With one worker - which is what a free instance gets, and
    what the deploy config pins - nothing else can be served meanwhile,
    including the platform's health check on /login. The host concludes the
    instance is dead, restarts it, and the user gets 502 Bad Gateway instead of
    their gap report. That is exactly what happened on the first live deploy.

    Declared as a plain `def`, FastAPI runs it in a threadpool and the loop
    stays free. `app.web.guard` supports both, so nothing forces the coroutine
    back.

    An `async def` here is only correct if the handler actually awaits
    something; none of them do, and the one route that does (`/resumes/upload`,
    which awaits the request body) is not decorated with @guard.
    """
    pattern = re.compile(r"@guard\s*\n\s*async def\s+(\w+)")
    offenders: list[str] = []
    for path in (APP_DIR / "routers").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for match in pattern.finditer(text):
            line = text[: match.start()].count("\n") + 1
            offenders.append(
                f"{path.relative_to(PROJECT_ROOT)}:{line}: {match.group(1)}"
            )
    assert not offenders, (
        "These handlers block the event loop for the length of an LLM call, "
        "which a health check reads as a dead instance. Drop `async`:\n  "
        + "\n  ".join(offenders)
    )


def test_guard_handles_a_sync_handler():
    """The decorator has to work on a plain `def`, or removing `async` above
    turns every guarded route into a coroutine nobody awaits."""
    from app.llm.client import LLMUnavailable
    from app.web import guard

    @guard
    def handler(request=None):
        raise LLMUnavailable("no provider configured")

    assert not inspect.iscoroutinefunction(handler)
    # Returns the explanation response rather than propagating.
    assert handler() is not None


def test_guard_still_handles_an_async_handler():
    import asyncio

    from app.llm.client import RateLimited
    from app.web import guard

    @guard
    async def handler(request=None):
        raise RateLimited("slow down")

    assert inspect.iscoroutinefunction(handler)
    assert asyncio.run(handler()) is not None
