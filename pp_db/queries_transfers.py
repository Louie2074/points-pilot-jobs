"""Ported query functions — bank programs, transfer partners & transfer bonuses (Postgres /
SQLAlchemy). The pp_db counterpart of the ``transfers`` block of the DuckDB ``db/queries.py``.

Each function takes an explicit SQLAlchemy ``Connection`` as its first arg, then the original
arguments. Behaviour must match the DuckDB original row-for-row (verified by the parity suite in
``tests/test_parity_transfers.py``).

Dialect notes:
  * Upserts use ``postgresql.insert(...).on_conflict_do_update`` on each table's natural key
    (mirroring DuckDB's ``ON CONFLICT (...) DO UPDATE SET col = excluded.col``).
  * ``get_transfer_options`` reproduces the DuckDB ``CEIL`` arithmetic verbatim. DuckDB returns a
    ``Decimal`` for ``base_points_needed`` but a ``float`` for ``effective_points_needed`` (the
    ``/ (1.0 + …)`` division promotes to DOUBLE). Postgres' ``ceil`` returns ``Numeric`` for both,
    so the port casts ``effective_points_needed`` to ``float`` to stay byte-identical to DuckDB.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from sqlalchemy import Connection, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from pp_db.models import BankProgram, TransferBonus, TransferPartner


def upsert_bank_program(conn: Connection, id: int, name: str, short_code: str) -> None:
    """Add or update a bank loyalty currency (e.g. Chase UR, Amex MR)."""
    stmt = pg_insert(BankProgram).values(id=id, name=name, short_code=short_code)
    stmt = stmt.on_conflict_do_update(
        index_elements=[BankProgram.id],
        set_={
            "name": stmt.excluded.name,
            "short_code": stmt.excluded.short_code,
        },
    )
    conn.execute(stmt)


def upsert_transfer_partner(
    conn: Connection,
    bank_program_id: int,
    airline_code: str,
    program_name: str,
    transfer_ratio: float = 1.0,
    min_transfer: int = 1000,
    transfer_increment: int = 1000,
) -> None:
    """
    Add or update a bank→airline transfer relationship.

    transfer_ratio: bank points required per 1 airline mile.
        1.0  → 1:1 (1 bank pt = 1 mile)
        3.0  → 3:1 (3 bank pts = 1 mile, e.g. Marriott→Alaska)
    """
    stmt = pg_insert(TransferPartner).values(
        bank_program_id=bank_program_id,
        airline_code=airline_code,
        program_name=program_name,
        transfer_ratio=transfer_ratio,
        min_transfer=min_transfer,
        transfer_increment=transfer_increment,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[TransferPartner.bank_program_id, TransferPartner.airline_code],
        set_={
            "program_name": stmt.excluded.program_name,
            "transfer_ratio": stmt.excluded.transfer_ratio,
            "min_transfer": stmt.excluded.min_transfer,
            "transfer_increment": stmt.excluded.transfer_increment,
        },
    )
    conn.execute(stmt)


def upsert_transfer_bonus(
    conn: Connection,
    bank_program_id: int,
    airline_code: str,
    bonus_pct: int,
    starts_at: date,
    ends_at: date,
    notes: str | None = None,
) -> None:
    """
    Record a transfer bonus offer. Matches on (bank_program_id, airline_code,
    starts_at) so re-running is safe — updates the bonus_pct and end date.
    """
    stmt = pg_insert(TransferBonus).values(
        bank_program_id=bank_program_id,
        airline_code=airline_code,
        bonus_pct=bonus_pct,
        starts_at=starts_at,
        ends_at=ends_at,
        notes=notes,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[
            TransferBonus.bank_program_id,
            TransferBonus.airline_code,
            TransferBonus.starts_at,
        ],
        set_={
            "bonus_pct": stmt.excluded.bonus_pct,
            "ends_at": stmt.excluded.ends_at,
            "notes": stmt.excluded.notes,
            "updated_at_utc": text("now()"),
        },
    )
    conn.execute(stmt)


# Column order matches the DuckDB original exactly so dict(zip(...)) yields identical keys.
_TRANSFER_OPTIONS_COLUMNS = [
    "bank_id",
    "bank_name",
    "short_code",
    "transfer_ratio",
    "min_transfer",
    "transfer_increment",
    "bonus_pct",
    "bonus_ends",
    "bonus_notes",
    "base_points_needed",
    "effective_points_needed",
]


def get_transfer_options(
    conn: Connection, airline_code: str, points_cost: int
) -> list[dict[str, Any]]:
    """
    Return all bank programs that can transfer to a given airline, with:
      - base points required (points_cost * transfer_ratio)
      - effective points required after any active bonus
      - current bonus details if applicable

    Results ordered by effective_points_needed ASC (best deal first).

    Faithful port: the SQL (incl. the ``CEIL`` arithmetic, the active-bonus LEFT JOIN windowed on
    ``current_date``, and the ``ORDER BY effective_points_needed ASC``) is identical to the DuckDB
    original. ``effective_points_needed`` is cast to ``float`` because DuckDB's DOUBLE division
    yields a Python float there, whereas Postgres ``ceil`` would otherwise return a Decimal.
    """
    sql = text(
        """
        SELECT
            bp.id                                       AS bank_id,
            bp.name                                     AS bank_name,
            bp.short_code,
            tp.transfer_ratio,
            tp.min_transfer,
            tp.transfer_increment,
            tb.bonus_pct,
            tb.ends_at                                  AS bonus_ends,
            tb.notes                                    AS bonus_notes,
            CEIL(:pc1 * tp.transfer_ratio)              AS base_points_needed,
            CEIL(:pc2 * tp.transfer_ratio
                 / (1.0 + COALESCE(tb.bonus_pct, 0) / 100.0))
                                                        AS effective_points_needed
        FROM pp.transfer_partners tp
        JOIN pp.bank_programs bp ON bp.id = tp.bank_program_id
        LEFT JOIN pp.transfer_bonuses tb
            ON  tb.bank_program_id = tp.bank_program_id
            AND tb.airline_code    = tp.airline_code
            AND tb.starts_at      <= current_date
            AND tb.ends_at        >= current_date
        WHERE tp.airline_code = :airline_code
        ORDER BY effective_points_needed ASC
        """
    )
    rows = conn.execute(
        sql,
        {"pc1": points_cost, "pc2": points_cost, "airline_code": airline_code},
    ).fetchall()

    out: list[dict[str, Any]] = []
    for row in rows:
        d = dict(zip(_TRANSFER_OPTIONS_COLUMNS, row, strict=False))
        # DuckDB returns effective_points_needed as a Python float (DOUBLE division); Postgres'
        # numeric ceil returns Decimal. Coerce to float for row-for-row parity.
        if d["effective_points_needed"] is not None:
            d["effective_points_needed"] = float(d["effective_points_needed"])
        out.append(d)
    return out


_ACTIVE_BONUSES_COLUMNS = [
    "bank_name",
    "short_code",
    "airline_code",
    "program_name",
    "bonus_pct",
    "starts_at",
    "ends_at",
    "notes",
]


def get_active_bonuses(conn: Connection) -> list[dict[str, Any]]:
    """Return all currently active transfer bonuses across all programs.

    Faithful port — identical join, ``current_date`` window, and
    ``ORDER BY tb.bonus_pct DESC, tb.ends_at ASC``.
    """
    sql = text(
        """
        SELECT
            bp.name        AS bank_name,
            bp.short_code,
            tb.airline_code,
            tp.program_name,
            tb.bonus_pct,
            tb.starts_at,
            tb.ends_at,
            tb.notes
        FROM pp.transfer_bonuses tb
        JOIN pp.bank_programs bp ON bp.id = tb.bank_program_id
        LEFT JOIN pp.transfer_partners tp
            ON  tp.bank_program_id = tb.bank_program_id
            AND tp.airline_code    = tb.airline_code
        WHERE tb.starts_at <= current_date
          AND tb.ends_at   >= current_date
        ORDER BY tb.bonus_pct DESC, tb.ends_at ASC
        """
    )
    rows = conn.execute(sql).fetchall()
    return [dict(zip(_ACTIVE_BONUSES_COLUMNS, row, strict=False)) for row in rows]
