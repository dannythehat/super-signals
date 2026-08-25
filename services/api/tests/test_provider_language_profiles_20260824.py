from app.provider_language_profiles import PROFILE_VERSION, execution_profile_id, provider_profile


def test_every_active_provider_has_a_versioned_language_profile() -> None:
    names = [
        "The Gold Club - TGC",
        "TIG’s Asia Trades",
        "FXTradingVision l Forex & Crypto Signals 🚀",
        "GTMO VIP 🤴🏽",
        "United Kings™ Signals! 👑",
        "SureShot GOLD",
        "PipXpert - Forex Signals",
        "AJD TRADES",
    ]
    for name in names:
        profile = provider_profile(name)
        assert profile is not None
        assert profile["version"] == PROFILE_VERSION
        assert profile["entry_style"]
        assert profile["management_style"]


def test_ajd_is_paper_enabled_and_high_risk_never_changes_risk() -> None:
    profile = provider_profile("AJD TRADES")
    assert profile is not None
    assert profile["id"] == "ajd_xauusd"
    assert "paper-enabled only" in profile["safety"]
    assert "never changes configured risk" in profile["safety"]


def test_profile_lookup_is_case_and_whitespace_insensitive() -> None:
    assert execution_profile_id("  SURESHOT GOLD  ") == "sureshot_xauusd"



def test_ajd_public_results_and_promotions_are_explicitly_inert() -> None:
    profile = provider_profile("AJD TRADES")
    assert profile is not None
    assert "promotions" in profile["non_trade_style"]
    assert "literal broker instruction" in profile["non_trade_style"]
