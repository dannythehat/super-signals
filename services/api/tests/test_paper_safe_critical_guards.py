from app.paper_safe_member_routing import _CRITICAL_TARGET, _TDC_LAYER_ZONE


def test_tdc_high_risk_layer_zone_is_paper_only_structure() -> None:
    raw = (
        "BUY GOLD @ 4398/4393\n\n"
        "TP 4400\nTP 4403\nTP 4407\nTP OPEN\nSL 4392\n\nHIGH RISK TRADE"
    )
    assert _TDC_LAYER_ZONE.search(raw) is not None


def test_tdc_pending_layer_zone_is_paper_only_structure() -> None:
    raw = (
        "BUY LIMITS GOLD @ 4386/4381 AREA\n\n"
        "TP 4389\nTP 4393\nTP 4398\nTP OPEN\nSL 4380\n\nHIGH RISK TRADE"
    )
    assert _TDC_LAYER_ZONE.search(raw) is not None


def test_new_layer_management_targets_are_blocked_from_live_member_engine() -> None:
    for target in (
        "best_entry",
        "all_but_best",
        "entry_price_4394",
        "pending_layers",
        "entry_2_partial_tp1",
        "worst_3_layers",
    ):
        assert _CRITICAL_TARGET.search(target) is not None, target
