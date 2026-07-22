from shoebox.models.listing_queue import ListingQueueRow
from shoebox.utils.sku import build_sku_50


def _row(**kw) -> ListingQueueRow:
    base = dict(
        set_year="2023",
        set_name="2023 Sample Chrome",
        subset_name="Base",
        parallel_variety="Refractor",
        card_number="1",
        player="Alex Rivera",
    )
    base.update(kw)
    return ListingQueueRow(**base)


def test_sku_is_50_chars():
    assert len(build_sku_50(_row())) == 50


def test_sku_is_deterministic():
    assert build_sku_50(_row()) == build_sku_50(_row())


def test_sku_changes_with_card_number():
    assert build_sku_50(_row(card_number="1")) != build_sku_50(_row(card_number="2"))


def test_prefix_is_applied_and_alnum_only():
    sku = build_sku_50(_row(), prefix="ab-1!")
    assert sku.startswith("AB1")
    assert len(sku) == 50


def test_missing_fields_do_not_raise():
    row = ListingQueueRow(set_name="X")
    assert len(build_sku_50(row)) == 50
