"""Ported query functions (Postgres / SQLAlchemy Core) — the pp_db counterpart of the DuckDB
``db/queries.py``. Each function takes an explicit SQLAlchemy ``Connection`` so it is testable and
works under both the sync and async engines. Behaviour must match the DuckDB original row-for-row
(verified by the parity suite in ``tests/``).
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import Connection, func, select

from pp_db.airport_tz import AIRPORT_TZ
from pp_db.models import Flight

# Unified namespace: re-export every ported query so consumers do `from pp_db.queries import …`.
from pp_db.queries_api_checks import has_any_flights, is_window_stale, mark_route_scraped
from pp_db.queries_api_heatmap import get_cash_fares, get_heatmap
from pp_db.queries_api_transfers_cov import (
    get_ondemand_coverage,
    get_transfer_partners,
    upsert_ondemand_coverage,
)
from pp_db.queries_budget import checkout_budget, get_budget, refund_budget, upsert_budget
from pp_db.queries_cash import (
    get_top_cash_routes,
    upsert_cash_coverage,
    upsert_cash_fare,
    upsert_cash_fares,
)
from pp_db.queries_flights import get_flights, get_fresh_scrape_dates, upsert_flights
from pp_db.queries_reporting import cabin_distribution, route_coverage
from pp_db.queries_routes import (
    bump_decayed_demand,
    get_due_routes,
    get_route,
    increment_search_count,
    is_route_stale,
    record_scrape_outcome,
    reset_all_route_schedules,
    set_route_tier,
    upsert_route,
)
from pp_db.queries_transfers import (
    get_active_bonuses,
    get_transfer_options,
    upsert_bank_program,
    upsert_transfer_bonus,
    upsert_transfer_partner,
)


def get_flights_for_match(
    conn: Connection, origin: str, destination: str, travel_date: date, *, cabin: str
) -> list[tuple[str, str, str]]:
    """Award flights for one cabin/route/date as (airline, raw_flight_number, dep_hhmm).

    Port of the DuckDB original: ``dep_hhmm`` is the ORIGIN-local wall-clock (HH:MM) rendered from
    the TIMESTAMPTZ ``departure_time_local`` via ``AT TIME ZONE`` — here Postgres'
    ``timezone(tz, ts)`` + ``to_char(..., 'HH24:MI')`` (DuckDB used ``strftime(... , '%H:%M')``).
    Origins absent from AIRPORT_TZ return [] (never a possibly-skewed match). Nonstop, non-expired,
    real-flight-number rows only.
    """
    tz = AIRPORT_TZ.get(origin.upper())
    if tz is None:
        return []
    dep_hhmm = func.to_char(func.timezone(tz, Flight.departure_time_local), "HH24:MI")
    stmt = select(Flight.airline, Flight.raw_flight_number, dep_hhmm).where(
        Flight.origin == origin,
        Flight.destination == destination,
        Flight.date == travel_date,
        Flight.cabin_class == cabin,
        Flight.stops == 0,
        Flight.raw_flight_number.is_not(None),
        Flight.raw_flight_number != "UNKNOWN",
        Flight.departure_time_local.is_not(None),
        Flight.expires_at_utc > func.now(),
    )
    return [(a, fn, hhmm) for a, fn, hhmm in conn.execute(stmt).all()]
