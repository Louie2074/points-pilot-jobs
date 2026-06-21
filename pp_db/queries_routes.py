"""Ported query functions (Postgres / SQLAlchemy Core) — the routes-queue group of the DuckDB
``db/queries.py``. Each function takes an explicit SQLAlchemy ``Connection`` as its first arg, then
the original arguments. Behaviour must match the DuckDB original row-for-row (verified by the parity
suite ``tests/test_parity_routes.py``).

Group: upsert_route, get_due_routes, get_route, bump_decayed_demand, record_scrape_outcome,
reset_all_route_schedules, increment_search_count, set_route_tier, is_route_stale.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import Connection, case, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from pp_db.models import Flight, RoutesQueue

# Column list returned by get_due_routes / get_route, in the original SELECT order. The aliased
# ``*_utc`` columns are renamed to the bare key the DuckDB query used (AS last_scraped_at, etc.).
_ROUTE_COLUMNS = [
    "origin",
    "dest",
    "airline",
    "priority_tier",
    "search_count",
    "last_scraped_at",
    "next_scrape_at",
    "decayed_demand",
    "last_search_at",
    "change_rate",
    "interval_h",
    "last_cheapest",
]

# select() column expressions matching the original column order (with the AS-renames baked in).
_ROUTE_SELECT_COLS = [
    RoutesQueue.origin,
    RoutesQueue.dest,
    RoutesQueue.airline,
    RoutesQueue.priority_tier,
    RoutesQueue.search_count,
    RoutesQueue.last_scraped_at_utc.label("last_scraped_at"),
    RoutesQueue.next_scrape_at_utc.label("next_scrape_at"),
    RoutesQueue.decayed_demand,
    RoutesQueue.last_search_at_utc.label("last_search_at"),
    RoutesQueue.change_rate,
    RoutesQueue.interval_h,
    RoutesQueue.last_cheapest,
]


def upsert_route(
    conn: Connection, origin: str, dest: str, tier: str, airline: str = "alaska"
) -> None:
    """Add a route to the queue if it doesn't exist (per airline). Idempotent — existing rows
    are NOT modified (ON CONFLICT DO NOTHING on the (origin, dest, airline) PK)."""
    stmt = (
        pg_insert(RoutesQueue)
        .values(origin=origin, dest=dest, airline=airline, priority_tier=tier)
        .on_conflict_do_nothing(index_elements=["origin", "dest", "airline"])
    )
    conn.execute(stmt)


def get_due_routes(
    conn: Connection, limit: int = 50, airline: str | None = None
) -> list[dict[str, Any]]:
    """Return routes whose next_scrape_at has passed, ordered by priority + staleness.

    Priority order: HIGH → MED → LOW, then by next_scrape_at ASC (most overdue first).
    ``airline`` filters to one scraper's queue (None = all airlines).
    """
    # CASE priority_tier WHEN 'HIGH' THEN 1 WHEN 'MED' THEN 2 ELSE 3 END
    tier_rank = case(
        (RoutesQueue.priority_tier == "HIGH", 1),
        (RoutesQueue.priority_tier == "MED", 2),
        else_=3,
    )
    stmt = select(*_ROUTE_SELECT_COLS).where(RoutesQueue.next_scrape_at_utc <= func.now())
    if airline:
        stmt = stmt.where(RoutesQueue.airline == airline)
    # next_scrape_at_utc is NOT NULL, so ASC ordering needs no NULLS-clause to match DuckDB.
    stmt = stmt.order_by(tier_rank, RoutesQueue.next_scrape_at_utc.asc()).limit(limit)

    rows = conn.execute(stmt).all()
    return [dict(zip(_ROUTE_COLUMNS, row, strict=False)) for row in rows]


def get_route(
    conn: Connection, origin: str, dest: str, airline: str = "alaska"
) -> dict[str, Any] | None:
    """Fetch a single route queue entry (per airline), or None if not found."""
    stmt = select(*_ROUTE_SELECT_COLS).where(
        RoutesQueue.origin == origin,
        RoutesQueue.dest == dest,
        RoutesQueue.airline == airline,
    )
    row = conn.execute(stmt).first()
    if row is None:
        return None
    return dict(zip(_ROUTE_COLUMNS, row, strict=False))


def bump_decayed_demand(
    conn: Connection,
    origin: str,
    dest: str,
    airline: str,
    now: datetime,
    half_life_days: float,
) -> float:
    """Decay the route's stored demand to ``now`` and add 1 for a new search. Creates the row
    (LOW) if absent. Returns the new decayed_demand. Read-modify-write under the single writer."""
    from pipeline import scoring

    conn.execute(
        pg_insert(RoutesQueue)
        .values(origin=origin, dest=dest, airline=airline, priority_tier="LOW")
        .on_conflict_do_nothing(index_elements=["origin", "dest", "airline"])
    )
    row = conn.execute(
        select(RoutesQueue.decayed_demand, RoutesQueue.last_search_at_utc).where(
            RoutesQueue.origin == origin,
            RoutesQueue.dest == dest,
            RoutesQueue.airline == airline,
        )
    ).first()
    stored = row[0] if row else 0.0
    last = row[1] if row else None
    # last_search_at_utc is stored naive (TIMESTAMP WITHOUT TIME ZONE); coerce to aware UTC so it's
    # subtractable from the aware ``now`` in scoring.decay_demand. Mirrors the DuckDB original.
    if last is not None and last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    new_val = scoring.bump_demand(stored, last, now, half_life_days)
    conn.execute(
        update(RoutesQueue)
        .where(
            RoutesQueue.origin == origin,
            RoutesQueue.dest == dest,
            RoutesQueue.airline == airline,
        )
        .values(decayed_demand=new_val, last_search_at_utc=now)
    )
    return new_val


def record_scrape_outcome(
    conn: Connection,
    origin: str,
    dest: str,
    airline: str,
    *,
    interval_h: float,
    change_rate: float,
    last_cheapest: str,
    next_scrape_at: datetime,
) -> None:
    """Persist a route's adaptive-scrape outcome: stamps last_scraped now, sets next_scrape_at
    to the adaptive interval, and saves change_rate / interval_h / last_cheapest (JSON string).
    Assumes the route row already exists — this bare UPDATE no-ops on a missing route."""
    conn.execute(
        update(RoutesQueue)
        .where(
            RoutesQueue.origin == origin,
            RoutesQueue.dest == dest,
            RoutesQueue.airline == airline,
        )
        .values(
            last_scraped_at_utc=func.now(),
            next_scrape_at_utc=next_scrape_at,
            interval_h=interval_h,
            change_rate=change_rate,
            last_cheapest=last_cheapest,
        )
    )


def reset_all_route_schedules(conn: Connection) -> int:
    """Mark every route as due-for-scrape RIGHT NOW. Returns the route count.

    Called on scraper startup so the first scheduler tick always picks every route up.
    """
    conn.execute(update(RoutesQueue).values(next_scrape_at_utc=func.now()))
    n = conn.execute(select(func.count()).select_from(RoutesQueue)).scalar()
    return n


def increment_search_count(
    conn: Connection, origin: str, dest: str, airline: str = "alaska"
) -> int:
    """Increment search_count for a route (per airline). Returns the new count.
    If the route doesn't exist yet, creates it as LOW tier."""
    # Ensure the row exists (idempotent)
    conn.execute(
        pg_insert(RoutesQueue)
        .values(origin=origin, dest=dest, airline=airline, priority_tier="LOW")
        .on_conflict_do_nothing(index_elements=["origin", "dest", "airline"])
    )
    conn.execute(
        update(RoutesQueue)
        .where(
            RoutesQueue.origin == origin,
            RoutesQueue.dest == dest,
            RoutesQueue.airline == airline,
        )
        .values(search_count=RoutesQueue.search_count + 1)
    )
    row = conn.execute(
        select(RoutesQueue.search_count).where(
            RoutesQueue.origin == origin,
            RoutesQueue.dest == dest,
            RoutesQueue.airline == airline,
        )
    ).first()
    return row[0] if row else 0


def set_route_tier(
    conn: Connection, origin: str, dest: str, new_tier: str, airline: str = "alaska"
) -> None:
    """Update the priority tier for a route (per airline)."""
    conn.execute(
        update(RoutesQueue)
        .where(
            RoutesQueue.origin == origin,
            RoutesQueue.dest == dest,
            RoutesQueue.airline == airline,
        )
        .values(priority_tier=new_tier)
    )


def is_route_stale(
    conn: Connection, origin: str, dest: str, airline: str | None = None
) -> bool:
    """Return True if the route has no fresh data — either never scraped, or all its flights are
    expired. When ``airline`` (program IATA code, e.g. "B6") is given, staleness is scoped to that
    airline so one airline's coverage of the route doesn't mask another's gap."""
    stmt = select(func.count()).select_from(Flight).where(
        Flight.origin == origin,
        Flight.destination == dest,
        Flight.expires_at_utc > func.now(),
    )
    if airline:
        stmt = stmt.where(Flight.airline == airline)
    count = conn.execute(stmt).scalar()
    return (count or 0) == 0
