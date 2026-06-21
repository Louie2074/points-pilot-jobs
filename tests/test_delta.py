import os

os.environ.setdefault("MOTHERDUCK_TOKEN", "test-dummy-token")

import pytest  # noqa: E402

from scrapers.delta import _brand_to_cabin  # noqa: E402 — env var must be set before import


@pytest.mark.parametrize(
    "brand_id",
    [
        "COMFORT",
        "COMFORT_PLUS",
        "DELTA_COMFORT_PLUS",
        "DCP",
        "CDCP",  # Comfort+ branded-fare code seen live
        "comfort",  # case-insensitive
    ],
)
def test_comfort_plus_is_economy(brand_id):
    # Delta Comfort+ is extra-legroom *economy* (an economy fare bucket sold on every
    # narrowbody), NOT a separate premium-economy cabin. It must classify as economy so it
    # doesn't flood the premium_economy award universe with phantom narrowbody units.
    assert _brand_to_cabin(brand_id) == "economy"


@pytest.mark.parametrize(
    "brand_id",
    [
        "PREMIUM_SELECT",
        "PREMIUM SELECT",
        "DELTA_PREMIUM_SELECT",
        "premium_select",  # case-insensitive
    ],
)
def test_premium_select_is_premium_economy(brand_id):
    # Delta Premium Select is the true long-haul widebody premium-economy cabin. It must stay
    # premium_economy — and must NOT be caught by the new COMFORT/DCP economy entries.
    assert _brand_to_cabin(brand_id) == "premium_economy"


def test_comfort_plus_and_premium_select_are_distinct():
    # Lock the distinction the fix establishes: the two are different cabins.
    assert _brand_to_cabin("COMFORT") != _brand_to_cabin("PREMIUM_SELECT")


def test_other_cabins_unchanged():
    assert _brand_to_cabin("DELTA_ONE") == "business"
    assert _brand_to_cabin("FIRST") == "first"
    assert _brand_to_cabin("MAIN") == "economy"
    assert _brand_to_cabin("BASIC") == "economy"
    assert _brand_to_cabin("ECONOMY") == "economy"
    assert _brand_to_cabin(None) is None
    assert _brand_to_cabin("") is None
    assert _brand_to_cabin("TOTALLY_UNKNOWN_BRAND") is None
