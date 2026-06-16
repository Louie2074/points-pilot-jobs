import os
from datetime import date

os.environ.setdefault("MOTHERDUCK_TOKEN", "test-dummy-token")

from scrapers.turkish import (  # noqa: E402 — env var must be set before this import
    TurkishScraper,
    _cabin_miles,
    _flight_number,
    _parse_tk_dt,
)

TRAVEL = date(2026, 7, 7)


def _fare(amount, currency="MILE", brand="Y"):
    """A fareCategory cabin entry priced at `amount` miles."""
    return {
        "bookingPriceInfoList": [
            {
                "referencePassengerFare": {
                    "totalFare": {"currencyCode": currency, "amount": amount}
                },
                "brandCode": brand,
            }
        ]
    }


def _seg(o="JFK", d="IST", dep="07-07-2026 00:25", arr="07-07-2026 17:15", num="0012"):
    return {
        "departureAirportCode": o,
        "arrivalAirportCode": d,
        "departureDateTime": dep,
        "arrivalDateTime": arr,
        "flightCode": {"airlineCode": "TK", "flightNumber": num},
        "carrierAirline": "TK",
        "equipmentCode": "77W",
    }


def _opt(segs=None, fares=None, seats=4):
    return {
        "segmentList": segs or [_seg()],
        "lastSeatCount": seats,
        "fareCategory": fares
        if fares is not None
        else {"ECONOMY": _fare(55000), "BUSINESS": _fare(135000, brand="C")},
    }


def _resp(options):
    return {
        "data": {"originDestinationInformationList": [{"originDestinationOptionList": options}]},
        "success": True,
    }


# ---------------------------------------------------------------- pure helpers
def test_flight_number_strips_leading_zeros():
    assert _flight_number({"flightCode": {"airlineCode": "TK", "flightNumber": "0012"}}) == "TK 12"


def test_flight_number_missing_returns_none():
    assert _flight_number({"flightCode": {}}) is None and _flight_number({}) is None


def test_parse_tk_dt_attaches_origin_timezone():
    dt = _parse_tk_dt("07-07-2026 19:15", "JFK")
    assert dt is not None and dt.tzinfo is not None
    assert (dt.year, dt.month, dt.day, dt.hour, dt.minute) == (2026, 7, 7, 19, 15)


def test_parse_tk_dt_unmapped_airport_is_none():
    assert _parse_tk_dt("07-07-2026 17:15", "IST") is None and _parse_tk_dt("", "JFK") is None


def test_cabin_miles_reads_mile_total_fare():
    assert _cabin_miles(_fare(135000)) == 135000
    assert _cabin_miles(_fare(500, currency="USD")) is None  # cash, not miles
    assert _cabin_miles(_fare(0)) is None
    assert _cabin_miles({}) is None


# ---------------------------------------------------------------- normalize
def test_normalize_reads_distinct_per_cabin_prices():
    # the bug this guards: economy 55k and business 135k must NOT both come out 55k
    recs = TurkishScraper().normalize(_resp([_opt()]), "JFK", "IST", TRAVEL)
    by_cabin = {r.cabin_class: r for r in recs}
    assert set(by_cabin) == {"economy", "business"}
    assert by_cabin["economy"].points_cost == 55000
    assert by_cabin["business"].points_cost == 135000
    eco = by_cabin["economy"]
    assert eco.origin == "JFK" and eco.destination == "IST" and eco.airline == "TK"
    assert eco.source == "turkish" and eco.program == "Miles&Smiles"
    assert eco.stops == 0 and eco.raw_flight_number == "TK 12" and eco.available_seats == 4
    assert eco.departure_time_local is not None  # JFK is mapped


def test_normalize_connection_sets_stops_and_layover():
    segs = [
        _seg("ORD", "IST", "07-07-2026 21:55", "08-07-2026 16:30", "0006"),
        _seg("IST", "BKK", "08-07-2026 20:10", "09-07-2026 09:25", "0068"),
    ]
    recs = TurkishScraper().normalize(_resp([_opt(segs=segs)]), "ORD", "BKK", TRAVEL)
    assert recs
    r = recs[0]
    assert r.stops == 1 and r.layover_airports == "IST" and r.raw_flight_number == "TK 6+TK 68"


def test_normalize_skips_cash_and_unmapped_cabins():
    fares = {"ECONOMY": _fare(500, currency="USD"), "WEIRDCABIN": _fare(99000)}
    assert TurkishScraper().normalize(_resp([_opt(fares=fares)]), "JFK", "IST", TRAVEL) == []


def test_normalize_handles_challenge_and_empty():
    sc = TurkishScraper()
    assert sc.normalize({"sec-cp-challenge": "true"}, "JFK", "IST", TRAVEL) == []
    assert sc.normalize({"data": None, "success": False}, "JFK", "IST", TRAVEL) == []
    assert sc.normalize({}, "JFK", "IST", TRAVEL) == []


# ---------------------------------------------------------------- request builder
def test_build_js_encodes_award_route_and_date():
    js = TurkishScraper()._build_js("JFK", "IST", TRAVEL)
    assert "moduleType" in js and "AWARD" in js
    assert '"JFK"' in js and '"IST"' in js and "07-07-2026" in js
    assert "/api/v1/availability" in js
