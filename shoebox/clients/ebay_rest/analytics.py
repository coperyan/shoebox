from __future__ import annotations

from .session import EbaySession

_UNSET = object()
DEFAULT_METRICS = ["LISTING_VIEWS_TOTAL", "LISTING_IMPRESSION_TOTAL"]
DEFAULT_CHUNKSIZE = 200


def extract_field_names(payload: dict) -> list[str]:
    """
    Returns ordered field names matching the order of values in records.
    """
    header = payload["header"]

    dimension_fields = [d["key"] for d in header.get("dimension_keys", [])]
    metric_fields = [m["key"] for m in header.get("metrics", [])]

    return dimension_fields + metric_fields


def map_records(payload: dict) -> list[dict]:
    """
    Maps header fields to record values.
    """
    fields = extract_field_names(payload)
    rows = []

    for record in payload["records"]:
        values = []

        # dimensions first
        for dv in record.get("dimension_values", []):
            values.append(dv["value"] if dv.get("applicable") else None)

        # metrics second
        for mv in record.get("metric_values", []):
            values.append(mv["value"] if mv.get("applicable") else None)

        rows.append(dict(zip(fields, values, strict=False)))

    return rows


class AnalyticsClient:
    def __init__(self, session: EbaySession):
        self.api = session.api

    def get_traffic_report(
        self,
        *,
        date_from: str,
        date_to: str,
        listing_ids: list[str],
        metrics: list[str] | None = None,
        chunksize: int = 200,
    ) -> list[dict]:
        metrics = metrics or list(DEFAULT_METRICS)
        chunksize = chunksize or DEFAULT_CHUNKSIZE
        results = []
        for i in range(0, len(listing_ids), chunksize):
            iter_ids = listing_ids[i : i + chunksize]
            resp = self.api.sell_analytics_get_traffic_report(
                dimension="LISTING",
                metric=",".join(metrics),
                filter=f"date_range:[{date_from}..{date_to}],listing_ids:{{{'|'.join(iter_ids)}}}",
            )
            results.extend(map_records(resp))
        return results
