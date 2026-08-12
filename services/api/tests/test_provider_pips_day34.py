from decimal import Decimal

import pytest

from app.provider_pips_day34 import normalize_provider_pips


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("+35 pips", Decimal("35")),
        ("-12.5 pips", Decimal("-12.5")),
        ("+35", Decimal("35")),
        ("0", Decimal("0")),
        (35, Decimal("35")),
        (-12.5, Decimal("-12.5")),
        (Decimal("8.25"), Decimal("8.25")),
    ],
)
def test_normalizes_unambiguous_provider_pips(raw, expected) -> None:
    assert normalize_provider_pips(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        None,
        True,
        "we made +35 pips today",
        "+35 pips secured at TP1",
        "around 35",
        "35-40 pips",
        "pips",
        "",
        "nan",
        "inf",
    ],
)
def test_ambiguous_or_non_numeric_provider_pips_stays_null(raw) -> None:
    assert normalize_provider_pips(raw) is None
