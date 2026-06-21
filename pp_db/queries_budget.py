"""Ported per-airline scrape-budget query functions (Postgres / SQLAlchemy Core) — the pp_db
counterpart of the token-bucket helpers in the DuckDB ``db/queries.py``.

Covers the ``airline_budget`` token bucket: ``get_budget`` (read), ``upsert_budget`` (seed/refresh
config), ``checkout_budget`` (refill-then-grant), and ``refund_budget`` (return unspent tokens).

Each function takes an explicit SQLAlchemy ``Connection`` as its first arg; behaviour must match the
DuckDB original row-for-row (verified by ``tests/test_parity_budget.py``). The pure token-bucket math
(``refill`` / ``grant``) still lives in ``pipeline.budget`` and is reused unchanged.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import Connection, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from pp_db.models import AirlineBudget


def get_budget(conn: Connection, airline: str) -> dict[str, Any] | None:
    """Fetch an airline's token-bucket row, or None if unconfigured.

    Dict keys mirror the DuckDB original: the ``last_refill_utc`` column is exposed as ``last_refill``.
    """
    stmt = select(
        AirlineBudget.airline,
        AirlineBudget.tokens,
        AirlineBudget.capacity,
        AirlineBudget.refill_per_hour,
        AirlineBudget.last_refill_utc,
    ).where(AirlineBudget.airline == airline)
    row = conn.execute(stmt).first()
    if not row:
        return None
    keys = ["airline", "tokens", "capacity", "refill_per_hour", "last_refill"]
    return dict(zip(keys, row, strict=False))


def upsert_budget(conn: Connection, airline: str, capacity: float, refill_per_hour: float) -> None:
    """Seed/refresh an airline's bucket config. On first insert tokens start full (= capacity);
    re-seeding updates capacity/refill but preserves current tokens + last_refill."""
    stmt = pg_insert(AirlineBudget).values(
        airline=airline,
        tokens=capacity,
        capacity=capacity,
        refill_per_hour=refill_per_hour,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[AirlineBudget.airline],
        set_={
            "capacity": stmt.excluded.capacity,
            "refill_per_hour": stmt.excluded.refill_per_hour,
        },
    )
    conn.execute(stmt)


def checkout_budget(conn: Connection, airline: str, requested: int, now: datetime) -> int:
    """Refill the bucket to ``now``, grant up to ``requested`` whole tokens, persist the
    remainder. An unconfigured airline (no row) is ungated — the full request is granted."""
    from pipeline import budget

    row = get_budget(conn, airline)
    if row is None:
        return requested
    # last_refill_utc is a naive TIMESTAMP; coerce it to aware UTC so it's comparable with an aware
    # ``now`` (mirrors the DuckDB original / scheduler.py normalization).
    last_refill = row["last_refill"]
    if last_refill is not None and last_refill.tzinfo is None and now.tzinfo is not None:
        last_refill = last_refill.replace(tzinfo=timezone.utc)
    refilled = budget.refill(
        row["tokens"], row["capacity"], row["refill_per_hour"], last_refill, now
    )
    granted = budget.grant(refilled, requested)
    conn.execute(
        update(AirlineBudget)
        .where(AirlineBudget.airline == airline)
        .values(tokens=refilled - granted, last_refill_utc=now)
    )
    return granted


def refund_budget(conn: Connection, airline: str, tokens: int, now: datetime) -> None:
    """Return unused ``tokens`` to the bucket, capped at capacity. No-op if the airline has no bucket
    row. Unlike ``checkout_budget`` this does NOT touch ``last_refill_utc`` — a refund is not a
    refill, it's giving back tokens checked out but never spent."""
    row = get_budget(conn, airline)
    if row is None:
        return
    new = min(row["capacity"], row["tokens"] + tokens)
    conn.execute(
        update(AirlineBudget).where(AirlineBudget.airline == airline).values(tokens=new)
    )
