from shoebox.settings import StoreAdCampaignSettings
from shoebox.utils.ad_campaign import get_ad_campaign

CAMPAIGNS = StoreAdCampaignSettings(
    default="DEFAULT",
    current_year="2025",
    current_year_default="CURRENT_YEAR",
    by_sport={"Basketball": "BASKETBALL", "Football": "FOOTBALL"},
    by_set={"2025 Topps Chrome": "CHROME"},
)


def test_sport_takes_priority():
    assert get_ad_campaign("t", "2025 Topps Chrome", "Basketball", CAMPAIGNS) == "BASKETBALL"


def test_set_match_when_no_sport_rule():
    assert get_ad_campaign("t", "2025 Topps Chrome", "Baseball", CAMPAIGNS) == "CHROME"


def test_current_year_default():
    assert get_ad_campaign("t", "2025 Topps Series 1", "Baseball", CAMPAIGNS) == "CURRENT_YEAR"


def test_falls_back_to_default():
    assert get_ad_campaign("t", "2019 Old Set", "Baseball", CAMPAIGNS) == "DEFAULT"


def test_no_current_year_config_uses_default():
    campaigns = StoreAdCampaignSettings(default="D")
    assert get_ad_campaign("t", "2025 Anything", "Baseball", campaigns) == "D"
