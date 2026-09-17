"""Adapters for applicant-tracking systems' public job-board APIs.

## Why this is not the thing docs/CONSTRAINTS.md forbids

The constraint is "no scraping, no session automation" against eight named job
boards, and the reasoning is in three parts: it backfires, it harvests personal
data, and it breaks a contract. None of the three applies here.

These endpoints are how an employer *publishes* its own vacancies. Greenhouse,
Ashby and the rest serve them unauthenticated, in JSON, documented, with no rate
limit worth the name, because the entire purpose is syndication - the same feed
powers the careers page on the company's own site. Reading one is the intended
use, involves no third party's personal data, and needs no session to automate.

`docs/CONSTRAINTS.md` §1 has said "public documented APIs (Greenhouse/Lever/Ashby
board endpoints) - Phase 2" since the first commit. This is that.

`ALLOWED_HOSTS` is pinned by `tests/test_constraints.py`, so the distinction is
enforced rather than merely intended: adding a job-board hostname here fails the
build.

## Verification status

Greenhouse, Ashby and SmartRecruiters were checked against live boards and the
field names below are what those APIs actually returned. Lever and Workable are
implemented from their documented shapes - the endpoints respond, but no board
token with open postings was available to confirm the item fields, so treat a
first run against them as the real test.
"""

from __future__ import annotations

import html
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 30

# Every host this module may contact. Pinned by tests/test_constraints.py.
ALLOWED_HOSTS = {
    "boards-api.greenhouse.io",
    "api.ashbyhq.com",
    "api.smartrecruiters.com",
    "api.lever.co",
    "apply.workable.com",
}


class ATSError(RuntimeError):
    """The board could not be read. The message is shown to the user, so it has
    to say which board and what to do about it."""


@dataclass
class Posting:
    """One vacancy, normalised across providers."""

    external_id: str
    title: str
    url: str
    jd_text: str
    company: str = ""
    location: str = ""
    department: str = ""
    employment_type: str = ""
    is_remote: bool = False
    posted_at: datetime | None = None
    raw: dict = field(default_factory=dict)


# --------------------------------------------------------------------------
# Shared helpers
# --------------------------------------------------------------------------

_TAG_RE = re.compile(r"<[^>]+>")
_BLANK_RUN_RE = re.compile(r"\n{3,}")
# Tags that imply a line break once the markup is gone, so a bulleted
# requirements list does not collapse into one unreadable paragraph - which
# would also destroy the section weighting in jd_match.
_BREAK_RE = re.compile(r"(?i)</?(?:p|br|div|li|tr|h[1-6]|ul|ol|table)[^>]*>")


def html_to_text(raw: str) -> str:
    """Flatten a job description's HTML into the plain text the scorer expects.

    Greenhouse double-escapes its `content` field - the JSON contains
    `&lt;h2&gt;` - so unescaping twice is correct rather than paranoid. Doing it
    once leaves literal tags in the text, where `jd_match` would read `<strong>`
    as a keyword.
    """
    text = html.unescape(html.unescape(raw or ""))
    text = _BREAK_RE.sub("\n", text)
    text = _TAG_RE.sub("", text)
    text = text.replace("\xa0", " ")
    lines = [line.strip() for line in text.splitlines()]
    return _BLANK_RUN_RE.sub("\n\n", "\n".join(lines)).strip()


def _looks_remote(*values: str) -> bool:
    joined = " ".join(v or "" for v in values).lower()
    return any(word in joined for word in ("remote", "anywhere", "distributed"))


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    # Naive UTC, to match app.models.utcnow - see its docstring.
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(tz=None).replace(tzinfo=None)
    return parsed


def _from_epoch_ms(value) -> datetime | None:
    try:
        return datetime.utcfromtimestamp(int(value) / 1000.0)
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def _get(url: str, *, label: str, params: dict | None = None):
    """GET with the host checked against the allowlist before the call.

    The check is here rather than only in the test so that a runtime value -
    a board token holding a full URL, say - cannot reach an unintended host.
    """
    from urllib.parse import urlparse

    import requests

    host = urlparse(url).hostname or ""
    if host not in ALLOWED_HOSTS:
        raise ATSError(
            f"Refusing to fetch {host!r}: not in the ATS allowlist. "
            "See docs/CONSTRAINTS.md §1."
        )

    try:
        response = requests.get(
            url,
            params=params,
            timeout=TIMEOUT_SECONDS,
            headers={"Accept": "application/json", "User-Agent": "Compass/0.1"},
        )
    except requests.Timeout as exc:
        raise ATSError(f"{label} did not respond within {TIMEOUT_SECONDS}s.") from exc
    except requests.RequestException as exc:
        raise ATSError(f"Could not reach {label}: {exc}") from exc

    if response.status_code == 404:
        raise ATSError(
            f"{label} has no board with that token. Check the slug in the "
            "employer's careers URL."
        )
    if response.status_code == 403:
        raise ATSError(f"{label} refused the request (403). The board may be private.")
    if response.status_code >= 400:
        raise ATSError(f"{label} returned HTTP {response.status_code}.")
    try:
        return response.json()
    except ValueError as exc:
        raise ATSError(f"{label} returned a response that was not JSON.") from exc


