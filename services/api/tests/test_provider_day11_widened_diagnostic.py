from app.provider_day11_widened_diagnostic import (
    DAY11_BASELINE_RUN_ID,
    DAY11_PROVIDER_COUNT,
    DAY11_WIDENED_SIGNAL_COUNT,
)


def test_day11_widened_diagnostic_constants_are_frozen():
    assert str(DAY11_BASELINE_RUN_ID) == "c5fae236-a63f-4340-99e7-7890e08f782a"
    assert DAY11_PROVIDER_COUNT == 5
    assert DAY11_WIDENED_SIGNAL_COUNT == 82
