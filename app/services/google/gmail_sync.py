"""Gmail-driven application tracking (PRD Epic E, Gap 5).

Every tracker in the market relies on you remembering to update it. This closes
that loop: read the user's own inbox, classify what arrived, and move the card.

Two design decisions worth stating outright:

* **A cheap deterministic pass runs first.** ATS sender domains and unambiguous
  subject lines are matched by rule, so most of a real inbox is dismissed
  without an LLM call. Only the genuinely ambiguous remainder is classified.
* **Confidence gates the write.** Below the threshold, the email is filed
  against the application as an event for the user to read, but the stage is not
  moved. A wrong automatic stage change silently corrupts the tracker, which is
  worse than doing nothing.
"""

from __future__ import annotations

import base64
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.llm import LLMUnavailable, get_llm
from app.models import Application, ApplicationEvent, Stage, SyncState, utcnow
from app.schemas import LLMEmailBatchClassification
from app.services import application_service
from app.services.google import oauth
from app.services.textutil import normalise_company

logger = logging.getLogger(__name__)

# ATS platforms send on behalf of the hiring company. Mail from these domains is
# almost always about an application, which makes them a high-precision filter.
ATS_DOMAINS = (
    "greenhouse.io", "us.greenhouse-mail.io", "lever.co", "hire.lever.co",
    "ashbyhq.com", "myworkday.com", "myworkdayjobs.com", "smartrecruiters.com",
    "icims.com", "taleo.net", "successfactors.com", "workable.com",
    "breezy.hr", "recruitee.com", "teamtailor.com", "jazzhr.com",
    "bamboohr.com", "personio.de", "zohorecruit.com", "keka.com", "darwinbox.in",
)

SUBJECT_HINTS = (
    "your application", "application received", "thank you for applying",
    "application for", "interview", "next steps", "assessment", "coding challenge",
    "take-home", "offer", "we regret", "unfortunately", "not moving forward",
    "candidacy", "recruitment", "shortlisted", "screening call",
)

# Deterministic rules, checked in order. First match wins.
RULE_PATTERNS: list[tuple[str, tuple[str, ...]]] = [
    ("rejection", (
        "we regret to inform", "unfortunately we", "not be moving forward",
        "not moving forward", "decided to move forward with other",
        "will not be progressing", "pursue other candidates",
        "keep your resume on file", "keep your cv on file",
        "no longer under consideration", "position has been filled",
    )),
    ("interview_invite", (
        "schedule an interview", "invitation to interview", "interview invitation",
        "book a time", "find a time", "availability for a call",
        "would like to speak", "schedule a call", "screening call",
        "invite you to interview", "next round",
    )),
    ("offer", ("pleased to offer", "offer letter", "we would like to offer",
               "extend an offer", "compensation package")),
    ("assessment_request", ("take-home", "take home assignment", "coding challenge",
                            "online assessment", "technical assessment", "hackerrank",
                            "codility", "complete the test")),
    ("application_confirmation", (
        "we have received your application", "application received",
        "thank you for applying", "thanks for applying",
        "your application has been submitted", "application confirmation",
    )),
]

CATEGORY_TO_STAGE = {
    "application_confirmation": Stage.APPLIED.value,
    "assessment_request": Stage.SCREEN.value,
    "interview_invite": Stage.INTERVIEW.value,
    "offer": Stage.OFFER.value,
    "rejection": Stage.CLOSED.value,
}

CONFIDENCE_FLOOR = 0.6
SEEN_IDS_KEY = "gmail_seen_message_ids"
MAX_SEEN_IDS = 3000
BATCH_SIZE = 10
MAX_MESSAGES_PER_RUN = 120
BODY_CHAR_LIMIT = 1200


@dataclass
class SyncSummary:
    connected: bool = True
    scanned: int = 0
    already_seen: int = 0
    classified_by_rule: int = 0
    classified_by_llm: int = 0
    events_created: int = 0
    stages_advanced: int = 0
    low_confidence: int = 0
    unmatched: int = 0
    errors: list[str] = field(default_factory=list)
    details: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "connected": self.connected,
            "scanned": self.scanned,
            "already_seen": self.already_seen,
            "classified_by_rule": self.classified_by_rule,
            "classified_by_llm": self.classified_by_llm,
            "events_created": self.events_created,
            "stages_advanced": self.stages_advanced,
            "low_confidence": self.low_confidence,
            "unmatched": self.unmatched,
            "errors": self.errors,
            "details": self.details,
        }


