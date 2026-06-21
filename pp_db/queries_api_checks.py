"""Ported query functions (Postgres / SQLAlchemy Core) — the api-only "checks" group of the DuckDB
``db/queries.py``. Each function takes an explicit SQLAlchemy ``Connection`` as its first arg, then
the original arguments. Behaviour must match the DuckDB original row-for-row (verified by the parity
suite ``tests/test_parity_api_checks.py``).

Group: has_any_flights, is_window_stale, mark_route_scraped.

  * ``has_any_flights`` — the positive "serves this route" signal for dispatch airlines: True if the
    route has EVER produced a flight row for the program airline (expiry ignored). The three inputs
    are upper-cased exactly like the original (``origin.upper()`` etc.).
  * ``is_window_stale`` — date-level staleness for a requested ``[date_from, date_to]`` window
    (unlike route-level ``is_route_stale``): True when no non-expired (``expires_at_utc > now()``)
    row falls in the window. ``airline`` scopes the check to one program.
  * ``mark_route_scraped`` — UPDATE after a successful scrape: stamps ``last_scraped_at_utc = now()``
    and ``next_scrape_at_utc = now() + ttl_hours``. Bare UPDATE — no-ops on a missing route.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from sqlalchemy import Connection, func, select, update

from pp_db.models import Flight, RoutesQueue


def has_any_flights(
    conn: Connection, origin: str, destination: str, airline: str
) -> bool:
    """True if the route has EVER produced flight rows for this program airline (ignores expiry). The
    positive 'serves this route' signal for dispatch airlines. Inputs are upper-cased to match the
    DuckDB original (``origin.upper()`` / ``destination.upper()`` / ``airline.upper()``)."""
    stmt = (
        select(1)
        .where(
            Flight.origin == origin.upper(),
            Flight.destination == destination.upper(),
            Flight.airline == airline.upper(),
        )
        .limit(1)
    )
    row = conn.execute(stmt).first()
    return row is not None


def is_window_stale(
    conn: Connection,
    origin: str,
    dest: str,
    date_from: date,
    date_to: date,
    airline: str | None = None,
) -> bool:
    """Return True if the route has no fresh data for the requested DATE WINDOW
    ``[date_from, date_to]`` — date-level staleness, unlike route-level ``is_route_stale``. Used to
    decide the on-demand GitHub-Actions dispatch for Delta / Southwest: a future-date search on a
    route that has fresh rows for *other* dates must still dispatch, not dead-end. ``airline`` scopes
    the check to one program. (api-only helper.)"""
    stmt = select(func.count()).select_from(Flight).where(
        Flight.origin == origin,
        Flight.destination == dest,
        Flight.date.between(date_from, date_to),
        Flight.expires_at_utc > func.now(),
    )
    if airline:
        stmt = stmt.where(Flight.airline == airline)
    count = conn.execute(stmt).scalar()
    return (count or 0) == 0


def mark_route_scraped(
    conn: Connection,
    origin: str,
    dest: str,
    tier: str,
    ttl_hours: int,
    airline: str = "alaska",
) -> None:
    """Update a route after a successful scrape (per airline). Sets last_scraped_at = now(),
    next_scrape_at = now() + ttl_hours. Bare UPDATE — no-ops on a missing route.

    DuckDB used ``now() + (? * INTERVAL '1 hour')``; the portable Postgres equivalent is
    ``now() + make_interval(hours => :ttl_hours)`` (``ttl_hours`` is an int, so a make_interval
    arg avoids the DuckDB-only ``int * INTERVAL`` multiply). ``tier`` is accepted for signature
    parity with the original but, as in DuckDB, is not written by this UPDATE."""
    conn.execute(
        update(RoutesQueue)
        .where(
            RoutesQueue.origin == origin,
            RoutesQueue.dest == dest,
            RoutesQueue.airline == airline,
        )
        .values(
            last_scraped_at_utc=func.now(),
            next_scrape_at_utc=func.now() + func.make_interval(0, 0, 0, 0, ttl_hours),
        )
    )
