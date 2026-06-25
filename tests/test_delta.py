from datetime import date

import pytest

from scrapers.delta import DeltaScraper, _brand_to_cabin, _most_premium_cabin


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


# --- Live Delta brand CODES (not the verbose names) ---------------------------------------------
# Delta's offer API returns short prefixed brand codes, captured live on JFK-CDG 2026-06-25.
# Before the fix, CD1 (Delta One) and CDPS (Premium Select) matched NO rule and resolved to None,
# so Delta One was silently dropped (0 business rows). See POI-20.
@pytest.mark.parametrize(
    "brand_id,expected",
    [
        ("CD1", "business"),  # Delta One (transatlantic/-pacific business) — live code
        ("CDPS", "premium_economy"),  # Delta Premium Select — live code
        ("CDCP", "economy"),  # Comfort+ — live code (extra-legroom economy)
        ("CMAIN", "economy"),  # Main Cabin — live code
        ("BMAIN", "economy"),  # Basic Economy — live code
        ("CFIRST", "first"),  # domestic First — live code
        ("AFST", "business"),  # Air France SkyTeam-partner business leg
        ("AFPE", "premium_economy"),  # Air France partner premium-economy leg
    ],
)
def test_live_delta_brand_codes(brand_id, expected):
    assert _brand_to_cabin(brand_id) == expected


def test_cd1_comfort_codes_do_not_collide():
    # CDCP (Comfort+) must NOT be mistaken for CD1 (Delta One); the economy "DCP" token catches it.
    assert _brand_to_cabin("CDCP") == "economy"
    # CDPS (Premium Select) must NOT be mistaken for CD1 either.
    assert _brand_to_cabin("CDPS") == "premium_economy"
    assert _brand_to_cabin("CD1") == "business"


def test_most_premium_cabin_fallback():
    # Empty / no-known cabins -> None.
    assert _most_premium_cabin([]) is None
    assert _most_premium_cabin(["??"]) is None
    # A Delta One itinerary that connects through a domestic-First hub lists two leg cabins; the
    # offer is the Delta One (business) product, so business must win over the incidental First.
    assert _most_premium_cabin(["first", "business"]) == "business"
    assert _most_premium_cabin(["business", "first"]) == "business"
    # A pure domestic-First itinerary has every leg "first" — stays first.
    assert _most_premium_cabin(["first", "first"]) == "first"
    # Mixed economy/premium picks the more premium.
    assert _most_premium_cabin(["economy", "premium_economy"]) == "premium_economy"


# --- End-to-end normalize() over a real-shaped offer set ----------------------------------------
def _fare_info(*, leg_brands: list[tuple[str, str]], miles: int, cash: float, seats: int) -> dict:
    """Build a fareInformation dict mirroring the live JFK-CDG shape.

    `leg_brands` is a list of (brandId, cosCode) per leg — e.g. [("CFIRST","OD"),("CD1","OD")]
    for a Delta One itinerary that connects through a domestic-First leg.
    """
    return {
        "brandByFlightLegs": [
            {"brandId": bid, "cosCode": cos} for bid, cos in leg_brands
        ],
        "availableSeatCnt": seats,
        "farePrice": [
            {
                "totalFarePrice": {
                    "milesEquivalentPrice": {"mileCnt": miles},
                    "currencyEquivalentPrice": {"roundedCurrencyAmt": cash},
                }
            }
        ],
    }


def _offer(*, dominant_brand: str, fare_info: dict, fare_type: str = "primary") -> dict:
    return {
        "offerId": "o1",
        "additionalOfferProperties": {
            "fareType": fare_type,
            "dominantSegmentBrandId": dominant_brand,
        },
        "offerItems": [
            {"retailItems": [{"retailItemMetaData": {"fareInformation": [fare_info]}}]}
        ],
    }


def _offer_set(*, origin: str, dest: str, aircraft: str, offers: list[dict]) -> dict:
    """A gqlOffersSets entry: a nonstop trip (one segment, one leg) paired with `offers`."""
    return {
        "trips": [
            {
                "tripId": "t1",
                "scheduledDepartureLocalTs": "2026-07-23T19:30:00",
                "scheduledArrivalLocalTs": "2026-07-24T08:55:00",
                "originAirportCode": origin,
                "destinationAirportCode": dest,
                "stopCnt": 0,
                "totalTripTime": {"dayCnt": 0, "hourCnt": 7, "minuteCnt": 25},
                "flightSegment": [
                    {
                        "marketingCarrier": {"carrierCode": "DL", "carrierNum": "404"},
                        "flightLeg": [
                            {
                                "legId": "l1",
                                "marketingCarrier": {"carrierCode": "DL", "carrierNum": "404"},
                                "aircraft": {"fleetTypeCode": aircraft},
                            }
                        ],
                    }
                ],
            }
        ],
        "offers": offers,
    }


def test_normalize_delta_one_is_business():
    # The exact live shape that mislabeled: dominant "CD1" with legs ["CFIRST","CD1"] on a 764
    # (A330) widebody. Must resolve to business, NOT first (the old bug), and not be dropped.
    fi = _fare_info(leg_brands=[("CFIRST", "OD"), ("CD1", "OD")], miles=230000, cash=22.4, seats=4)
    raw = {
        "data": {
            "gqlSearchOffers": {
                "gqlOffersSets": [
                    _offer_set(
                        origin="JFK",
                        dest="CDG",
                        aircraft="764",
                        offers=[_offer(dominant_brand="CD1", fare_info=fi)],
                    )
                ]
            }
        }
    }
    recs = DeltaScraper().normalize(raw, "JFK", "CDG", date(2026, 7, 23))
    assert len(recs) == 1
    assert recs[0].cabin_class == "business"
    assert recs[0].points_cost == 230000


def test_normalize_premium_select_is_premium_economy():
    fi = _fare_info(leg_brands=[("CDPS", "RA")], miles=120000, cash=22.4, seats=6)
    raw = {
        "data": {
            "gqlSearchOffers": {
                "gqlOffersSets": [
                    _offer_set(
                        origin="JFK",
                        dest="CDG",
                        aircraft="359",
                        offers=[_offer(dominant_brand="CDPS", fare_info=fi)],
                    )
                ]
            }
        }
    }
    recs = DeltaScraper().normalize(raw, "JFK", "CDG", date(2026, 7, 23))
    assert len(recs) == 1
    assert recs[0].cabin_class == "premium_economy"


def test_normalize_domestic_first_stays_first():
    # Genuine domestic First on a narrowbody (A321 "32S"): dominant "CFIRST", single first leg.
    # Must STAY first — the fix must not flip real domestic First to business.
    fi = _fare_info(leg_brands=[("CFIRST", "OD")], miles=45000, cash=11.2, seats=4)
    raw = {
        "data": {
            "gqlSearchOffers": {
                "gqlOffersSets": [
                    _offer_set(
                        origin="ATL",
                        dest="BOS",
                        aircraft="32S",
                        offers=[_offer(dominant_brand="CFIRST", fare_info=fi)],
                    )
                ]
            }
        }
    }
    recs = DeltaScraper().normalize(raw, "ATL", "BOS", date(2026, 7, 23))
    assert len(recs) == 1
    assert recs[0].cabin_class == "first"