@dataclass
class Email:
    id: str
    thread_id: str
    sender: str
    subject: str
    date: datetime | None
    body: str

    def as_prompt_block(self, index: int) -> str:
        stamp = self.date.isoformat() if self.date else "unknown date"
        return (
            f"--- Email {index} ---\n"
            f"From: {self.sender}\n"
            f"Subject: {self.subject}\n"
            f"Date: {stamp}\n"
            f"Body: {self.body}"
        )


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def sync(session: Session, user_id: int) -> SyncSummary:
    service = oauth.service(session, user_id, "gmail", "v1")
    if service is None:
        return SyncSummary(connected=False, errors=["Google account is not connected."])

    summary = SyncSummary()
    applications = list(
        session.scalars(
            select(Application).where(
                Application.user_id == user_id,
                Application.stage != Stage.CLOSED.value,
            )
        )
    )
    if not applications:
        summary.errors.append(
            "No open applications to match against - add one before syncing."
        )
        return summary

    _record_account_email(session, user_id, service)

    seen = _load_seen_ids(session, user_id)
    try:
        message_ids = _search(service, applications)
    except Exception as exc:
        logger.exception("Gmail search failed")
        summary.errors.append(f"Gmail search failed: {exc}")
        return summary

    fresh = [mid for mid in message_ids if mid not in seen]
    summary.already_seen = len(message_ids) - len(fresh)
    fresh = fresh[:MAX_MESSAGES_PER_RUN]

    emails: list[Email] = []
    for message_id in fresh:
        try:
            emails.append(_fetch(service, message_id))
        except Exception as exc:
            logger.warning("Could not fetch message %s: %s", message_id, exc)
            summary.errors.append(f"Could not fetch one message: {exc}")
    summary.scanned = len(emails)

    # Pass 1 - rules.
    needs_llm: list[Email] = []
    classifications: dict[str, dict] = {}
    for email in emails:
        verdict = _rule_classify(email)
        if verdict is None:
            needs_llm.append(email)
        else:
            classifications[email.id] = verdict
            summary.classified_by_rule += 1

    # Pass 2 - LLM, only for what the rules could not settle.
    if needs_llm:
        try:
            for result in _llm_classify(needs_llm, applications):
                classifications[result["_email_id"]] = result
                summary.classified_by_llm += 1
        except LLMUnavailable as exc:
            summary.errors.append(f"LLM classification skipped: {exc}")
        except Exception as exc:
            logger.exception("LLM classification failed")
            summary.errors.append(f"LLM classification failed: {exc}")

    for email in emails:
        verdict = classifications.get(email.id)
        seen.add(email.id)
        if verdict is None or verdict["category"] == "other":
            continue
        _apply(session, email, verdict, applications, summary)

    _save_seen_ids(session, user_id, seen)
    return summary


# --------------------------------------------------------------------------
# Gmail access
# --------------------------------------------------------------------------


def _search(service, applications: list[Application]) -> list[str]:
    days = get_settings().compass_gmail_lookback_days
    domain_clause = " OR ".join(f"from:{d}" for d in ATS_DOMAINS)
    subject_clause = " OR ".join(f'subject:"{s}"' for s in SUBJECT_HINTS[:12])

    companies = []
    for application in applications:
        name = (application.job_posting.company if application.job_posting else "").strip()
        if name and name not in companies:
            companies.append(name)
    company_clause = " OR ".join(f'"{c}"' for c in companies[:15])

    clauses = [f"({domain_clause})", f"({subject_clause})"]
    if company_clause:
        clauses.append(f"({company_clause})")
    query = f"newer_than:{days}d ({' OR '.join(clauses)}) -in:spam -in:trash"

    ids: list[str] = []
    page_token = None
    while True:
        response = (
            service.users()
            .messages()
            .list(userId="me", q=query, maxResults=100, pageToken=page_token)
            .execute()
        )
        ids += [m["id"] for m in response.get("messages", [])]
        page_token = response.get("nextPageToken")
        if not page_token or len(ids) >= MAX_MESSAGES_PER_RUN * 2:
            break
    return ids


def _fetch(service, message_id: str) -> Email:
    message = (
        service.users().messages().get(userId="me", id=message_id, format="full").execute()
    )
    payload = message.get("payload", {})
    headers = {h["name"].lower(): h["value"] for h in payload.get("headers", [])}

    date = None
    internal = message.get("internalDate")
    if internal:
        try:
            date = datetime.fromtimestamp(int(internal) / 1000, tz=timezone.utc)
        except (TypeError, ValueError):
            date = None

    body = _extract_body(payload) or message.get("snippet", "")
    return Email(
        id=message_id,
        thread_id=message.get("threadId", ""),
        sender=headers.get("from", ""),
        subject=headers.get("subject", ""),
        date=date,
        body=_clean_body(body)[:BODY_CHAR_LIMIT],
    )


