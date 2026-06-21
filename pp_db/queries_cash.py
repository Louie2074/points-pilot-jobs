"""Ported cash query functions (Postgres / SQLAlchemy) — the pp_db counterpart of the cash-group
functions in the DuckDB ``db/queries.py``: ``upsert_cash_fare``, ``upsert_cash_fares``,
``get_top_cash_routes`` and ``upsert_cash_coverage``.

Each function takes an explicit SQLAlchemy ``Connection`` as its first argument; behaviour must
match the DuckDB original row-for-row (verified by ``tests/test_parity_cash.py``).

Dialect notes:
  * Upserts use ``postgresql.insert(...).on_conflict_do_update`` on the table's natural key
    (``cash_fares`` = 6-col UNIQUE incl. flight_number; ``cash_coverage`` = 4-col PK incl. cabin).
  * ``get_top_cash_routes`` is reproduced with ``text()`` to mirror the DuckDB CTE + correlated
    ``NOT EXISTS`` subqueries exactly. DuckDB's ``current_date + <int>`` and
    ``now() - (<num> * INTERVAL '1 hour')`` are both portable to Postgres with explicit casts.
  * ``*_utc`` columns are naive TIMESTAMP; the engine pins the session to UTC so ``> now()``
    comparisons line up with the DuckDB original (which also runs with ``TimeZone='UTC'``).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING

from sqlalchemy import Connection, bindparam, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from pp_db.models import CashCoverage, CashFare

if TYPE_CHECKING:
    from scrapers.base import CashFareRecord


def upsert_cash_fare(
    conn: Connection,
    origin: str,
    destination: str,
    date: date,
    airline: str,
    cabin_class: str,
    flight_number: str,
    cash_price: float,
    scraped_at_utc: datetime,
    expires_at_utc: datetime,
    currency: str = "USD",
    source: str = "google_flights",
) -> None:
    """Insert or update one cash fare (per-flight grain; shares flights' natural key).

    Port of the DuckDB ``upsert_cash_fare`` — same ON CONFLICT target (the 6-col UNIQUE incl.
    flight_number) and the same update set (cash_price/currency/source/scraped_at/expires_at).
    """
    stmt = pg_insert(CashFare).values(
        origin=origin,
        destination=destination,
        date=date,
        airline=airline,
        cabin_class=cabin_class,
        flight_number=flight_number,
        cash_price=cash_price,
        currency=currency,
        source=source,
        scraped_at_utc=scraped_at_utc,
        expires_at_utc=expires_at_utc,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[
            "origin",
            "destination",
            "date",
            "airline",
            "cabin_class",
            "flight_number",
        ],
        set_={
            "cash_price": stmt.excluded.cash_price,
            "currency": stmt.excluded.currency,
            "source": stmt.excluded.source,
            "scraped_at_utc": stmt.excluded.scraped_at_utc,
            "expires_at_utc": stmt.excluded.expires_at_utc,
        },
    )
    conn.execute(stmt)


def upsert_cash_fares(conn: Connection, records: list[CashFareRecord]) -> int:
    """Batch insert/update cash fares on the natural key. Returns rows processed.

    Port of the DuckDB ``upsert_cash_fares``: mirrors the single-row upsert's ON CONFLICT
    behaviour, applied to a batch. Empty input is a no-op returning 0.
    """
    if not records:
        return 0
    rows = [
        {
            "origin": r.origin,
            "destination": r.destination,
            "date": r.date,
            "airline": r.airline,
            "cabin_class": r.cabin_class,
            "flight_number": r.flight_number,
            "cash_price": r.cash_price,
            "currency": r.currency,
            "source": r.source,
            "scraped_at_utc": r.scraped_at_utc,
            "expires_at_utc": r.expires_at_utc,
        }
        for r in records
    ]
    stmt = pg_insert(CashFare)
    stmt = stmt.on_conflict_do_update(
        index_elements=[
            "origin",
            "destination",
            "date",
            "airline",
            "cabin_class",
            "flight_number",
        ],
        set_={
            "cash_price": stmt.excluded.cash_price,
            "currency": stmt.excluded.currency,
            "source": stmt.excluded.source,
            "scraped_at_utc": stmt.excluded.scraped_at_utc,
            "expires_at_utc": stmt.excluded.expires_at_utc,
        },
    )
    conn.execute(stmt, rows)
    return len(rows)


# DuckDB original SQL, parameterised for Postgres. The pinned-route placeholders are spliced into
# the IN-list and the ORDER BY uses the same pinned-first → demand DESC → date/origin/dest ASC rank.
_TOP_CASH_ROUTES_SQL = """
WITH demand AS (
    SELECT origin, dest, SUM(decayed_demand) AS sc
    FROM pp.routes_queue
    GROUP BY origin, dest
)
SELECT f.origin, f.destination, f.date, f.cabin_class
FROM (
    SELECT DISTINCT origin, destination, date, cabin_class
    FROM pp.flights
    WHERE cabin_class IN :cabins AND stops = 0 AND expires_at_utc > now()
      AND raw_flight_number IS NOT NULL AND raw_flight_number != 'UNKNOWN'
      AND date BETWEEN current_date AND current_date + CAST(:days_ahead AS integer)
) f
LEFT JOIN demand d ON d.origin = f.origin AND d.dest = f.destination
WHERE NOT EXISTS (
    SELECT 1 FROM pp.cash_fares c
    WHERE c.origin = f.origin AND c.destination = f.destination AND c.date = f.date
      AND c.cabin_class = f.cabin_class
      AND c.scraped_at_utc > now() - (CAST(:ttl_hours AS double precision) * INTERVAL '1 hour')
)
AND NOT EXISTS (
    SELECT 1 FROM pp.cash_coverage cc
    WHERE cc.origin = f.origin AND cc.destination = f.destination AND cc.date = f.date
      AND cc.cabin = f.cabin_class
      AND cc.fare_count = 0 AND cc.next_probe_utc > now()
)
ORDER BY {pin_rank}, COALESCE(d.sc, 0) DESC, f.date ASC, f.origin ASC, f.destination ASC
LIMIT :limit
"""


def get_top_cash_routes(
    conn: Connection,
    limit: int,
    days_ahead: int,
    ttl_hours: int,
    cabins: tuple[str, ...] = ("economy",),
) -> list[tuple[str, str, date, str]]:
    """Route/date/cabin units to scrape for cash — port of the DuckDB ``get_top_cash_routes``.

    Distinct (origin, dest, date, cabin) that HAVE non-expired nonstop award rows in one of the
    enabled ``cabins`` within ``days_ahead`` days AND lack fresh cash (no cash_fares row within
    ``ttl_hours``) AND are not a fresh zero-yield negative-memory entry. Ranked pinned-first, then
    summed route demand DESC, then date/origin/dest ASC. Returns (origin, dest, date, cabin).
    """
    from config.routes import CASH_PINNED_ROUTES

    # Both directions of each pinned hub, as "ORIG-DEST" keys for the ORDER BY rank below —
    # pinned routes are scheduled FIRST regardless of demand so a hub never loses cash coverage.
    pinned = sorted(
        {f"{o}-{d}" for o, d in CASH_PINNED_ROUTES} | {f"{d}-{o}" for o, d in CASH_PINNED_ROUTES}
    )
    params: dict = {
        "days_ahead": days_ahead,
        "ttl_hours": ttl_hours,
        "limit": limit,
    }
    if pinned:
        pin_keys = [f"pin_{i}" for i in range(len(pinned))]
        placeholders = ",".join(f":{k}" for k in pin_keys)
        pin_rank = f"CASE WHEN f.origin || '-' || f.destination IN ({placeholders}) THEN 0 ELSE 1 END"
        params.update(dict(zip(pin_keys, pinned, strict=False)))
    else:
        pin_rank = "1"

    sql = text(_TOP_CASH_ROUTES_SQL.format(pin_rank=pin_rank)).bindparams(
        # Expanding bindparam so IN :cabins becomes IN (:c1, :c2, ...) with the tuple's values.
        bindparam("cabins", value=list(cabins), expanding=True)
    )
    rows = conn.execute(sql, params).fetchall()
    return [(o, dst, dt, cab) for o, dst, dt, cab in rows]


def upsert_cash_coverage(
    conn: Connection,
    origin: str,
    destination: str,
    travel_date: date,
    *,
    cabin: str,
    fare_count: int,
    reprobe_days: int,
) -> None:
    """Record a cash-scrape attempt for (route, date, cabin) — port of DuckDB ``upsert_cash_coverage``.

    A ZERO-yield attempt pushes ``next_probe_utc`` out by ``reprobe_days``; any fares make the unit
    re-eligible immediately (``next_probe = now``). Origin/destination are upper-cased to match the
    original. ON CONFLICT target is the 4-col PK (origin, destination, date, cabin).
    """
    now = datetime.now(timezone.utc)
    next_probe = now + timedelta(days=reprobe_days) if fare_count == 0 else now
    stmt = pg_insert(CashCoverage).values(
        origin=origin.upper(),
        destination=destination.upper(),
        date=travel_date,
        cabin=cabin,
        last_attempt_utc=now,
        fare_count=fare_count,
        next_probe_utc=next_probe,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["origin", "destination", "date", "cabin"],
        set_={
            "last_attempt_utc": stmt.excluded.last_attempt_utc,
            "fare_count": stmt.excluded.fare_count,
            "next_probe_utc": stmt.excluded.next_probe_utc,
        },
    )
    conn.execute(stmt)
