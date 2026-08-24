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
        "Matthew trades",
    ]
    for name in names:
        profile = provider_profile(name)
        assert profile is not None
        assert profile["version"] == PROFILE_VERSION
        assert profile["entry_style"]
        assert profile["management_style"]


def test_matthew_is_paper_enabled_and_high_risk_never_changes_risk() -> None:
    profile = provider_profile("Matthew trades")
    assert profile is not None
    assert profile["id"] == "matthew_xauusd"
    assert "paper-enabled only" in profile["safety"]
    assert "never changes configured risk" in profile["safety"]


def test_profile_lookup_is_case_and_whitespace_insensitive() -> None:
    assert execution_profile_id("  SURESHOT GOLD  ") == "sureshot_xauusd"