def _extract_body(payload: dict) -> str:
    """Depth-first walk preferring text/plain; HTML is stripped as a fallback."""
    mime = payload.get("mimeType", "")
    data = payload.get("body", {}).get("data")

    if mime == "text/plain" and data:
        return _b64(data)
    if mime == "text/html" and data:
        return re.sub(r"<[^>]+>", " ", _b64(data))

    for part in payload.get("parts", []) or []:
        text = _extract_body(part)
        if text.strip():
            return text
    return ""


def _b64(data: str) -> str:
    try:
        return base64.urlsafe_b64decode(data.encode("utf-8")).decode("utf-8", errors="replace")
    except Exception:
        return ""


def _clean_body(text: str) -> str:
    text = re.sub(r"https?://\S+", "[link]", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _record_account_email(session: Session, user_id: int, service) -> None:
    from app.models import GoogleToken

    row = session.scalars(
        select(GoogleToken).where(GoogleToken.user_id == user_id)
    ).first()
    if row is None or row.account_email:
        return
    try:
        profile = service.users().getProfile(userId="me").execute()
        row.account_email = profile.get("emailAddress", "")
        session.commit()
    except Exception as exc:
        logger.debug("Could not read Gmail profile: %s", exc)


# --------------------------------------------------------------------------
# Classification
# --------------------------------------------------------------------------


def _rule_classify(email: Email) -> dict | None:
    haystack = f"{email.subject}\n{email.body}".lower()
    sender_is_ats = any(domain in email.sender.lower() for domain in ATS_DOMAINS)

    for category, needles in RULE_PATTERNS:
        if any(needle in haystack for needle in needles):
            return {
                "_email_id": email.id,
                "category": category,
                "company": _guess_company(email),
                "role_title": "",
                # A rule hit on an ATS sender is about as certain as this gets;
                # the same phrase in a newsletter is not, so it stays below the
                # floor and only files an event.
                "confidence": 0.9 if sender_is_ats else 0.72,
                "reasoning": f"Matched the {category} rule on subject/body wording.",
                "interview_datetime": "",
                "by": "rule",
            }

    # Obvious non-candidates: dismiss without paying for a classification.
    if any(
        marker in haystack
        for marker in ("unsubscribe from job alerts", "jobs matching your profile",
                       "new jobs for you", "recommended jobs", "newsletter")
    ) and not sender_is_ats:
        return {
            "_email_id": email.id,
            "category": "other",
            "company": "",
            "role_title": "",
            "confidence": 0.9,
            "reasoning": "Job-alert or newsletter mail.",
            "interview_datetime": "",
            "by": "rule",
        }
    return None


def _guess_company(email: Email) -> str:
    """Best-effort from the From header display name, e.g.
    `"Acme Careers" <no-reply@greenhouse.io>` -> `Acme Careers`."""
    match = re.match(r'\s*"?([^"<]+?)"?\s*<', email.sender)
    if match:
        return match.group(1).strip()
    match = re.search(r"@([\w.-]+)", email.sender)
    if match:
        domain = match.group(1)
        if not any(ats in domain for ats in ATS_DOMAINS):
            return domain.split(".")[0]
    return ""


def _llm_classify(emails: list[Email], applications: list[Application]) -> list[dict]:
    llm = get_llm()
    tracked = [
        {
            "company": a.job_posting.company if a.job_posting else "",
            "role_title": a.job_posting.title if a.job_posting else "",
            "stage": a.stage,
        }
        for a in applications
    ]

    out: list[dict] = []
    for start in range(0, len(emails), BATCH_SIZE):
        chunk = emails[start : start + BATCH_SIZE]
        user = "\n\n".join(e.as_prompt_block(i + 1) for i, e in enumerate(chunk))
        result = llm.structured(
            "email_classify",
            user=user,
            output_model=LLMEmailBatchClassification,
            prompt_vars={"applications_json": tracked},
            category="classify",
        )
        results = result.value.results
        if len(results) != len(chunk):
            logger.warning(
                "Classifier returned %d results for %d emails - pairing by position "
                "up to the shorter length",
                len(results),
                len(chunk),
            )
        for email, classification in zip(chunk, results, strict=False):
            payload = classification.model_dump()
            payload["_email_id"] = email.id
            payload["by"] = "llm"
            out.append(payload)
    return out


# --------------------------------------------------------------------------
# Applying results
# --------------------------------------------------------------------------


def _apply(
    session: Session,
    email: Email,
    verdict: dict,
    applications: list[Application],
    summary: SyncSummary,
) -> None:
    application = _match_application(verdict, email, applications)
    if application is None:
        summary.unmatched += 1
        summary.details.append(
            {
                "subject": email.subject,
                "category": verdict["category"],
                "outcome": "no matching application - review manually",
            }
        )
        return

    # Unique on (source, external_id), so a re-run cannot duplicate an event.
    existing = session.scalars(
        select(ApplicationEvent).where(
            ApplicationEvent.source == "gmail", ApplicationEvent.external_id == email.id
        )
    ).first()
    if existing is not None:
        return

    application_service.log_event(
        session,
        application,
        kind="email",
        summary=f"[{verdict['category']}] {email.subject}",
        detail={
            "from": email.sender,
            "category": verdict["category"],
            "confidence": verdict.get("confidence", 0.0),
            "reasoning": verdict.get("reasoning", ""),
            "classified_by": verdict.get("by", "llm"),
            "interview_datetime": verdict.get("interview_datetime", ""),
        },
        source="gmail",
        external_id=email.id,
        # Gmail hands back an aware timestamp; timestamp columns are naive UTC.
        occurred_at=(email.date.replace(tzinfo=None) if email.date else None) or utcnow(),
    )
    summary.events_created += 1

    confidence = float(verdict.get("confidence") or 0.0)
    target_stage = CATEGORY_TO_STAGE.get(verdict["category"])

    if target_stage is None:
        return
    if confidence < CONFIDENCE_FLOOR:
        summary.low_confidence += 1
        summary.details.append(
            {
                "subject": email.subject,
                "category": verdict["category"],
                "outcome": (
                    f"filed against {application.job_posting.company} but stage left "
                    f"alone (confidence {confidence:.0%})"
                ),
            }
        )
        return

    moved = application_service.advance_stage(
        session,
        application,
        target_stage,
        source="gmail",
        summary=f"Gmail: {verdict['category']} - {email.subject}",
        external_id=f"stage:{email.id}",
    )
    if moved:
        summary.stages_advanced += 1
    summary.details.append(
        {
            "subject": email.subject,
            "category": verdict["category"],
            "outcome": (
                f"{application.job_posting.company} -> {target_stage}"
                if moved
                else f"{application.job_posting.company}: event filed, stage unchanged"
            ),
        }
    )


def _match_application(
    verdict: dict, email: Email, applications: list[Application]
) -> Application | None:
    candidates = applications
    company = normalise_company(verdict.get("company", ""))

    if company:
        exact = [
            a
            for a in candidates
            if a.job_posting and normalise_company(a.job_posting.company) == company
        ]
        if len(exact) == 1:
            return exact[0]
        if exact:
            candidates = exact
        else:
            partial = [
                a
                for a in candidates
                if a.job_posting
                and company
                and (
                    company in normalise_company(a.job_posting.company)
                    or normalise_company(a.job_posting.company) in company
                )
            ]
            if len(partial) == 1:
                return partial[0]
            if partial:
                candidates = partial

    # Narrow a remaining ambiguity by role title mentioned in the email.
    title = (verdict.get("role_title") or "").lower()
    haystack = f"{email.subject} {email.body}".lower()
    if len(candidates) > 1:
        titled = [
            a
            for a in candidates
            if a.job_posting
            and a.job_posting.title
            and (
                a.job_posting.title.lower() in haystack
                or (title and a.job_posting.title.lower() in title)
            )
        ]
        if len(titled) == 1:
            return titled[0]

    # Only auto-attach when there is exactly one plausible target. Guessing
    # between two open applications at the same company is how a tracker starts
    # lying to you.
    return candidates[0] if len(candidates) == 1 and company else None


# --------------------------------------------------------------------------
# Seen-id bookkeeping
# --------------------------------------------------------------------------


def _seen_row(session: Session, user_id: int) -> SyncState | None:
    return session.scalars(
        select(SyncState).where(
            SyncState.user_id == user_id, SyncState.key == SEEN_IDS_KEY
        )
    ).first()


def _load_seen_ids(session: Session, user_id: int) -> set[str]:
    row = _seen_row(session, user_id)
    if row is None or not row.value:
        return set()
    try:
        return set(json.loads(row.value))
    except json.JSONDecodeError:
        return set()


def _save_seen_ids(session: Session, user_id: int, ids: set[str]) -> None:
    trimmed = list(ids)[-MAX_SEEN_IDS:]
    row = _seen_row(session, user_id)
    if row is None:
        session.add(
            SyncState(user_id=user_id, key=SEEN_IDS_KEY, value=json.dumps(trimmed))
        )
    else:
        row.value = json.dumps(trimmed)
    session.commit()
