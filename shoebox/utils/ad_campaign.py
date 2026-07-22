"""Promoted-listings campaign routing.

Campaign IDs and routing rules live in `settings.store.ad_campaigns`
(see `configs/app.yaml.example`). This keeps eBay-account-specific IDs out
of the source tree.
"""

from __future__ import annotations

from shoebox.settings import StoreAdCampaignSettings, get_settings


def get_ad_campaign(
    title: str,
    set_name: str,
    sport: str,
    campaigns: StoreAdCampaignSettings | None = None,
) -> str:
    """Resolve the promoted-listings campaign ID for a listing.

    Resolution order: by_sport → by_set → current-year default → default.
    `title` is accepted for signature compatibility with callers.
    """
    if campaigns is None:
        campaigns = get_settings().store.ad_campaigns

    if sport in campaigns.by_sport:
        return campaigns.by_sport[sport]
    if set_name in campaigns.by_set:
        return campaigns.by_set[set_name]
    if (
        campaigns.current_year
        and campaigns.current_year_default
        and set_name[: len(campaigns.current_year)] == campaigns.current_year
    ):
        return campaigns.current_year_default
    return campaigns.default