# --------------------------------------------------------------------------
# Greenhouse  - verified against a live board
# --------------------------------------------------------------------------


def fetch_greenhouse(token: str) -> list[Posting]:
    """`boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true`

    `content=true` inlines the description, which turns what would be an N+1
    into a single request per employer.
    """
    payload = _get(
        f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs",
        label="Greenhouse",
        params={"content": "true"},
    )
    postings: list[Posting] = []
    for job in payload.get("jobs") or []:
        location = ((job.get("location") or {}).get("name")) or ""
        departments = [
            d.get("name", "") for d in (job.get("departments") or []) if d.get("name")
        ]
        postings.append(
            Posting(
                external_id=str(job.get("id") or ""),
                title=job.get("title") or "",
                url=job.get("absolute_url") or "",
                jd_text=html_to_text(job.get("content") or ""),
                company=job.get("company_name") or "",
                location=location,
                department=", ".join(departments),
                is_remote=_looks_remote(location, job.get("title") or ""),
                posted_at=_parse_iso(job.get("first_published") or job.get("updated_at")),
                raw=job,
            )
        )
    return postings


# --------------------------------------------------------------------------
# Ashby  - verified against a live board
# --------------------------------------------------------------------------


def fetch_ashby(token: str) -> list[Posting]:
    """`api.ashbyhq.com/posting-api/job-board/{token}`

    The friendliest of the five: `descriptionPlain` arrives in the list call, so
    there is no HTML to flatten and no second request.
    """
    payload = _get(
        f"https://api.ashbyhq.com/posting-api/job-board/{token}",
        label="Ashby",
        params={"includeCompensation": "true"},
    )
    postings: list[Posting] = []
    for job in payload.get("jobs") or []:
        if job.get("isListed") is False:
            continue
        text = job.get("descriptionPlain") or html_to_text(job.get("descriptionHtml") or "")
        location = job.get("location") or ""
        postings.append(
            Posting(
                external_id=str(job.get("id") or ""),
                title=job.get("title") or "",
                url=job.get("jobUrl") or job.get("applyUrl") or "",
                jd_text=text,
                location=location,
                department=" / ".join(
                    p for p in (job.get("department"), job.get("team")) if p
                ),
                employment_type=job.get("employmentType") or "",
                is_remote=bool(job.get("isRemote"))
                or _looks_remote(location, job.get("workplaceType") or ""),
                posted_at=_parse_iso(job.get("publishedAt")),
                raw=job,
            )
        )
    return postings


# --------------------------------------------------------------------------
# SmartRecruiters  - verified against a live board
# --------------------------------------------------------------------------

# The list endpoint omits the description, so each posting needs a detail call.
# Capped because a large board would otherwise mean hundreds of requests: the
# list is ordered newest-first, and the newest postings are the ones worth
# ranking.
SMARTRECRUITERS_DETAIL_CAP = 60


def fetch_smartrecruiters(token: str) -> list[Posting]:
    """`api.smartrecruiters.com/v1/companies/{token}/postings`"""
    payload = _get(
        f"https://api.smartrecruiters.com/v1/companies/{token}/postings",
        label="SmartRecruiters",
        params={"limit": 100},
    )
    postings: list[Posting] = []
    for index, job in enumerate(payload.get("content") or []):
        job_id = str(job.get("id") or "")
        location_parts = job.get("location") or {}
        location = ", ".join(
            p
            for p in (
                location_parts.get("city"),
                location_parts.get("region"),
                location_parts.get("country"),
            )
            if p
        )
        jd_text = ""
        if index < SMARTRECRUITERS_DETAIL_CAP and job_id:
            jd_text = _smartrecruiters_detail(token, job_id)
        postings.append(
            Posting(
                external_id=job_id,
                title=job.get("name") or "",
                # A link for the user to click, not a host this module fetches -
                # see test_discovery_has_exactly_one_outbound_call_site.
                # Deliberately not `job["ref"]`: that is the API's own
                # self-link, so it would send the user to a JSON document
                # instead of the posting.
                url=f"https://jobs.smartrecruiters.com/{token}/{job_id}",
                jd_text=jd_text,
                company=((job.get("company") or {}).get("name")) or "",
                location=location,
                department=((job.get("department") or {}).get("label")) or "",
                employment_type=((job.get("typeOfEmployment") or {}).get("label")) or "",
                is_remote=bool(location_parts.get("remote"))
                or _looks_remote(location, job.get("name") or ""),
                posted_at=_parse_iso(job.get("releasedDate")),
                raw=job,
            )
        )
    return postings


