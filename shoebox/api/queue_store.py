"""The queue objects in GCS: the enriched JSONL ``create-listings`` reads, and
the raw-rows draft document the phone reloads."""

from __future__ import annotations

import json
from typing import Any

from shoebox.clients.gcs import parse_gs_uri

QUEUE_FILENAME = "listing_queue_enriched.jsonl"
DRAFT_FILENAME = "listing_queue_draft.json"


def draft_uri_for(queue_uri: str) -> str:
    """The draft document lives next to the queue object."""
    bucket, name = parse_gs_uri(queue_uri)
    prefix = name.rsplit("/", 1)[0] + "/" if "/" in name else ""
    return f"gs://{bucket}/{prefix}{DRAFT_FILENAME}"


class QueueStore:
    """Reads and writes the two queue objects. ``gcs`` needs ``upload_text`` and
    ``read_text`` (``GCSClient``, or a fake in tests)."""

    def __init__(self, gcs: Any, queue_uri: str):
        self.gcs = gcs
        self.queue_uri = queue_uri
        self.draft_uri = draft_uri_for(queue_uri)

    def write_queue(self, enriched: list[dict[str, Any]]) -> None:
        bucket, name = parse_gs_uri(self.queue_uri)
        text = "".join(json.dumps(rec, ensure_ascii=False) + "\n" for rec in enriched)
        self.gcs.upload_text(
            bucket=bucket, object_name=name, text=text, content_type="application/json"
        )

    def write_draft(self, doc: dict[str, Any]) -> None:
        bucket, name = parse_gs_uri(self.draft_uri)
        self.gcs.upload_text(
            bucket=bucket,
            object_name=name,
            text=json.dumps(doc, ensure_ascii=False),
            content_type="application/json",
        )

    def read_draft(self) -> dict[str, Any] | None:
        bucket, name = parse_gs_uri(self.draft_uri)
        try:
            text = self.gcs.read_text(bucket=bucket, object_name=name)
        except FileNotFoundError:
            return None
        return json.loads(text)
