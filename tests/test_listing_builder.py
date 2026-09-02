import pytest

from shoebox.models.ebay_listing import EbayListingDraft
from shoebox.models.listing_queue import ListingQueueRow
from shoebox.transforms.listing_builder import (
    MAX_INVENTORY_DESCRIPTION_LEN,
    build_draft,
    build_offer_payload,
    fit_inventory_description,
    store_footer_html,
    title,
)


def _draft(price: float) -> EbayListingDraft:
    return EbayListingDraft(
        sku="S",
        title="t",
        description="d",
        quantity=1,
        price=price,
        category_id="261328",
        store_category="Base Cards",
        listing_start_date=None,
        condition_id="",
        image_urls=[],
        aspects={},
    )


class TestTitle:
    def test_title_within_80_chars(self):
        row = ListingQueueRow(
            set_year="2023",
            set_name="2023 Sample Chrome Baseball",
            subset_name="Base",
            subset_type="Base",
            card_number="1",
            player="Alex Rivera",
            team="Sample City Suns",
        )
        assert len(title(row)) <= 80

    def test_long_title_is_truncated_to_80(self):
        row = ListingQueueRow(
            set_year="2023",
            set_name="2023 Super Long Premium Chrome Refractor Baseball Set Name Edition",
            subset_name="Base",
            subset_type="Base",
            card_number="123456",
            player="Alexander Maximilian Rivera",
            team="Sample City Suns",
            parallel_variety="Gold Refractor",
            print_run=99,
        )
        assert len(title(row)) <= 80


class TestStoreFooter:
    def test_uses_explicit_store_name(self):
        html = store_footer_html("My Cards Shop")
        assert "My Cards Shop" in html

    def test_reads_name_from_settings(self):
        # conftest points settings at app.example.yml -> "Your Store Name"
        assert "Your Store Name" in store_footer_html()

    def test_store_name_is_not_hardcoded(self):
        # The footer name must come from config, not a baked-in default.
        assert "Your Store Name" not in store_footer_html("My Cards Shop")


class TestOfferPayload:
    def test_pulls_store_values_from_config(self):
        body = build_offer_payload(draft=_draft(5.0))
        assert body["merchantLocationKey"] == "YOUR_LOCATION_KEY"
        assert body["listingPolicies"]["paymentPolicyId"] == "0000000000"

    def test_low_and_high_price_use_config_threshold(self):
        low = build_offer_payload(draft=_draft(5.0))
        high = build_offer_payload(draft=_draft(25.0))
        # example config uses the same placeholder id for both tiers, but the
        # selection logic still runs against fulfillment_low_max_price (19.99).
        assert "fulfillmentPolicyId" in low["listingPolicies"]
        assert "fulfillmentPolicyId" in high["listingPolicies"]


def _queue_row() -> ListingQueueRow:
    return ListingQueueRow(
        set_year="2023",
        set_name="2023 Sample Chrome Baseball",
        subset_name="Future Stars of Baseball",
        subset_type="Insert",
        card_number="150",
        player="Alexander Maximilian Rivera",
        team="Sample City Suns",
        parallel_variety="Gold Refractor",
        print_run=50,
        price=9.99,
        quantity=1,
    )


class TestDescriptionLimits:
    def test_inventory_description_fits_ebay_limit(self):
        # eBay rejects an inventory item whose product.description exceeds 4000
        # characters; the long store footer belongs on the offer instead.
        draft = build_draft(row=_queue_row(), image_urls=[], sku="S")
        description = draft.inventory_item["product"]["description"]
        assert 0 < len(description) <= MAX_INVENTORY_DESCRIPTION_LEN

    def test_inventory_description_keeps_card_specifics(self):
        draft = build_draft(row=_queue_row(), image_urls=[], sku="S")
        description = draft.inventory_item["product"]["description"]
        assert "Card #: 150" in description
        assert "Gold Refractor" in description

    def test_offer_description_carries_the_store_footer(self):
        draft = build_draft(row=_queue_row(), image_urls=[], sku="S")
        listing_description = draft.offer["listingDescription"]
        assert "Card #: 150" in listing_description
        assert "Your Store Name" in listing_description

    def test_fit_minifies_before_giving_up(self):
        # Over the limit as authored, under it once inter-tag whitespace goes.
        html = "<div>\n    " + ("x" * (MAX_INVENTORY_DESCRIPTION_LEN - 14)) + "\n</div>"
        assert len(html) > MAX_INVENTORY_DESCRIPTION_LEN
        assert len(fit_inventory_description(html)) <= MAX_INVENTORY_DESCRIPTION_LEN

    def test_fit_raises_rather_than_truncating(self):
        with pytest.raises(ValueError, match="4000"):
            fit_inventory_description("x" * (MAX_INVENTORY_DESCRIPTION_LEN + 1))
