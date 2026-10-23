import pytest

from shoebox.models.ebay_listing import EbayListingDraft
from shoebox.models.listing_queue import ListingQueueRow
from shoebox.transforms.listing_builder import (
    MAX_INVENTORY_DESCRIPTION_LEN,
    build_draft,
    build_offer_payload,
    fit_inventory_description,
    live_store_category_names,
    store_categories,
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


class TestStoreCategories:
    """New listings must use live ``/path`` names or eBay files them under Other."""

    def test_baseball_hit_gets_team_and_hits_paths(self):
        row = _queue_row().model_copy(update={"team": "San Francisco Giants"})
        assert store_categories(row) == [
            "/Baseball Singles/San Francisco Giants",
            "/Hits/Numbered",
        ]

    def test_base_card_gets_only_its_team(self):
        row = _queue_row().model_copy(
            update={
                "team": "Chicago Cubs",
                "subset_name": "Base",
                "subset_type": "Base",
                "parallel_variety": "",
                "print_run": None,
            }
        )
        assert store_categories(row) == ["/Baseball Singles/Chicago Cubs"]

    def test_autograph_outranks_numbering(self):
        row = _queue_row().model_copy(
            update={"team": "New York Mets", "subset_name": "Rookie Autographs"}
        )
        assert store_categories(row)[1] == "/Hits/Autographs"

    def test_relic_from_parallel_note(self):
        row = _queue_row().model_copy(
            update={"team": "New York Mets", "print_run": None, "parallel_note": "MEM"}
        )
        assert store_categories(row)[1] == "/Hits/Relics"

    def test_football_stays_flat_but_keeps_hits(self):
        row = _queue_row().model_copy(
            update={"set_name": "2023 Sample Football", "team": "Dallas Cowboys"}
        )
        assert store_categories(row) == ["/Football Singles", "/Hits/Numbered"]

    def test_unknown_baseball_team_lands_in_other_teams(self):
        assert store_categories(_queue_row())[0] == "/Baseball Singles/Other Teams"

    def test_draft_and_offer_carry_both_paths(self):
        row = _queue_row().model_copy(update={"team": "San Francisco Giants"})
        draft = build_draft(row=row, image_urls=[], sku="S")
        assert draft.store_categories == [
            "/Baseball Singles/San Francisco Giants",
            "/Hits/Numbered",
        ]
        assert draft.store_category == "/Baseball Singles/San Francisco Giants"
        assert draft.offer["storeCategoryNames"] == draft.store_categories

    def test_no_flat_legacy_names_survive(self):
        legacy = {"Base Cards", "Parallels", "Inserts", "Serial Numbered", "Autographs", "Relics"}
        for name in store_categories(_queue_row()):
            assert name.startswith("/")
            assert name not in legacy

    def test_offer_payload_falls_back_to_the_single_category(self):
        offer = build_offer_payload(draft=_draft(5.0))
        assert offer["storeCategoryNames"] == ["Base Cards"]


class TestVariationStoreCategory:
    def test_you_pick_listings_take_the_you_pick_branch(self):
        from shoebox.transforms.variation_listing_builder import (
            ChecklistVariationRow,
            variation_store_category,
        )

        for subset_name, subset_type in (("Base", "Base"), ("Rookie Autographs", "Insert")):
            row = ChecklistVariationRow(
                record_id="r",
                set_year="2025",
                subset_id="s",
                set_name="2025 Topps",
                subset_name=subset_name,
                subset_type=subset_type,
                derived_card_number=None,
                card_number="1",
                qty=1,
                price=0.99,
                player="p",
                team=None,
                display_name="d",
                note=None,
            )
            assert variation_store_category(row) == "/Complete Your Set - You Pick"


class TestLiveStoreCategoryNames:
    def test_round_trip_restores_the_slash_and_drops_empty_slots(self):
        # getOffers reports "Baseball Singles/Chicago Cubs | None"; sending that
        # back verbatim would file the relisted offer under Other.
        assert live_store_category_names(["Baseball Singles/Chicago Cubs", "None"]) == [
            "/Baseball Singles/Chicago Cubs"
        ]
        assert live_store_category_names(["/Hits/Numbered", "", None]) == ["/Hits/Numbered"]
        assert live_store_category_names([]) == []
