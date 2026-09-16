"""Remembers which listings have already been through a price review.

``review-listings`` runs on a schedule and the listings it surfaces change
slowly: a card with 20 views and no watchers this week still has 20 views and
no watchers next week. Without a memory, every run would re-raise the same
cards until they finally sold, and a review you have learned to scroll past is
worth nothing. A decision -- repriced, or explicitly kept -- puts that listing
to sleep for ``review.cooldown_days``.

Only decisions are recorded. A prompt that was skipped, or that nobody
answered before the deadline, deliberately leaves no trace, so it comes back on
the next run.

The file is small (one line per decided listing, pruned to listings that are
still live), so unlike ``clients/search_state.py`` this rewrites the whole
document on each write rather than keeping an append log to compact.

Layout under ``<exports_dir>/jsonl/reviews/``::

    price_review_state.json    item_id -> last decision
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

from ..models.listing_review import OUTCOME_KEPT, OUTCOME_REPRICED, ReviewRecord
from ..settings import Settings, get_settings

logger = logging.getLogger(__name__)

STATE_FILENAME = "price_review_state.json"

VALID_OUTCOMES = frozenset({OUTCOME_REPRICED, OUTCOME_KEPT})


class ReviewStateStore:
    """Per-listing review history, kept as one JSON document."""

    def __init__(
        self,
        *,
        base_dir: Path | None = None,
        settings: Settings | None = None,
    ) -> None:
        if base_dir is None:
            settings = settings or get_settings()
            base_dir = Path(settings.paths.exports_dir) / "jsonl" / "reviews"
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.state_path = self.base_dir / STATE_FILENAME

    def load(self) -> dict[str, ReviewRecord]:
        if not self.state_path.exists():
            return {}
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            # An unreadable history costs at most one round of repeat prompts,
            # which is a far better failure than refusing to run the review.
            logger.warning("Unreadable %s; treating every listing as unreviewed", self.state_path)
            return {}

        records: dict[str, ReviewRecord] = {}
        for item_id, value in (raw or {}).items():
            try:
                records[item_id] = ReviewRecord(**{"item_id": item_id, **value})
            except Exception:
                logger.warning("Skipping malformed review record for %s", item_id)
        return records

    def save(self, records: dict[str, ReviewRecord]) -> None:
        payload = {
            item_id: record.model_dump(mode="json", exclude={"item_id"})
            for item_id, record in sorted(records.items())
        }
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(self.state_path)

    def record(
        self,
        item_id: str,
        *,
        outcome: str,
        price: float | None = None,
        new_price: float | None = None,
        at: datetime | None = None,
    ) -> ReviewRecord:
        """Store a decision, bumping this listing's review count."""
        if outcome not in VALID_OUTCOMES:
            raise ValueError(f"outcome must be one of {sorted(VALID_OUTCOMES)}; got {outcome!r}")

        records = self.load()
        previous = records.get(item_id)
        record = ReviewRecord(
            item_id=item_id,
            last_reviewed_at=at or datetime.now(UTC),
            outcome=outcome,
            price=price,
            new_price=new_price,
            times_reviewed=(previous.times_reviewed + 1) if previous else 1,
        )
        records[item_id] = record
        self.save(records)
        return record

    def prune(self, live_item_ids: Iterable[str]) -> int:
        """Drop records for listings that are no longer live. Returns the count.

        A sold or relisted card never comes back under the same item id, so its
        history is dead weight -- and a relist is a genuinely new listing that
        should be reviewable on its own merits.
        """
        live = set(live_item_ids)
        records = self.load()
        kept = {item_id: r for item_id, r in records.items() if item_id in live}
        dropped = len(records) - len(kept)
        if dropped:
            self.save(kept)
            logger.info("Pruned %d review record(s) for listings no longer live", dropped)
        return dropped
