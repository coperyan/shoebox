from datetime import UTC, datetime, timedelta

from ...models.ebay.order import Order
from .session import EbaySession


def _min_date() -> datetime:
    return datetime.now(UTC) - timedelta(days=729)


def _today() -> datetime:
    return datetime.now(UTC) - timedelta(seconds=30)


def dt_to_str(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def to_inclusive_ranges(ranges):
    return [(s, e - timedelta(microseconds=1)) for s, e in ranges]


def date_queue(
    start_date: datetime, end_date: datetime, chunksize_days: int = 85
) -> list[tuple[datetime, datetime]]:
    """
    Returns half-open ranges [start, end) that fully cover [start_date, end_date)
    without gaps or overlaps.
    """
    if end_date <= start_date:
        return []

    chunk = timedelta(days=chunksize_days)
    chunks = []
    current_start = start_date

    while current_start < end_date:
        current_end = min(current_start + chunk, end_date)
        chunks.append((current_start, current_end))
        current_start = current_end  # <- no gaps

    return to_inclusive_ranges(chunks)


class FulfillmentClient:
    def __init__(self, session: EbaySession):
        self.api = session.api

    def get_orders(
        self,
        start_date: str = None,
        end_date: str = None,
        trailing_x_days: int = None,
        status: str = "FULFILLED",  # or NOT_STARTED
    ) -> list[Order]:
        if trailing_x_days:
            end_date = _today()
            start_date = end_date - timedelta(days=trailing_x_days)
        else:
            if not start_date:
                start_date = _min_date()
            else:
                start_date = datetime.strptime(start_date, "%Y-%m-%d").astimezone(UTC)
            if not end_date:
                end_date = _today()
            else:
                end_date = datetime.strptime(end_date, "%Y-%m-%d").astimezone(UTC)
                if end_date >= _today():
                    end_date = _today()
        date_filters = date_queue(start_date=start_date, end_date=end_date)
        status_list = "|".join([status, "IN_PROGRESS"])
        results = []
        for dates in date_filters:
            filter_str = f"creationdate:[{dt_to_str(dates[0])}..{dt_to_str(dates[1])}]"
            if status_list:
                filter_str += f",orderfulfillmentstatus:{{{status_list}}}"
            iter_resp = self.api.sell_fulfillment_get_orders(filter=filter_str)
            results.extend([x["record"] for x in iter_resp if "record" in x])
        return [Order.from_api(r) for r in results]
