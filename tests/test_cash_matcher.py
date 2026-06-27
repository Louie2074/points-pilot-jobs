from datetime import date, datetime, timezone

from pipeline.cash_matcher import CARRIER_TO_IATA, match_cash_fares
from scrapers.google_flights import GoogleFare

NOW = datetime(2026, 6, 7, tzinfo=timezone.utc)
D = date(2026, 6, 15)


def _award(rows):  # rows of (airline, flight_number, hhmm)
    return rows


def _per_flight(recs):
    """The per-flight matched records, excluding the additive airline-agnostic '__OD__' anchor
    (Task 3) — so the per-flight matching assertions below stay independent of the anchor."""
    return [r for r in recs if r.flight_number != "__OD__"]


def test_emits_od_anchor_cheapest_any_routing():
    # Cheapest cash across ALL routings (nonstop + connecting, any carrier) → one '__OD__' record.
    fares = [
        GoogleFare("Alaska", "08:00", 380.0, True),  # nonstop
        GoogleFare("Delta", "09:30", 250.0, False),  # connecting, cheaper, other carrier
        GoogleFare("Alaska", "14:00", 410.0, False),
    ]
    award = _award([("AS", "AS 100", "08:00")])
    recs = match_cash_fares(
        fares,
        award,
        origin="LAX",
        destination="SEA",
        travel_date=D,
        now=NOW,
        ttl_hours=48,
        cabin="economy",
    )
    od = [r for r in recs if r.flight_number == "__OD__"]
    assert len(od) == 1
    assert (
        od[0].airline == "__OD__" and od[0].cash_price == 250.0 and od[0].cabin_class == "economy"
    )
    # the nonstop per-flight match is unchanged (still emitted for AS 100):
    assert any(r.airline == "AS" and r.flight_number == "AS 100" for r in recs)


def test_od_anchor_absent_when_no_fares():
    assert (
        match_cash_fares(
            [],
            [],
            origin="LAX",
            destination="SEA",
            travel_date=D,
            now=NOW,
            ttl_hours=48,
            cabin="economy",
        )
        == []
    )


def test_exact_match_borrows_flight_number():
    fares = [GoogleFare("JetBlue", "11:45", 269.0, True)]
    award = _award([("B6", "B6 1234", "11:45")])
    recs = _per_flight(
        match_cash_fares(
            fares, award, origin="SEA", destination="BOS", travel_date=D, now=NOW, ttl_hours=48
        )
    )
    assert len(recs) == 1
    r = recs[0]
    assert (r.airline, r.flight_number, r.cash_price, r.cabin_class) == (
        "B6",
        "B6 1234",
        269.0,
        "economy",
    )
    assert r.source == "google_flights"


def test_no_match_dropped():
    fares = [GoogleFare("JetBlue", "09:00", 200.0, True)]
    award = _award([("B6", "B6 1234", "11:45")])
    assert (
        _per_flight(
            match_cash_fares(
                fares, award, origin="SEA", destination="BOS", travel_date=D, now=NOW, ttl_hours=48
            )
        )
        == []
    )


def test_unknown_carrier_dropped():
    fares = [GoogleFare("Spirit", "11:45", 99.0, True)]
    award = _award([("NK", "NK 1", "11:45")])
    assert (
        _per_flight(
            match_cash_fares(
                fares, award, origin="SEA", destination="BOS", travel_date=D, now=NOW, ttl_hours=48
            )
        )
        == []
    )


def test_equidistant_same_time_breaks_to_lowest_flight_number():
    # Two award flights at the same carrier+time → pick the lowest flight_number
    # deterministically (was: skip both). "DL 1" < "DL 2" lexicographically.
    fares = [GoogleFare("Delta", "16:00", 289.0, True)]
    award = _award([("DL", "DL 1", "16:00"), ("DL", "DL 2", "16:00")])
    recs = _per_flight(
        match_cash_fares(
            fares, award, origin="SEA", destination="BOS", travel_date=D, now=NOW, ttl_hours=48
        )
    )
    assert len(recs) == 1
    assert (recs[0].flight_number, recs[0].cash_price) == ("DL 1", 289.0)


def test_cheapest_wins_per_flight():
    fares = [GoogleFare("Alaska", "23:18", 401.0, True), GoogleFare("Alaska", "23:18", 289.0, True)]
    award = _award([("AS", "AS 12", "23:18")])
    recs = _per_flight(
        match_cash_fares(
            fares, award, origin="SEA", destination="BOS", travel_date=D, now=NOW, ttl_hours=48
        )
    )
    assert [r.cash_price for r in recs] == [289.0]


def test_carrier_map_covers_live_carriers():
    assert CARRIER_TO_IATA == {
        "Alaska": "AS",
        "Delta": "DL",
        "JetBlue": "B6",
        "Turkish Airlines": "TK",
        "Etihad": "EY",
        "Southwest": "WN",
    }


def test_turkish_fare_matches_award():
    # Google labels the nonstop Turkish flight "flight with Turkish Airlines." → carrier "Turkish
    # Airlines"; award rows carry airline "TK". Verified live on IST→JFK 2026-06-16.
    fares = [GoogleFare("Turkish Airlines", "01:05", 620.0, True)]
    award = _award([("TK", "TK 3", "01:05")])
    recs = _per_flight(
        match_cash_fares(
            fares, award, origin="IST", destination="JFK", travel_date=D, now=NOW, ttl_hours=48
        )
    )
    assert len(recs) == 1
    assert (recs[0].airline, recs[0].flight_number, recs[0].cash_price) == ("TK", "TK 3", 620.0)


def test_etihad_fare_matches_award():
    # Google labels the nonstop Etihad flight "flight with Etihad." (NOT "Etihad Airways") →
    # carrier "Etihad"; award rows carry airline "EY". Verified live on JFK→AUH 2026-06-16.
    fares = [GoogleFare("Etihad", "15:45", 1922.0, True)]
    award = _award([("EY", "EY 100", "15:45")])
    recs = _per_flight(
        match_cash_fares(
            fares, award, origin="JFK", destination="AUH", travel_date=D, now=NOW, ttl_hours=48
        )
    )
    assert len(recs) == 1
    assert (recs[0].airline, recs[0].flight_number, recs[0].cash_price) == ("EY", "EY 100", 1922.0)


def test_codeshare_uses_award_airline_not_flight_prefix():
    # Award flight: program carrier AS but codeshare flight number "AA 2957".
    # The cash record must carry airline="AS" (for the CPP join), flight_number="AA 2957".
    fares = [GoogleFare("Alaska", "07:00", 150.0, True)]
    award = _award([("AS", "AA 2957", "07:00")])
    recs = _per_flight(
        match_cash_fares(
            fares, award, origin="LAS", destination="ORD", travel_date=D, now=NOW, ttl_hours=48
        )
    )
    assert len(recs) == 1
    assert recs[0].airline == "AS"
    assert recs[0].flight_number == "AA 2957"


def test_within_tolerance_nearest_matches():
    # Delta-style: Google time is 18 min off the award flight → still matches (within 30).
    fares = [GoogleFare("Delta", "07:15", 289.0, True)]
    award = _award([("DL", "DL 326", "07:33")])
    recs = _per_flight(
        match_cash_fares(
            fares,
            award,
            origin="ATL",
            destination="LAX",
            travel_date=D,
            now=NOW,
            ttl_hours=48,
            tolerance_min=30,
        )
    )
    assert len(recs) == 1 and recs[0].flight_number == "DL 326"


def test_outside_tolerance_dropped():
    fares = [GoogleFare("Delta", "07:15", 289.0, True)]
    award = _award([("DL", "DL 326", "08:00")])  # 45 min off
    assert (
        _per_flight(
            match_cash_fares(
                fares,
                award,
                origin="ATL",
                destination="LAX",
                travel_date=D,
                now=NOW,
                ttl_hours=48,
                tolerance_min=30,
            )
        )
        == []
    )


