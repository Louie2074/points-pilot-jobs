"""Match Google Flights cash fares to award flights → CashFareRecords (the CPP cash anchor).

Pure function: no DB, no I/O. Each Google fare is matched to the same-carrier award flight whose
local departure time is NEAREST within a tolerance window (award times can differ slightly from
Google's — Delta is ~10-30 min off — while Alaska/JetBlue match to the minute). Ties (two award
flights equally close) are skipped rather than guessed. Cheapest price per matched flight wins.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

from scrapers.base import CashFareRecord
from scrapers.google_flights import GoogleFare

logger = logging.getLogger(__name__)

# Only carriers we have award data for (others have no CPP value). Keys are Google Flights'
# display names (from the row aria-label "...flight with <carrier>."), values the award program
# IATA code stamped on flights.airline. Turkish/Etihad strings verified live 2026-06-16 — Google
# renders Etihad as "Etihad", not "Etihad Airways".
CARRIER_TO_IATA: dict[str, str] = {
    "Alaska": "AS",
    "Delta": "DL",
    "JetBlue": "B6",
    "Turkish Airlines": "TK",
    "Etihad": "EY",
    "Southwest": "WN",  # Southwest joined Google Flights' display in 2026; award is economy-only
}

_DEFAULT_TOLERANCE_MIN = 10


def _to_minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def match_cash_fares(
    google_fares: list[GoogleFare],
    award_flights: list[tuple[str, str, str]],  # (airline, flight_number, dep_hhmm)
    *,
    origin: str,
    destination: str,
    travel_date: date,
    now: datetime,
    ttl_hours: int,
    tolerance_min: int = _DEFAULT_TOLERANCE_MIN,
    cabin: str = "economy",
) -> list[CashFareRecord]:
    # Award flights grouped by carrier → [(flight_number, minutes_since_midnight)].
    by_airline: dict[str, list[tuple[str, int]]] = {}
    for airline, flight_number, hhmm in award_flights:
        by_airline.setdefault(airline, []).append((flight_number, _to_minutes(hhmm)))

    # flight_number -> (matched award airline, cheapest price). The airline is the award
    # program carrier we matched on (CARRIER_TO_IATA value), NOT the flight-number prefix —
    # codeshares (e.g. airline 'AS' with raw_flight_number 'AA 2957') must keep airline='AS'
    # so the CPP join (cash.airline = flights.airline) holds.
    cheapest: dict[str, tuple[str, float]] = {}
    for fare in google_fares:
        if not fare.nonstop:
            continue
        iata = CARRIER_TO_IATA.get(fare.carrier)
        if iata is None:
            continue
        fare_min = _to_minutes(fare.dep_hhmm)
        # Same-carrier award flights within the tolerance window, nearest first.
        near = sorted(
            (abs(m - fare_min), fn)
            for fn, m in by_airline.get(iata, [])
            if abs(m - fare_min) <= tolerance_min
        )
        if not near:
            continue
        # Nearest wins. On an exact distance tie `near` is already sorted by
        # (distance, flight_number), so near[0] is the lexicographically first flight number — a
        # deterministic, stable pick. Better than the old skip-both (which dropped CPP for both);
        # measured tie volume is tiny and widening the tolerance only makes the stable pick safer.
        flight_number = near[0][1]
        prev = cheapest.get(flight_number)
        if prev is None or fare.price < prev[1]:
            cheapest[flight_number] = (iata, fare.price)

    expires = now + timedelta(hours=ttl_hours)
    records: list[CashFareRecord] = []
    for flight_number, (airline, price) in cheapest.items():
        records.append(
            CashFareRecord(
                origin=origin.upper(),
                destination=destination.upper(),
                date=travel_date,
                airline=airline,
                cabin_class=cabin,
                flight_number=flight_number,
                cash_price=round(price, 2),
                scraped_at_utc=now,
                expires_at_utc=expires,
                source="google_flights",
            )
        )

    # O&D anchor: the cheapest cash for this market across ALL routings + carriers — the real
    # cash alternative for a CONNECTING award (which has no single nonstop flight to match).
    # Airline-agnostic sentinel ('__OD__'/'__OD__'); get_flights joins connecting (stops>0) rows
    # to it without an airline match. One row per (origin,dest,date,cabin); +0 nonstop change.
    if google_fares:
        od_price = round(min(f.price for f in google_fares), 2)
        records.append(
            CashFareRecord(
                origin=origin.upper(),
                destination=destination.upper(),
                date=travel_date,
                airline="__OD__",
                cabin_class=cabin,
                flight_number="__OD__",
                cash_price=od_price,
                scraped_at_utc=now,
                expires_at_utc=expires,
                source="google_flights",
            )
        )
    return records
