"""Types for the periodic price review (``review-listings``).

``ReviewCandidate`` is one live listing the review picked out, already carrying
the numbers the Slack prompt shows and the price it proposes. ``ReviewRecord``
is the memory of a decision, persisted by ``clients/review_state.py`` so a
listing that has just been dealt with isn't raised again next run.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import Field

from .common import Model

# What was decided about a listing. Only these two are persisted: a skipped or
# unanswered prompt deliberately leaves no record, so it comes back next run.
OUTCOME_REPRICED = "repriced"
OUTCOME_KEPT = "kept"


class ReviewCandidate(Model):
    """A live listing whose asking price looks like it has stopped working."""

    item_id: str
    title: str
    sku: str | None = None
    price: float
    suggested_price: float
    views: int = 0
    impressions: int = 0
    watchers: int = 0
    age_days: int = 0
    # How many times this listing has been through a review before.
    times_reviewed: int = 0
    view_item_url: str | None = None
    picture_url: str | None = None

    @property
    def views_per_day(self) -> float:
        return self.views / max(self.age_days, 1)

    @property
    def markdown(self) -> float:
        """Dollars the suggestion takes off the current price."""
        return round(self.price - self.suggested_price, 2)

    @property
    def markdown_pct(self) -> float:
        if not self.price:
            return 0.0
        return round(self.markdown / self.price * 100, 1)


class ReviewRecord(Model):
    """The last decision made about one listing."""

    item_id: str
    last_reviewed_at: datetime
    outcome: str
    # The price at the time of the decision, and what it became (None when kept).
    price: float | None = None
    new_price: float | None = None
    times_reviewed: int = Field(default=1, ge=1)