def test_nearest_within_window_wins():
    fares = [GoogleFare("Delta", "16:00", 289.0, True)]
    award = _award([("DL", "DL 1", "16:10"), ("DL", "DL 2", "16:25")])  # nearest = DL 1 (10 min)
    recs = _per_flight(
        match_cash_fares(
            fares,
            award,
            origin="ATL",
            destination="LAX",
            travel_date=D,
            now=NOW,
            ttl_hours=48,
            tolerance_min=30,
        )
    )
    assert [r.flight_number for r in recs] == ["DL 1"]


def test_equidistant_window_breaks_to_lowest_flight_number():
    # DL 1 (15 min before) and DL 2 (15 min after) are equidistant → pick the lowest
    # flight_number deterministically rather than dropping CPP for both.
    fares = [GoogleFare("Delta", "16:00", 289.0, True)]
    award = _award([("DL", "DL 1", "15:45"), ("DL", "DL 2", "16:15")])
    recs = _per_flight(
        match_cash_fares(
            fares,
            award,
            origin="ATL",
            destination="LAX",
            travel_date=D,
            now=NOW,
            ttl_hours=48,
            tolerance_min=30,
        )
    )
    assert len(recs) == 1
    assert recs[0].flight_number == "DL 1"


def test_match_cash_fares_stamps_cabin():
    from datetime import date as _date
    from datetime import datetime as _dt
    from datetime import timezone

    from pipeline.cash_matcher import match_cash_fares
    from scrapers.google_flights import GoogleFare

    fares = [GoogleFare(carrier="JetBlue", dep_hhmm="09:00", price=850.0, nonstop=True)]
    award = [("B6", "B6 123", "09:00")]
    recs = _per_flight(
        match_cash_fares(
            fares,
            award,
            origin="JFK",
            destination="LAX",
            travel_date=_date(2026, 7, 12),
            now=_dt(2026, 6, 20, tzinfo=timezone.utc),
            ttl_hours=48,
            cabin="business",
        )
    )
    assert len(recs) == 1
    assert recs[0].cabin_class == "business"


def test_delta_skew_matches_at_tolerance_20():
    # Delta's Google time runs ~10-30 min off the award time; a 15-min skew must match
    # at the new prod tolerance of 20 (it was dropped at the old default of 10).
    fares = [GoogleFare("Delta", "07:15", 289.0, True)]
    award = _award([("DL", "DL 326", "07:30")])  # 15 min off
    recs = _per_flight(
        match_cash_fares(
            fares,
            award,
            origin="ATL",
            destination="LAX",
            travel_date=D,
            now=NOW,
            ttl_hours=48,
            tolerance_min=20,
        )
    )
    assert len(recs) == 1 and recs[0].flight_number == "DL 326"


def test_skew_beyond_tolerance_20_still_dropped():
    fares = [GoogleFare("Delta", "07:15", 289.0, True)]
    award = _award([("DL", "DL 326", "07:40")])  # 25 min off
    assert (
        _per_flight(
            match_cash_fares(
                fares,
                award,
                origin="ATL",
                destination="LAX",
                travel_date=D,
                now=NOW,
                ttl_hours=48,
                tolerance_min=20,
            )
        )
        == []
    )


def test_southwest_fare_matches_wn_award():
    # Southwest joined Google Flights' display in 2026; its fares must map to WN award (economy).
    assert CARRIER_TO_IATA.get("Southwest") == "WN"
    fares = [GoogleFare("Southwest", "08:05", 189.0, True)]
    award = _award([("WN", "WN 1234", "08:05")])
    recs = _per_flight(
        match_cash_fares(
            fares,
            award,
            origin="MDW",
            destination="MCO",
            travel_date=D,
            now=NOW,
            ttl_hours=72,
            cabin="economy",
        )
    )
    assert len(recs) == 1
    assert recs[0].airline == "WN"
    assert recs[0].flight_number == "WN 1234"
    assert recs[0].cash_price == 189.0
