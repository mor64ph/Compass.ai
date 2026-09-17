"""Google Calendar integration (PRD Epic E).

Two directions:

* **Out** - place an interview slot with reminders, either from a time you type
  in or from a time Gmail found in an invite email.
* **In** - read upcoming events so the dashboard can show what is coming without
  you switching tabs.

Calendar writes are idempotent: the created event id is stored on an
`ApplicationEvent`, and `place_pending_interviews` skips anything already placed.
Re-running a sync therefore cannot litter the calendar with duplicates.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from dateutil import parser as date_parser
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Application, ApplicationEvent
from app.services import application_service
from app.services.google import oauth

logger = logging.getLogger(__name__)

DEFAULT_DURATION_MINUTES = 60
REMINDER_MINUTES = (24 * 60, 60, 10)


@dataclass
class PlacementSummary:
    connected: bool = True
    created: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)
    details: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "connected": self.connected,
            "created": self.created,
            "skipped": self.skipped,
            "errors": self.errors,
            "details": self.details,
        }


def create_interview_event(
    session: Session,
    application: Application,
    *,
    start: datetime,
    duration_minutes: int = DEFAULT_DURATION_MINUTES,
    note: str = "",
    source: str = "manual",
    dedupe_key: str | None = None,
) -> dict | None:
    # Owner comes from the application, which was fetched through a scoped
    # lookup - so the event always lands on the right person's calendar.
    service = oauth.service(session, application.user_id, "calendar", "v3")
    if service is None:
        return None

    posting = application.job_posting
    company = posting.company if posting else "Unknown company"
    title = posting.title if posting else "Interview"

    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    end = start + timedelta(minutes=duration_minutes)

    description_lines = [
        f"Interview for {title} at {company}.",
        "",
        f"Compass application #{application.id}",
    ]
    if posting and posting.url:
        description_lines.append(f"Posting: {posting.url}")
    if note:
        description_lines += ["", note]
    if application.talking_points:
        description_lines += ["", "Talking points:"]
        description_lines += [
            f"- {tp.get('point', '')}" for tp in application.talking_points[:5]
        ]

    body = {
        "summary": f"Interview: {title} - {company}",
        "description": "\n".join(description_lines),
        "start": {"dateTime": start.isoformat()},
        "end": {"dateTime": end.isoformat()},
        "reminders": {
            "useDefault": False,
            "overrides": [{"method": "popup", "minutes": m} for m in REMINDER_MINUTES],
        },
    }

    try:
        event = service.events().insert(calendarId="primary", body=body).execute()
    except Exception as exc:
        logger.exception("Calendar insert failed")
        raise RuntimeError(f"Could not create the calendar event: {exc}") from exc

    application_service.log_event(
        session,
        application,
        kind="interview_scheduled",
        summary=f"Interview placed on the calendar for {start:%d %b %Y, %H:%M}",
        detail={
            "google_event_id": event.get("id", ""),
            "html_link": event.get("htmlLink", ""),
            "start": start.isoformat(),
            "duration_minutes": duration_minutes,
        },
        source="calendar",
        external_id=dedupe_key or f"cal:{event.get('id', '')}",
        occurred_at=start.replace(tzinfo=None),
    )
    return event


def place_pending_interviews(session: Session, user_id: int) -> PlacementSummary:
    """Create calendar events for interview times Gmail extracted but that have
    not been placed yet."""
    summary = PlacementSummary()
    if oauth.load_credentials(session, user_id) is None:
        summary.connected = False
        summary.errors.append("Google account is not connected.")
        return summary

    # Events carry no user_id - join through the application to stay scoped.
    email_events = list(
        session.scalars(
            select(ApplicationEvent)
            .join(Application, ApplicationEvent.application_id == Application.id)
            .where(
                Application.user_id == user_id,
                ApplicationEvent.source == "gmail",
                ApplicationEvent.kind == "email",
            )
        )
    )
    already_placed = {
        (e.external_id or "")
        for e in session.scalars(
            select(ApplicationEvent)
            .join(Application, ApplicationEvent.application_id == Application.id)
            .where(
                Application.user_id == user_id,
                ApplicationEvent.kind == "interview_scheduled",
            )
        )
    }

    for event in email_events:
        raw = (event.detail or {}).get("interview_datetime", "")
        if not raw:
            continue
        dedupe_key = f"cal-from-email:{event.external_id}"
        if dedupe_key in already_placed:
            summary.skipped += 1
            continue

        start = parse_datetime(raw)
        if start is None:
            summary.errors.append(f"Could not read the date '{raw}' from an invite email.")
            continue

        application = session.get(Application, event.application_id)
        if application is None:
            continue
        try:
            create_interview_event(
                session,
                application,
                start=start,
                note=f"Auto-placed from email: {event.summary}",
                source="calendar",
                dedupe_key=dedupe_key,
            )
        except RuntimeError as exc:
            summary.errors.append(str(exc))
            continue
        summary.created += 1
        summary.details.append(
            f"{application.job_posting.company if application.job_posting else '?'} "
            f"- {start:%d %b %Y, %H:%M}"
        )
    return summary


def upcoming(
    session: Session, user_id: int, *, days: int = 21, max_results: int = 15
) -> list[dict]:
    service = oauth.service(session, user_id, "calendar", "v3")
    if service is None:
        return []
    now = datetime.now(timezone.utc)
    try:
        response = (
            service.events()
            .list(
                calendarId="primary",
                timeMin=now.isoformat(),
                timeMax=(now + timedelta(days=days)).isoformat(),
                singleEvents=True,
                orderBy="startTime",
                maxResults=max_results,
                q="Interview",
            )
            .execute()
        )
    except Exception as exc:
        logger.warning("Calendar list failed: %s", exc)
        return []

    out: list[dict] = []
    for item in response.get("items", []):
        start = item.get("start", {})
        out.append(
            {
                "summary": item.get("summary", ""),
                "start": start.get("dateTime") or start.get("date", ""),
                "link": item.get("htmlLink", ""),
            }
        )
    return out


def parse_datetime(raw: str) -> datetime | None:
    try:
        parsed = date_parser.isoparse(raw)
    except (ValueError, TypeError):
        try:
            parsed = date_parser.parse(raw)
        except (ValueError, TypeError, OverflowError):
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed
