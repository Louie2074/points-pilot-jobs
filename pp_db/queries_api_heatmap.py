"""Ported API-only read queries (Postgres / SQLAlchemy) — the pp_db counterpart of two
api-repo helpers in the DuckDB ``db/queries.py``: ``get_heatmap`` and ``get_cash_fares``.

Both are read-only and live only in the API copy (the flexible-date heatmap + the cash-fare
lookup that feeds CPP). Each takes an explicit SQLAlchemy ``Connection`` as its first arg and must
match the DuckDB original row-for-row (verified by ``tests/test_parity_api_heatmap.py``).

Dialect notes:
  * Reproduced with ``text()`` so the GROUP BY / aggregate / ORDER BY / LIMIT are byte-faithful to
    the DuckDB SQL. Tables live in schema ``pp``.
  * ``current_date`` and ``now()`` are portable; both functions filter on the real session clock
    (``f.date >= current_date`` and, for cash, ``expires_at_utc > now()``).
  * NO float/round expression in either query — ``get_heatmap`` aggregates the INTEGER
    ``points_cost`` (MIN) and a COUNT (both yield Python ``int`` on each driver), and
    ``get_cash_fares`` selects the NUMERIC ``cash_price`` column verbatim (Decimal on both drivers).
    So there is no ``::float8`` keystone cast to apply here (unlike ``get_flights``' ``cpp``).
  * ``*_utc`` columns are naive TIMESTAMP; the engine pins the session to UTC so ``> now()`` lines
    up with the DuckDB original (which runs with ``TimeZone='UTC'``).
"""

from __future__ import annotations

from datetime import date
from typing import Any

from sqlalchemy import Connection, text


def get_heatmap(
    conn: Connection,
    origin: str,
    destination: str,
    date_from: date,
    date_to: date,
    cabin_class: str | None = None,
    airline: str | None = None,
) -> list[dict[str, Any]]:
    """Per-day cheapest-award summary for a route over a date window — the flexible-date heatmap.

    Faithful port of the DuckDB ``get_heatmap``: returns one row per day that has flights,
    ``{date, min_points, flight_count}``. Freshness matches ``get_flights``
    (``f.date >= current_date``, not expires-based). Optional cabin/airline filters. Grouped by
    day and ordered ``f.date ASC``.
    """
    filters = [
        "f.origin = :origin",
        "f.destination = :destination",
        "f.date BETWEEN :date_from AND :date_to",
        "f.date >= current_date",
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
    if airline:
        filters.append("f.airline = :airline")
        params["airline"] = airline

    sql = text(
        f"""
        SELECT f.date AS date, MIN(f.points_cost) AS min_points, COUNT(*) AS flight_count
        FROM pp.flights f
        WHERE {" AND ".join(filters)}
        GROUP BY f.date
        ORDER BY f.date ASC
        """
    )
    columns = ["date", "min_points", "flight_count"]
    rows = conn.execute(sql, params).fetchall()
    return [dict(zip(columns, row, strict=False)) for row in rows]


def get_cash_fares(
    conn: Connection,
    origin: str,
    destination: str,
    date_from: date,
    date_to: date,
    airline: str | None = None,
    cabin_class: str | None = None,
) -> list[dict[str, Any]]:
    """Return fresh (non-expired) cash fares for a route + date range.

    Faithful port of the DuckDB ``get_cash_fares``: filtered on the date window, ``date >=
    current_date`` and ``expires_at_utc > now()``, optionally by airline / cabin. Ordered by date
    ASC then cash_price ASC, capped at 200 rows. ``cash_price`` is selected verbatim (NUMERIC →
    Decimal on both drivers, so no cast).
    """
    filters = [
        "origin = :origin",
        "destination = :destination",
        "date BETWEEN :date_from AND :date_to",
        "date >= current_date",
        "expires_at_utc > now()",
    ]
    params: dict[str, Any] = {
        "origin": origin,
        "destination": destination,
        "date_from": date_from,
        "date_to": date_to,
    }
    if airline:
        filters.append("airline = :airline")
        params["airline"] = airline
    if cabin_class:
        filters.append("cabin_class = :cabin_class")
        params["cabin_class"] = cabin_class

    where = " AND ".join(filters)
    sql = text(
        f"""
        SELECT origin, destination, date, airline, cabin_class,
               flight_number, cash_price, currency, scraped_at_utc AS scraped_at
        FROM pp.cash_fares
        WHERE {where}
        ORDER BY date ASC, cash_price ASC
        LIMIT 200
        """
    )
    columns = [
        "origin",
        "destination",
        "date",
        "airline",
        "cabin_class",
        "flight_number",
        "cash_price",
        "currency",
        "scraped_at",
    ]
    rows = conn.execute(sql, params).fetchall()
    return [dict(zip(columns, row, strict=False)) for row in rows]
