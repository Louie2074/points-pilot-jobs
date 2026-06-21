"""Ported transfer-partner + on-demand-coverage query functions (Postgres / SQLAlchemy) — the pp_db
counterpart of the api-side DuckDB ``db/queries.py``: ``get_transfer_partners``,
``get_ondemand_coverage`` and ``upsert_ondemand_coverage``.

Each function takes an explicit SQLAlchemy ``Connection`` as its first argument; behaviour must match
the DuckDB original row-for-row (verified by ``tests/test_parity_api_transfers_cov.py``).

Dialect notes:
  * ``get_transfer_partners`` is reproduced with ``text()`` so the bidirectional ``(? IS NULL OR …
    ILIKE … OR … ILIKE …)`` filter, the ``ILIKE`` substring matches and the
    ``ORDER BY bp.name, tp.transfer_ratio ASC`` are byte-faithful to the DuckDB original. The
    ``? IS NULL`` guards are kept as explicit ``:bank IS NULL`` / ``:airline IS NULL`` bind params
    (typed as text so an all-NULL bind is unambiguous to Postgres).
  * ``transfer_ratio`` is ``DECIMAL/NUMERIC(5,2)`` in BOTH engines — DuckDB and the psycopg driver
    each yield ``decimal.Decimal``, so NO ``::float8`` cast is needed (it would BREAK parity here).
  * ``upsert_ondemand_coverage`` upserts on the 3-col PK (origin, destination, airline); origin/
    destination/airline are upper-cased to match the original. ``next_probe`` is pushed out by
    ``reprobe_ttl_days`` for a zero-result attempt, else collapses to ``now`` (negative memory).
  * ``*_utc`` columns are naive TIMESTAMP; the engine pins the session to UTC so the timestamps the
    upsert writes (``datetime.now(timezone.utc)``) line up with the DuckDB original.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import Connection, String, bindparam, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from pp_db.models import OndemandCoverage


# DuckDB original SQL, parameterised for Postgres. The bidirectional filter and the
# ``ORDER BY bp.name, tp.transfer_ratio ASC`` mirror the original exactly.
_TRANSFER_PARTNERS_SQL = """
SELECT
    bp.name              AS bank_name,
    bp.short_code,
    tp.airline_code,
    tp.program_name,
    tp.transfer_ratio,
    tp.min_transfer,
    tp.transfer_increment,
    tb.bonus_pct,
    tb.ends_at           AS bonus_ends
FROM pp.transfer_partners tp
JOIN pp.bank_programs bp ON bp.id = tp.bank_program_id
LEFT JOIN pp.transfer_bonuses tb
    ON  tb.bank_program_id = tp.bank_program_id
    AND tb.airline_code    = tp.airline_code
    AND tb.starts_at      <= current_date
    AND tb.ends_at        >= current_date
WHERE (:bank IS NULL OR bp.short_code ILIKE :bank_term OR bp.name ILIKE :bank_term)
  AND (:airline IS NULL OR tp.airline_code = :airline_code OR tp.program_name ILIKE :airline_term)
ORDER BY bp.name, tp.transfer_ratio ASC
"""

_TRANSFER_PARTNERS_COLUMNS = [
    "bank_name",
    "short_code",
    "airline_code",
    "program_name",
    "transfer_ratio",
    "min_transfer",
    "transfer_increment",
    "bonus_pct",
    "bonus_ends",
]


def get_transfer_partners(
    conn: Connection,
    bank: str | None = None,
    airline: str | None = None,
) -> list[dict[str, Any]]:
    """Return the bank ↔ airline transfer-partner matrix with ratios — port of the DuckDB
    ``get_transfer_partners``.

    Optionally filtered by ``bank`` and/or ``airline`` in BOTH directions. ``bank`` matches a
    program's short_code OR name (case/substring-insensitive ``ILIKE %term%``); ``airline`` matches
    the IATA code (exact, upper-cased) OR the program name (``ILIKE %term%``). Either/both omitted →
    the full matrix. Any active transfer bonus is attached. Ordered by bank name, then best ratio
    first (``ORDER BY bp.name, tp.transfer_ratio ASC``).
    """
    bank_term = f"%{bank.strip()}%" if bank else None
    airline_term = f"%{airline.strip()}%" if airline else None
    airline_code = airline.strip().upper() if airline else None

    sql = text(_TRANSFER_PARTNERS_SQL).bindparams(
        # Type the NULLable filter binds as text so an all-NULL bind is unambiguous to Postgres
        # (mirrors DuckDB's untyped ``? IS NULL`` guard, which never needs an explicit cast).
        bindparam("bank", type_=String),
        bindparam("bank_term", type_=String),
        bindparam("airline", type_=String),
        bindparam("airline_code", type_=String),
        bindparam("airline_term", type_=String),
    )
    rows = conn.execute(
        sql,
        {
            "bank": bank,
            "bank_term": bank_term,
            "airline": airline,
            "airline_code": airline_code,
            "airline_term": airline_term,
        },
    ).fetchall()
    return [dict(zip(_TRANSFER_PARTNERS_COLUMNS, row, strict=False)) for row in rows]


def upsert_ondemand_coverage(
    conn: Connection,
    origin: str,
    destination: str,
    airline: str,
    *,
    result_count: int,
    reprobe_ttl_days: int,
) -> None:
    """Record an on-demand inline-scrape attempt — port of the DuckDB ``upsert_ondemand_coverage``.

    A ZERO-result attempt pushes ``next_probe_utc`` out by ``reprobe_ttl_days`` (negative memory —
    don't hammer an airline that returned nothing); any results make it re-eligible immediately
    (``next_probe = now``). Origin/destination/airline are upper-cased to match the original.
    ON CONFLICT target is the 3-col PK (origin, destination, airline).
    """
    now = datetime.now(timezone.utc)
    next_probe = now + timedelta(days=reprobe_ttl_days) if result_count == 0 else now
    stmt = pg_insert(OndemandCoverage).values(
        origin=origin.upper(),
        destination=destination.upper(),
        airline=airline.upper(),
        last_attempt_utc=now,
        result_count=result_count,
        next_probe_utc=next_probe,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["origin", "destination", "airline"],
        set_={
            "last_attempt_utc": stmt.excluded.last_attempt_utc,
            "result_count": stmt.excluded.result_count,
            "next_probe_utc": stmt.excluded.next_probe_utc,
        },
    )
    conn.execute(stmt)


def get_ondemand_coverage(
    conn: Connection, origin: str, destination: str
) -> dict[str, dict[str, Any]]:
    """Return ``{airline_iata: {result_count, last_attempt_utc, next_probe_utc}}`` for a route —
    port of the DuckDB ``get_ondemand_coverage``. Origin/destination are upper-cased to match the
    original lookup.
    """
    rows = conn.execute(
        text(
            """
            SELECT airline, result_count, last_attempt_utc, next_probe_utc
            FROM pp.ondemand_coverage WHERE origin = :origin AND destination = :destination
            """
        ),
        {"origin": origin.upper(), "destination": destination.upper()},
    ).fetchall()
    return {
        r[0]: {"result_count": r[1], "last_attempt_utc": r[2], "next_probe_utc": r[3]} for r in rows
    }