def _smartrecruiters_detail(token: str, job_id: str) -> str:
    """One posting's description. A failure here is not fatal - a posting with no
    body still ranks on its title, and losing the whole board because one detail
    call failed would be worse."""
    try:
        detail = _get(
            f"https://api.smartrecruiters.com/v1/companies/{token}/postings/{job_id}",
            label="SmartRecruiters",
        )
    except ATSError as exc:
        logger.info("SmartRecruiters detail %s failed: %s", job_id, exc)
        return ""

    chunks: list[str] = []
    sections = ((detail.get("jobAd") or {}).get("sections")) or {}
    for key in ("companyDescription", "jobDescription", "qualifications", "additionalInformation"):
        section = sections.get(key) or {}
        body = section.get("text") or ""
        if body:
            title = section.get("title") or key
            chunks.append(f"{title}\n{html_to_text(body)}")
    return "\n\n".join(chunks)


# --------------------------------------------------------------------------
# Lever  - endpoint live, item shape from documentation
# --------------------------------------------------------------------------


def fetch_lever(token: str) -> list[Posting]:
    """`api.lever.co/v0/postings/{token}?mode=json`

    Returns a JSON *array* on success and an object with `ok: false` for an
    unknown board, which is why the shape is checked rather than assumed.
    """
    payload = _get(
        f"https://api.lever.co/v0/postings/{token}",
        label="Lever",
        params={"mode": "json"},
    )
    if isinstance(payload, dict):
        raise ATSError(
            f"Lever has no public board for {token!r} "
            f"({payload.get('error', 'unknown board')})."
        )

    postings: list[Posting] = []
    for job in payload:
        categories = job.get("categories") or {}
        body = job.get("descriptionPlain") or html_to_text(job.get("description") or "")
        extra = job.get("additionalPlain") or html_to_text(job.get("additional") or "")
        location = categories.get("location") or ""
        postings.append(
            Posting(
                external_id=str(job.get("id") or ""),
                title=job.get("text") or "",
                url=job.get("hostedUrl") or job.get("applyUrl") or "",
                jd_text="\n\n".join(p for p in (body, extra) if p),
                location=location,
                department=" / ".join(
                    p for p in (categories.get("department"), categories.get("team")) if p
                ),
                employment_type=categories.get("commitment") or "",
                is_remote=_looks_remote(location, categories.get("commitment") or ""),
                posted_at=_from_epoch_ms(job.get("createdAt")),
                raw=job,
            )
        )
    return postings


# --------------------------------------------------------------------------
# Workable  - endpoint live, item shape from documentation
# --------------------------------------------------------------------------


def fetch_workable(token: str) -> list[Posting]:
    """`apply.workable.com/api/v1/widget/accounts/{token}?details=true`"""
    payload = _get(
        f"https://apply.workable.com/api/v1/widget/accounts/{token}",
        label="Workable",
        params={"details": "true"},
    )
    postings: list[Posting] = []
    for job in payload.get("jobs") or []:
        location = ", ".join(
            p for p in (job.get("city"), job.get("state"), job.get("country")) if p
        )
        postings.append(
            Posting(
                external_id=str(job.get("shortcode") or job.get("id") or ""),
                title=job.get("title") or "",
                url=job.get("url") or job.get("application_url") or "",
                jd_text=html_to_text(
                    (job.get("description") or "") + "\n" + (job.get("requirements") or "")
                ),
                company=job.get("company_name") or "",
                location=location,
                department=job.get("department") or "",
                employment_type=job.get("employment_type") or "",
                is_remote=bool(job.get("telecommuting")) or _looks_remote(location),
                posted_at=_parse_iso(job.get("published_on") or job.get("created_at")),
                raw=job,
            )
        )
    return postings


# --------------------------------------------------------------------------
# Registry
# --------------------------------------------------------------------------

FETCHERS = {
    "greenhouse": fetch_greenhouse,
    "ashby": fetch_ashby,
    "smartrecruiters": fetch_smartrecruiters,
    "lever": fetch_lever,
    "workable": fetch_workable,
}

# Shown on the "add a board" form. `hint` is where to find the token, because
# that is the only genuinely confusing part of adding a source.
PROVIDERS = [
    {
        "key": "greenhouse",
        "label": "Greenhouse",
        "hint": "The slug in job-boards.greenhouse.io/<slug> — e.g. `stripe`.",
        "verified": True,
    },
    {
        "key": "ashby",
        "label": "Ashby",
        "hint": "The slug in jobs.ashbyhq.com/<slug>.",
        "verified": True,
    },
    {
        "key": "smartrecruiters",
        "label": "SmartRecruiters",
        "hint": "The identifier in jobs.smartrecruiters.com/<identifier>.",
        "verified": True,
    },
    {
        "key": "lever",
        "label": "Lever",
        "hint": "The slug in jobs.lever.co/<slug>.",
        "verified": False,
    },
    {
        "key": "workable",
        "label": "Workable",
        "hint": "The subdomain in <name>.workable.com.",
        "verified": False,
    },
]


def fetch(ats: str, token: str) -> list[Posting]:
    fetcher = FETCHERS.get((ats or "").strip().lower())
    if fetcher is None:
        raise ATSError(
            f"Unknown ATS {ats!r}. Supported: {', '.join(sorted(FETCHERS))}."
        )
    token = (token or "").strip().strip("/")
    if not token:
        raise ATSError("A board token is required.")
    return fetcher(token)
