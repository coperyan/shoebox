"""Sync upcoming Topps product releases into Google Calendar.

Scrapes https://www.topps.com/release-calendar with a real browser, then
upserts each upcoming release as a 9:00 AM PT calendar event with reminders
30 minutes and 1 day before. Newly created events are announced to the
configured Slack notify channel.

Run: ``python -m shoebox.pipelines.sync_topps_calendar [--dry-run]``
"""

from __future__ import annotations

import argparse
import logging
from datetime import date

from shoebox.clients.google_calendar import (
    GoogleCalendarClient,
    UpsertResult,
)
from shoebox.clients.topps_release_scraper import ToppsReleaseScraper
from shoebox.models.topps_release import ToppsRelease
from shoebox.settings import get_settings
from shoebox.utils.slack import notify

logger = logging.getLogger(__name__)


def _format_release_message(release: ToppsRelease, result: UpsertResult) -> str:
    lines = [
        "📅 New Topps release added to calendar",
        release.name,
        f"Release date: {release.release_date.strftime('%a, %b %d %Y').replace(' 0', ' ')}",
        "Calendar event: 9:00 AM PT",
    ]
    if release.url:
        lines.append(release.url)
    if result.html_link:
        lines.append(result.html_link)
    return "\n".join(lines)


def run(dry_run: bool = False, headless: bool = True) -> int:
    settings = get_settings()
    today = date.today()

    scraper = ToppsReleaseScraper(headless=headless)
    scraper.start()
    try:
        releases = scraper.fetch_releases()
    finally:
        scraper.close()

    upcoming = [r for r in releases if r.release_date >= today]
    logger.info("scraped %d releases (%d upcoming)", len(releases), len(upcoming))

    if dry_run:
        for r in upcoming:
            pre_order_flag = " [pre-order]" if r.is_pre_order else ""
            print(f"{r.release_date.isoformat()}  {r.name}{pre_order_flag}  {r.url or ''}")
        return 0

    cal = GoogleCalendarClient.from_service_account_file(settings.gcp.service_account_json)
    calendar_id = settings.google_calendar.calendar_id

    created_count = 0
    patched_count = 0
    for r in upcoming:
        try:
            result = cal.upsert_event(r, calendar_id=calendar_id)
        except Exception as e:
            logger.exception("failed to upsert %s: %s", r.name, e)
            continue

        if result.created:
            created_count += 1
            try:
                notify(
                    settings.slack.notify_channel,
                    _format_release_message(r, result),
                )
            except Exception as e:
                logger.warning("slack notify failed for %s: %s", r.name, e)
        else:
            patched_count += 1

    logger.info(
        "topps calendar sync done: %d created, %d patched",
        created_count,
        patched_count,
    )
    return 0


def main() -> int:
    from shoebox.utils.logging_setup import setup_logging

    setup_logging()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print scraped releases without writing to Google Calendar or Slack.",
    )
    parser.add_argument(
        "--no-headless",
        action="store_true",
        help="Show the browser window (useful for debugging the scraper).",
    )
    args = parser.parse_args()
    return run(dry_run=args.dry_run, headless=not args.no_headless)


if __name__ == "__main__":
    raise SystemExit(main())
