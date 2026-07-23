"""Application settings.

Key goals:
- No import-time side effects (no filesystem reads/dir creation at import).
- Allow config path override via env var.
- Provide a cached settings accessor for the rest of the codebase.

Environment variables:
- SHOEBOX_CONFIG_PATH: path to app.yaml (defaults to configs/app.yaml)
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field


class GCPSettings(BaseModel):
    project_id: str
    # Path to a service-account json file. Prefer ADC/Secret Manager in production.
    service_account_json: str


class BigQuerySettings(BaseModel):
    checklist_dataset: str
    ebay_dataset: str
    images_dataset: str


class eBaySettings(BaseModel):
    application: str
    user: str
    header: str
    path: str
    campaign_id: str


class GCSSettings(BaseModel):
    image_bucket: str
    metadata_bucket: str
    ebay_bucket: str
    image_log_bucket: str


class PathSettings(BaseModel):
    data_dir: str
    query_dir: str
    exports_dir: str
    scans_dir: str
    set_images_dir: str
    tools_dir: str

    # Scan filename convention used to resolve a bare image number/id to a file
    # under scans_dir: f"{scan_prefix}{number.zfill(scan_number_padding)}{scan_extension}".
    # Defaults reproduce the historical "SCAN_0123.jpg" layout. Point these at
    # whatever your scanner/camera emits (e.g. prefix "IMG", padding 5,
    # extension ".png" -> "IMG00123.png").
    scan_prefix: str = "SCAN_"
    scan_number_padding: int = 4
    scan_extension: str = ".jpg"

    def ensure_dirs(self) -> None:
        Path(self.data_dir).mkdir(parents=True, exist_ok=True)
        Path(self.exports_dir).mkdir(parents=True, exist_ok=True)
        Path(self.scans_dir).mkdir(parents=True, exist_ok=True)
        Path(self.query_dir).mkdir(parents=True, exist_ok=True)
        Path(self.set_images_dir).mkdir(parents=True, exist_ok=True)
        Path(self.tools_dir).mkdir(parents=True, exist_ok=True)


class SlackSettings(BaseModel):
    bot_token: str
    app_token: str
    notify_channel: str
    pricing_channel: str
    command_channel: str
    # Slack user IDs allowed to run commands in command_channel.
    # Empty list = anyone in the channel may run commands.
    allowed_user_ids: list[str] = []


class GoogleCalendarSettings(BaseModel):
    calendar_id: str


class StorePolicySettings(BaseModel):
    """eBay business policy IDs (Account → Business policies)."""

    payment_policy_id: str = "REPLACE_PAYMENT_POLICY_ID"
    return_policy_id: str = "REPLACE_RETURN_POLICY_ID"
    # Single-card listings pick a fulfillment policy by price:
    fulfillment_policy_id_low: str = (
        "REPLACE_FULFILLMENT_POLICY_ID_LOW"  # <= low_max_price
    )
    fulfillment_policy_id_high: str = "REPLACE_FULFILLMENT_POLICY_ID_HIGH"  # above it
    # "You Pick" / multi-variation listings use their own (e.g. free-shipping) policy:
    fulfillment_policy_id_variation: str = "REPLACE_FULFILLMENT_POLICY_ID_VARIATION"


class StoreAdCampaignSettings(BaseModel):
    """Promoted-listings campaign IDs and simple routing rules.

    Resolution order in ``utils.ad_campaign.get_ad_campaign``:
    by_sport → by_set → current-year default → default.
    """

    default: str = "REPLACE_CAMPAIGN_ID"
    # If a set name starts with this year string, use ``current_year_default``.
    current_year: str | None = None
    current_year_default: str | None = None
    # Route by sport, e.g. {"Basketball": "1587...", "Football": "1587..."}.
    by_sport: dict[str, str] = Field(default_factory=dict)
    # Exact-match overrides keyed by set name, e.g. {"2025 Topps Chrome": "1587..."}.
    by_set: dict[str, str] = Field(default_factory=dict)


class StoreSettings(BaseModel):
    """Store-specific identity and eBay account values used when building
    listings. All fields have placeholder defaults so an existing config
    without a ``store`` section still validates; set real values before
    creating live listings."""

    # Public store name shown in the listing description footer.
    name: str = "Your Store"
    # eBay inventory location key (Account → Business info → Locations).
    merchant_location_key: str = "REPLACE_MERCHANT_LOCATION_KEY"
    # eBay leaf category ID (261328 = Sports Trading Cards → Singles).
    category_id: str = "261328"
    # Item condition enum for single-card listings.
    condition: str = "USED_VERY_GOOD"
    # Item condition descriptor
    condition_descriptor: str = "NEAR_MINT_OR_BETTER"
    # Message attached to seller-initiated best offers (negotiation API).
    offer_message: str = (
        "Enjoy the discount on this card! Valid for the next 24 hours. Thank you!"
    )
    # Price threshold separating the low/high fulfillment policies.
    fulfillment_low_max_price: float = 19.99
    policies: StorePolicySettings = Field(default_factory=StorePolicySettings)
    ad_campaigns: StoreAdCampaignSettings = Field(
        default_factory=StoreAdCampaignSettings
    )


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    gcp: GCPSettings
    bigquery: BigQuerySettings
    gcs: GCSSettings
    paths: PathSettings
    ebay: eBaySettings
    slack: SlackSettings
    google_calendar: GoogleCalendarSettings
    store: StoreSettings = Field(default_factory=StoreSettings)


def _resolve_config_path(config_path: str | os.PathLike[str] | None = None) -> Path:
    if config_path is None:
        config_path = os.getenv("SHOEBOX_CONFIG_PATH", "configs/app.yaml")
    return Path(config_path)


@lru_cache(maxsize=1)
def get_settings(config_path: str | os.PathLike[str] | None = None) -> Settings:
    """Load and validate settings from YAML.

    This function is cached (singleton-like) but *does not* create directories.
    Call `ensure_runtime_dirs()` explicitly from entrypoints.
    """

    path = _resolve_config_path(config_path)
    if not path.exists():
        raise FileNotFoundError(
            f"Missing config file: {path}. "
            "Create it from configs/app.yaml.example (or set SHOEBOX_CONFIG_PATH)."
        )

    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    return Settings(**raw)


def ensure_runtime_dirs(config_path: str | os.PathLike[str] | None = None) -> None:
    """Create local directories referenced by settings."""

    settings = get_settings(config_path)
    settings.paths.ensure_dirs()
