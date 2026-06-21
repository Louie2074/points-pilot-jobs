"""Ported reporting/coverage query functions (Postgres / SQLAlchemy Core) — the pp_db counterpart
of the DuckDB ``db/queries.py`` coverage helpers (``route_coverage``, ``cabin_distribution``).

These are read-only observability queries that power ``check_coverage.py``. Each function takes an
explicit SQLAlchemy ``Connection`` as its first argument; behaviour must match the DuckDB original
row-for-row (verified by the parity suite in ``tests/test_parity_reporting.py``).

Dialect notes:
  * ``string_agg(DISTINCT cabin_class, ',')`` — DuckDB leaves the concatenation order unspecified;
    Postgres requires an explicit ``ORDER BY`` when ``DISTINCT`` is present. We add
    ``ORDER BY cabin_class`` so the port is deterministic. The parity tests therefore compare the
    ``cabin_list`` field as a *set* of comma-split values rather than as an ordered string.
  * ``count(DISTINCT origin || '-' || destination)`` — string concatenation via ``func.concat`` /
    the SQL ``||`` operator is portable; both engines treat it identically here (no NULL routes).
  * ``f.date >= current_date`` — ``current_date`` is portable; rendered via ``func.current_date()``.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import Connection, case, distinct, func, select
from sqlalchemy.dialects.postgresql import aggregate_order_by

from pp_db.models import Flight, RoutesQueue


def route_coverage(conn: Connection, source: str | None = "alaska") -> list[dict[str, Any]]:
    """Per-route coverage: every queued route LEFT JOINed to its future-dated flight rows, so routes
    with zero data still appear (the gaps we care about).

    Ordered fewest-rows-first (then HIGH→MED→LOW tier). ``source`` filters the joined flights
    (None = all sources); routes_queue itself is not source-tagged so all routes are listed.

    Port of the DuckDB original. The source filter lives in the LEFT JOIN's ON clause (not WHERE),
    so a route with no matching-source flights still surfaces with zero counts.
    """
    # Tier rank used for both ORDER BY here and in get_due_routes: HIGH→1, MED→2, else→3.
    tier_rank = case(
        (RoutesQueue.priority_tier == "HIGH", 1),
        (RoutesQueue.priority_tier == "MED", 2),
        else_=3,
    )

    # cabin_list: string_agg(DISTINCT cabin_class, ',') — matches the DuckDB original verbatim
    # (concatenation order unspecified on both engines; the parity test compares as a set).
    cabin_list = func.string_agg(distinct(Flight.cabin_class), ",")

    # LEFT JOIN ON: route match + future-dated; source filter (when given) also rides the ON clause
    # so a no-data route still appears (it would be dropped if pushed to WHERE).
    on_clause = (
        (Flight.origin == RoutesQueue.origin)
        & (Flight.destination == RoutesQueue.dest)
        & (Flight.date >= func.current_date())
    )
    if source is not None:
        on_clause = on_clause & (Flight.source == source)

    stmt = (
        select(
            RoutesQueue.origin,
            RoutesQueue.dest,
            RoutesQueue.airline,
            RoutesQueue.priority_tier,
            RoutesQueue.search_count,
            RoutesQueue.last_scraped_at_utc.label("last_scraped_at"),
            RoutesQueue.next_scrape_at_utc.label("next_scrape_at"),
            func.count(Flight.id).label("flight_rows"),
            func.count(distinct(Flight.date)).label("dates_covered"),
            func.count(distinct(Flight.cabin_class)).label("cabins_seen"),
            cabin_list.label("cabin_list"),
            func.min(Flight.date).label("first_date"),
            func.max(Flight.date).label("last_date"),
            func.max(Flight.scraped_at_utc).label("last_flight_scrape"),
        )
        .select_from(RoutesQueue)
        .outerjoin(Flight, on_clause)
        .group_by(
            RoutesQueue.origin,
            RoutesQueue.dest,
            RoutesQueue.airline,
            RoutesQueue.priority_tier,
            RoutesQueue.search_count,
            RoutesQueue.last_scraped_at_utc,
            RoutesQueue.next_scrape_at_utc,
        )
        .order_by(func.count(Flight.id).asc(), tier_rank)
    )

    columns = [
        "origin",
        "dest",
        "airline",
        "priority_tier",
        "search_count",
        "last_scraped_at",
        "next_scrape_at",
        "flight_rows",
        "dates_covered",
        "cabins_seen",
        "cabin_list",
        "first_date",
        "last_date",
        "last_flight_scrape",
    ]
    return [dict(zip(columns, row, strict=False)) for row in conn.execute(stmt).all()]


def cabin_distribution(conn: Connection, source: str | None = "alaska") -> list[dict[str, Any]]:
    """Row / route / date counts per cabin across future-dated flights — a cabin that's suddenly
    absent points at a CABIN_MAP miss or a points<=0 drop.

    Port of the DuckDB original. ``routes`` counts DISTINCT ``origin || '-' || destination`` pairs.
    """
    filters = [Flight.date >= func.current_date()]
    if source is not None:
        filters.append(Flight.source == source)

    route_key = Flight.origin.concat("-").concat(Flight.destination)

    stmt = (
        select(
            Flight.cabin_class,
            func.count().label("rows"),
            func.count(distinct(route_key)).label("routes"),
            func.count(distinct(Flight.date)).label("dates"),
        )
        .where(*filters)
        .group_by(Flight.cabin_class)
        .order_by(func.count().desc())
    )

    columns = ["cabin_class", "rows", "routes", "dates"]
    return [dict(zip(columns, row, strict=False)) for row in conn.execute(stmt).all()]
