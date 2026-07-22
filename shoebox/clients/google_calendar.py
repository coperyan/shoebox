from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from google.oauth2 import service_account
from googleapiclient.discovery import build

from shoebox.models.topps_release import ToppsRelease

logger = logging.getLogger(__name__)


SCOPES = ["https://www.googleapis.com/auth/calendar.events"]
EVENT_TZ = "America/Los_Angeles"
EVENT_START_HOUR = 9
EVENT_DURATION_MIN = 15
PRIVATE_KEY = "toppsReleaseKey"


@dataclass
class UpsertResult:
    created: bool
    event_id: str
    html_link: str | None


def _build_event_body(release: ToppsRelease) -> dict[str, Any]:
    tz = ZoneInfo(EVENT_TZ)
    start_dt = datetime.combine(release.release_date, time(EVENT_START_HOUR, 0), tzinfo=tz)
    end_dt = start_dt + timedelta(minutes=EVENT_DURATION_MIN)

    description_parts = []
    if release.url:
        description_parts.append(release.url)
    if release.description:
        description_parts.append(release.description)
    description = "\n\n".join(description_parts)

    summary = f"{release.name} (pre-order)" if release.is_pre_order else release.name
    return {
        "summary": summary,
        "description": description,
        "start": {"dateTime": start_dt.isoformat(), "timeZone": EVENT_TZ},
        "end": {"dateTime": end_dt.isoformat(), "timeZone": EVENT_TZ},
        "reminders": {
            "useDefault": False,
            "overrides": [
                {"method": "popup", "minutes": 1440},
                {"method": "popup", "minutes": 30},
            ],
        },
        "extendedProperties": {"private": {PRIVATE_KEY: release.stable_key}},
    }


class GoogleCalendarClient:
    def __init__(self, service: Any) -> None:
        self._service = service

    @classmethod
    def from_service_account_file(cls, path: str) -> GoogleCalendarClient:
        creds = service_account.Credentials.from_service_account_file(path, scopes=SCOPES)
        service = build("calendar", "v3", credentials=creds, cache_discovery=False)
        return cls(service)

    def upsert_event(self, release: ToppsRelease, calendar_id: str) -> UpsertResult:
        body = _build_event_body(release)
        existing = self._find_existing(calendar_id, release.stable_key)

        if existing is not None:
            event_id = existing["id"]
            updated = (
                self._service.events()
                .patch(calendarId=calendar_id, eventId=event_id, body=body)
                .execute()
            )
            logger.info("patched calendar event %s for %s", event_id, release.name)
            return UpsertResult(
                created=False,
                event_id=event_id,
                html_link=updated.get("htmlLink"),
            )

        created = self._service.events().insert(calendarId=calendar_id, body=body).execute()
        logger.info("inserted calendar event %s for %s", created.get("id"), release.name)
        return UpsertResult(
            created=True,
            event_id=created["id"],
            html_link=created.get("htmlLink"),
        )

    def _find_existing(self, calendar_id: str, key: str) -> dict[str, Any] | None:
        resp = (
            self._service.events()
            .list(
                calendarId=calendar_id,
                privateExtendedProperty=f"{PRIVATE_KEY}={key}",
                showDeleted=False,
                singleEvents=True,
                maxResults=2,
            )
            .execute()
        )
        items = resp.get("items") or []
        if not items:
            return None
        if len(items) > 1:
            logger.warning(
                "found %d events with key %s; using first (%s)",
                len(items),
                key,
                items[0].get("id"),
            )
        return items[0]
