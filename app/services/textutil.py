"""Small text helpers shared across services."""

from __future__ import annotations

import re

TRUNCATION_MARKER = "\n\n[...truncated by Compass - this text exceeded the input budget]"

_YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")
_YEAR_RANGE_RE = re.compile(r"(?:19|20)\d{2}\s*[-–—]\s*(?:19|20)\d{2}")

# Deliberately over-matching: the digit-count check in `find_phones` is what
# actually decides, so this only has to be loose enough to cover Indian mobile
# (`+91 98765 43210`, 5+5 grouping), US (`+1 (555) 123-4567`, where the digits
# after a parenthesised area code are only seven) and international forms.
# Commas are excluded from the run, which is what keeps `12,345,678 records`
# from reading as a phone number.
_PHONE_LIKE_RE = re.compile(
    r"(?:\+\d{1,3}[\s.\-]?)?(?:\(\d{1,4}\)[\s.\-]?)?\d[\d\s.\-]{5,16}\d"
)
_DIGIT_RE = re.compile(r"\d")


def clip(text: str, limit: int) -> str:
    """Trim over-long input at a line boundary and say so.

    Silent truncation would make a downstream report quietly wrong about a
    section it never saw, so the marker is part of the contract.
    """
    if len(text) <= limit:
        return text
    cut = text[:limit]
    boundary = cut.rfind("\n")
    if boundary > limit * 0.6:
        cut = cut[:boundary]
    return cut + TRUNCATION_MARKER


def has_metrics(text: str) -> bool:
    """True when the text contains something that reads as a real quantity.

    Phone numbers and calendar years are stripped first. Without that, every
    résumé with an employment date would look quantified — `2023` matches any
    naive number pattern — and the "no quantified outcomes" rule would never
    fire on the documents that most need it.
    """
    stripped = _PHONE_LIKE_RE.sub(" ", text or "")
    stripped = _YEAR_RE.sub(" ", stripped)
    return bool(_DIGIT_RE.search(stripped))


def find_phones(text: str) -> list[str]:
    """Phone-like tokens, verified by digit count rather than by shape.

    Matching a single regex against every national format is a losing game, so
    this over-matches and then filters: 8-15 digits, and not a year range
    (`2020 - 2023` has eight digits and is not a phone number).
    """
    out: list[str] = []
    for match in _PHONE_LIKE_RE.finditer(text or ""):
        token = match.group(0).strip()
        digits = re.sub(r"\D", "", token)
        if not 8 <= len(digits) <= 15:
            continue
        if _YEAR_RANGE_RE.fullmatch(token):
            continue
        out.append(token)
    return out


def word_count(text: str) -> int:
    return len((text or "").split())


_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_ISO_MONTH_RE = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])$")


def format_month(value: str) -> str:
    """`2023-04` -> `Apr 2023`, anything else unchanged.

    The profile stores month precision as ISO because it sorts and round-trips
    cleanly; a résumé should read `Apr 2023`. Emitting the raw ISO form is also
    what made Compass's own generated variants fail its date-parseability rule.
    """
    value = (value or "").strip()
    match = _ISO_MONTH_RE.match(value)
    if not match:
        return value
    year, month = match.groups()
    return f"{_MONTHS[int(month) - 1]} {year}"


def format_range(start: str, end: str, is_current: bool) -> str:
    left = format_month(start)
    right = format_month(end) or ("Present" if is_current else "")
    if left and right:
        return f"{left} - {right}"
    return left or right


def normalise_company(name: str) -> str:
    """Strip legal suffixes so 'Acme Technologies Pvt. Ltd.' matches 'Acme'."""
    cleaned = re.sub(r"[^\w\s&]", " ", (name or "").lower())
    cleaned = re.sub(
        r"\b(pvt|private|ltd|limited|llp|inc|incorporated|corp|corporation|"
        r"co|company|gmbh|plc|technologies|technology|solutions|services|"
        r"labs|group|holdings|india)\b",
        " ",
        cleaned,
    )
    return re.sub(r"\s+", " ", cleaned).strip()
