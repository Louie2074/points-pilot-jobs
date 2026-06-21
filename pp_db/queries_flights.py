"""Ported flight-table query functions (Postgres / SQLAlchemy) — the pp_db counterpart of the
DuckDB ``db/queries.py`` "Flights" section. Covers ``upsert_flights``, ``get_flights`` and
``get_fresh_scrape_dates``.

Each function takes an explicit SQLAlchemy ``Connection`` as its first arg. Behaviour matches the
DuckDB original row-for-row (verified by ``tests/test_parity_flights.py``):

  * ``upsert_flights`` — batch INSERT … ON CONFLICT on the 6-col natural key
    (origin, destination, date, airline, cabin_class, raw_flight_number); the conflict UPDATE set
    is the exact column list the DuckDB ``_UPSERT_FLIGHT`` updates (route/date/cabin/flight-number
    are the key, so they are *not* in the SET — same as the original). The ``raw_flight_number or
    "UNKNOWN"`` sentinel is preserved.
  * ``get_flights`` — route+date-range read with a non-expired ``cash_fares`` LEFT JOIN and the
    derived ``cpp`` (``round(cash_price / points_cost * 100, 2)``). Reproduced via ``text()`` so the
    join/CASE/round and the ``ORDER BY date ASC, points_cost ASC`` + ``LIMIT`` are byte-faithful.
  * ``get_fresh_scrape_dates`` — DISTINCT dates with a still-fresh scrape, optionally scoped to one
    ``source``.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from sqlalchemy import Connection, func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from pp_db.models import Flight

# Scraper-side record type (only imported for typing parity with the original signature).
try:  # pragma: no cover - import shape only
    from scrapers.base import FlightRecord
except Exception:  # pragma: no cover
    FlightRecord = Any  # type: ignore[assignment,misc]


# Columns updated on conflict — the exact set the DuckDB _UPSERT_FLIGHT touches. The six natural-key
# columns (origin, destination, date, airline, cabin_class, raw_flight_number) plus ``program`` are
# intentionally NOT updated (program is omitted in the original too).
_UPDATE_COLS = (
    "source",
    "points_cost",
    "cash_cost",
    "stops",
    "available_seats",
    "partner_airline",
    "scraped_at_utc",
    "expires_at_utc",
    "departure_time_local",
    "arrival_time_local",
    "duration_minutes",
    "aircraft_type",
    "is_saver",
    "fare_class",
    "layover_airports",
    "layover_duration_minutes",
    "next_day_arrival",
    "mixed_cabin",
)


def upsert_flights(conn: Connection, records: list[FlightRecord]) -> int:
    """Insert or update a batch of FlightRecord objects. Returns the number processed.

    Mirrors the DuckDB ``executemany(_UPSERT_FLIGHT, …)``: ON CONFLICT on the 6-col UNIQUE
    natural key, updating the same non-key columns the original does. ``raw_flight_number or
    "UNKNOWN"`` keeps the non-NULL sentinel the UNIQUE constraint relies on.
    """
    if not records:
        return 0

    rows = [
        {
            "origin": r.origin,
            "destination": r.destination,
            "date": r.date,
            "airline": r.airline,
            "program": r.program,
            "source": r.source,
            "points_cost": r.points_cost,
            "cash_cost": r.cash_cost,
            "stops": r.stops,
            "cabin_class": r.cabin_class,
            "available_seats": r.available_seats,
            # sentinel — UNIQUE constraint needs non-NULL
            "raw_flight_number": r.raw_flight_number or "UNKNOWN",
            "partner_airline": r.partner_airline,
            "scraped_at_utc": r.scraped_at_utc,
            "expires_at_utc": r.expires_at_utc,
            "departure_time_local": r.departure_time_local,
            "arrival_time_local": r.arrival_time_local,
            "duration_minutes": r.duration_minutes,
            "aircraft_type": r.aircraft_type,
            "is_saver": r.is_saver,
            "fare_class": r.fare_class,
            "layover_airports": r.layover_airports,
            "layover_duration_minutes": r.layover_duration_minutes,
            "next_day_arrival": r.next_day_arrival,
            "mixed_cabin": r.mixed_cabin,
        }
        for r in records
    ]

    stmt = pg_insert(Flight)
    stmt = stmt.on_conflict_do_update(
        index_elements=[
            Flight.origin,
            Flight.destination,
            Flight.date,
            Flight.airline,
            Flight.cabin_class,
            Flight.raw_flight_number,
        ],
        set_={col: getattr(stmt.excluded, col) for col in _UPDATE_COLS},
    )
    conn.execute(stmt, rows)
    return len(rows)


# Column order returned by get_flights — identical to the DuckDB original.
_GET_FLIGHTS_COLUMNS = [
    "id",
    "origin",
    "destination",
    "date",
    "airline",
    "program",
    "source",
    "points_cost",
    "cash_cost",
    "stops",
    "cabin_class",
    "available_seats",
    "raw_flight_number",
    "partner_airline",
    "scraped_at",
    "expires_at",
    "departure_time",
    "arrival_time",
    "duration_minutes",
    "aircraft_type",
    "is_saver",
    "fare_class",
    "layover_airports",
    "layover_duration_minutes",
    "next_day_arrival",
    "mixed_cabin",
    "cash_price",
    "cpp",
]


def get_flights(
    conn: Connection,
    origin: str,
    destination: str,
    date_from: date,
    date_to: date,
    cabin_class: str | None = None,
    max_points: int | None = None,
    airline: str | None = None,
    fresh_only: bool = True,
    limit: int = 1000,
    passengers: int = 1,
) -> list[dict[str, Any]]:
    """Query available flights for a route + date range.

    Faithful port of the DuckDB ``get_flights``: a non-expired ``cash_fares`` LEFT JOIN on the
    flight's natural key, the derived ``cpp`` (``round(cash_price / points_cost * 100, 2)`` when
    cash exists and points_cost > 0), the same optional filters, ``ORDER BY date ASC, points_cost
    ASC`` and ``LIMIT``. ``fresh_only`` filters on ``date >= current_date`` only (expires_at is NOT
    a row filter here — it just dates the cached scrape).

    ``passengers`` is the party size (API consumer): the budget compares the TOTAL party cost
    (``points_cost * passengers <= max_points``) and, when > 1, keeps only flights that can seat
    the whole party (``available_seats >= passengers``, or the -1 "unknown count" sentinel). At the
    default of 1 this is byte-identical to the scraper's signature, which omits the param.
    """
    filters = [
        "f.origin = :origin",
        "f.destination = :destination",
        "f.date BETWEEN :date_from AND :date_to",
    ]
    params: dict[str, Any] = {
        "origin": origin,
        "destination": destination,
        "date_from": date_from,
        "date_to": date_to,
    }

    if cabin_class:
        filters.append("f.cabin_class = :cabin_class")
        params["cabin_class"] = cabin_class
    if max_points:
        # Budget is the TOTAL for the whole party, so compare points_cost * passengers
        # (at passengers=1 this is identical to the old points_cost <= max_points).
        filters.append("f.points_cost * :passengers <= :max_points")
        params["passengers"] = passengers
        params["max_points"] = max_points
    if airline:
        # `airline` is the program carrier each scraper stamps (AS/DL/AA/B6).
        filters.append("f.airline = :airline")
        params["airline"] = airline
    if passengers > 1:
        # Only keep flights that can seat the whole party. available_seats = -1 means the
        # scraper didn't stamp a count — keep those rather than hide valid options.
        filters.append("(f.available_seats >= :passengers OR f.available_seats < 0)")
        params["passengers"] = passengers
    if fresh_only:
        filters.append("f.date >= current_date")

    where = " AND ".join(filters)
    params["limit"] = limit

    sql = text(
        f"""
        SELECT
            f.id, f.origin, f.destination, f.date, f.airline, f.program, f.source,
            f.points_cost, f.cash_cost, f.stops, f.cabin_class,
            f.available_seats, f.raw_flight_number, f.partner_airline,
            f.scraped_at_utc AS scraped_at, f.expires_at_utc AS expires_at,
            f.departure_time_local AS departure_time, f.arrival_time_local AS arrival_time,
            f.duration_minutes, f.aircraft_type,
            f.is_saver, f.fare_class, f.layover_airports, f.layover_duration_minutes,
            f.next_day_arrival, f.mixed_cabin,
            c.cash_price,
            -- DuckDB's round(DECIMAL, 2) returns a DOUBLE; Postgres' round(numeric, 2) returns
            -- NUMERIC. Cast to float8 so the driver yields a Python float, matching DuckDB's cpp
            -- type exactly (Decimal('0.41') != 0.41, so an uncast NUMERIC would break parity).
            CASE WHEN c.cash_price IS NOT NULL AND f.points_cost > 0
                 THEN round(c.cash_price / f.points_cost * 100, 2)::float8 END AS cpp
        FROM pp.flights f
        LEFT JOIN pp.cash_fares c
               ON c.origin = f.origin AND c.destination = f.destination AND c.date = f.date
              AND c.airline = f.airline AND c.cabin_class = f.cabin_class
              AND c.flight_number = f.raw_flight_number
              AND c.expires_at_utc > now()
        WHERE {where}
        ORDER BY f.date ASC, f.points_cost ASC
        LIMIT :limit
        """
    )

    result = conn.execute(sql, params)
    return [dict(zip(_GET_FLIGHTS_COLUMNS, row, strict=False)) for row in result.fetchall()]


def get_fresh_scrape_dates(
    conn: Connection, origin: str, destination: str, source: str | None = None
) -> set[date]:
    """Return the set of flight dates with at least one non-expired (expires_at > now()) row.

    Optionally scoped to one ``source`` (scraper slug) so a different airline's coverage of the same
    route doesn't suppress this one's scrape. Faithful port of the DuckDB DISTINCT-date query.
    """
    stmt = (
        select(Flight.date)
        .distinct()
        .where(
            Flight.origin == origin,
            Flight.destination == destination,
            Flight.expires_at_utc > func.now(),
        )
    )
    if source:
        stmt = stmt.where(Flight.source == source)
    return {row[0] for row in conn.execute(stmt).fetchall()}
