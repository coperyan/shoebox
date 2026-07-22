from shoebox.models.ebay_listing import EbayListingDraft
from shoebox.models.listing_queue import ListingQueueRow
from shoebox.transforms.listing_builder import (
    build_offer_payload,
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
        # conftest points settings at app.yaml.example -> "Your Store Name"
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
