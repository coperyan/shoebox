from __future__ import annotations

import hashlib

from shoebox.models.listing_queue import ListingQueueRow


def build_sku_50(row: ListingQueueRow, *, prefix: str = "") -> str:
    """Generate a stable 50-character SKU based on listing metadata."""

    base = "|".join(
        [
            str(row.set_year or ""),
            row.set_name or "",
            row.subset_name or "",
            row.parallel_variety or "",
            row.card_number or "",
            row.player or "",
        ]
    )
    digest = hashlib.sha1(base.encode("utf-8")).hexdigest().upper()  # 40 chars
    prefix_clean = "".join([c for c in (prefix or "").upper() if c.isalnum()])
    sku = (prefix_clean + digest)[:50]
    return sku.ljust(50, "0")
