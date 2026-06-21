"""Unit tests for transfer_bonuses.py.

parse_bonuses: pure HTML → list[dict], tested with a minimal fixture that mirrors
the travel-on-points.com table structure (4 cols: Point Program, Bonus Rate,
Airline / Hotel Program, End Date). No network/DB — always runs.

reconcile: snapshot-replace logic, now against the real ``pp`` Postgres container (the
MotherDuck→Supabase cutover ported ``reconcile`` from DuckDB to a SQLAlchemy Connection on
``pp.transfer_bonuses``). Those tests skip if ``DATABASE_URL`` is unset and run inside a
rolled-back transaction so they leave the table untouched.
"""

from __future__ import annotations

import os
from datetime import date

import pytest

from transfer_bonuses import parse_bonuses, reconcile

_NEEDS_PG = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL unset — reconcile test needs a live pp schema",
)

# ---------------------------------------------------------------------------
# Minimal HTML fixture — mirrors the travel-on-points.com table structure.
# Contains:
#   - one valid airline bonus (American Express → Air France)
#   - one with a trailing asterisk in the airline cell (Chase → JetBlue*)
#   - one hotel destination to skip (Capital One → Marriott Bonvoy)
#   - one unknown bank to skip (Rove Miles → Air Canada Aeroplan)
# Dates use the real page format: "M/D/YY"
# ---------------------------------------------------------------------------
HTML_FIXTURE = """\
<html><body>
<table>
<tr>
  <td>Point Program</td><td>Bonus Rate</td>
  <td>Airline / Hotel Program</td><td>End Date</td>
</tr>
<tr>
  <td>American Express</td>
  <td>25%</td>
  <td>Air France/KLM Flying Blue</td>
  <td>6/30/26</td>
</tr>
<tr>
  <td>Chase</td>
  <td>30%</td>
  <td>JetBlue TrueBlue*</td>
  <td>7/15/26</td>
</tr>
<tr>
  <td>Capital One</td>
  <td>55%</td>
  <td>Marriott Bonvoy</td>
  <td>6/30/26</td>
</tr>
<tr>
  <td>Rove Miles</td>
  <td>25%</td>
  <td>Air Canada Aeroplan</td>
  <td>6/6/26</td>
</tr>
</table>
</body></html>
"""

TODAY = date(2026, 6, 6)


# ---------------------------------------------------------------------------
# parse_bonuses tests
# ---------------------------------------------------------------------------


def test_parse_valid_bonus():
    """American Express → Air France: pct, dates, and notes parsed correctly."""
    records = parse_bonuses(HTML_FIXTURE, today=TODAY)
    amex_af = next((r for r in records if r["airline_code"] == "AF"), None)
    assert amex_af is not None
    assert amex_af["bank_program_id"] == 2  # American Express
    assert amex_af["bonus_pct"] == 25
    assert amex_af["starts_at"] == TODAY  # starts_at always = today on this site
    assert amex_af["ends_at"] == date(2026, 6, 30)
    assert amex_af["notes"] is None


def test_parse_asterisk_stripped_into_notes():
    """Trailing asterisk in airline cell is stripped for lookup; raw text stored in notes."""
    records = parse_bonuses(HTML_FIXTURE, today=TODAY)
    jetblue = next((r for r in records if r["airline_code"] == "B6"), None)
    assert jetblue is not None
    assert jetblue["bank_program_id"] == 1  # Chase
    assert jetblue["bonus_pct"] == 30
    assert jetblue["ends_at"] == date(2026, 7, 15)
    # Raw cell "JetBlue TrueBlue*" stored in notes because it was altered
    assert jetblue["notes"] == "JetBlue TrueBlue*"


def test_parse_hotel_destination_skipped():
    """'Marriott Bonvoy' as a destination → skipped; only AF and B6 survive."""
    records = parse_bonuses(HTML_FIXTURE, today=TODAY)
    assert len(records) == 2
    codes = {r["airline_code"] for r in records}
    assert codes == {"AF", "B6"}


def test_parse_unknown_bank_skipped():
    """'Rove Miles' is not in BANK_MAP → its row is silently skipped."""
    html = """\
<html><body>
<table>
<tr><td>Point Program</td><td>Bonus Rate</td><td>Airline / Hotel Program</td><td>End Date</td></tr>
<tr>
  <td>Rove Miles</td>
  <td>25%</td>
  <td>Air Canada Aeroplan</td>
  <td>6/6/26</td>
</tr>
</table>
</body></html>
"""
    records = parse_bonuses(html, today=TODAY)
    assert records == []


def test_parse_no_table_raises():
    """If the page has no <table>, raise ValueError — structure changed."""
    with pytest.raises(ValueError, match="No <table>"):
        parse_bonuses("<html><body><p>nothing here</p></body></html>", today=TODAY)


# ---------------------------------------------------------------------------
# reconcile tests — in-memory DuckDB, no MotherDuck needed
# ---------------------------------------------------------------------------


@pytest.fixture()
def pg_conn():
    """A pp_db engine Connection inside a transaction, seeded so the DELETE-scope predicate
    (``airline_code IN (SELECT airline_code FROM pp.transfer_partners)``) has the two tracked
    airlines AS + AF, plus one stale AS bonus. Rolled back at teardown so the real
    ``pp.transfer_bonuses``/``transfer_partners`` tables are untouched — mirrors production, where
    ``reconcile`` runs inside ``get_engine().begin()``."""
    from sqlalchemy import text

    from pp_db.engine import get_engine

    conn = get_engine().connect()
    trans = conn.begin()
    conn.execute(text("DELETE FROM pp.transfer_bonuses"))
    conn.execute(text("DELETE FROM pp.transfer_partners"))
    # Two tracked airlines: AS (Alaska) and AF (Air France).
    conn.execute(
        text(
            "INSERT INTO pp.transfer_partners "
            "(bank_program_id, airline_code, program_name) VALUES "
            "(5, 'AS', 'Mileage Plan'), (2, 'AF', 'Flying Blue')"
        )
    )
    # A stale AS bonus the snapshot must replace/clear.
    conn.execute(
        text(
            "INSERT INTO pp.transfer_bonuses "
            "(bank_program_id, airline_code, bonus_pct, starts_at, ends_at) "
            "VALUES (5, 'AS', 30, '2026-01-01', '2026-01-31')"
        )
    )
    try:
        yield conn
    finally:
        trans.rollback()
        conn.close()


@_NEEDS_PG
def test_reconcile_replaces_existing(pg_conn):
    """Existing bonus is deleted; fresh record is inserted."""
    from sqlalchemy import text

    assert pg_conn.execute(text("SELECT COUNT(*) FROM pp.transfer_bonuses")).scalar() == 1

    fresh = [
        {
            "bank_program_id": 2,  # Amex
            "airline_code": "AF",
            "bonus_pct": 25,
            "starts_at": date(2026, 6, 6),
            "ends_at": date(2026, 6, 30),
            "notes": None,
        }
    ]
    deleted, inserted = reconcile(pg_conn, fresh)
    assert deleted == 1
    assert inserted == 1
    rows = pg_conn.execute(
        text("SELECT bank_program_id, airline_code, bonus_pct FROM pp.transfer_bonuses")
    ).fetchall()
    assert [tuple(r) for r in rows] == [(2, "AF", 25)]


@_NEEDS_PG
def test_reconcile_zero_bonuses_clears_table(pg_conn):
    """Empty records list → DELETE fires, nothing inserted. Valid (no active bonuses)."""
    from sqlalchemy import text

    deleted, inserted = reconcile(pg_conn, [])
    assert deleted == 1
    assert inserted == 0
    assert pg_conn.execute(text("SELECT COUNT(*) FROM pp.transfer_bonuses")).scalar() == 0


@_NEEDS_PG
def test_reconcile_dry_run_leaves_table_unchanged(pg_conn):
    """--dry-run: no DB changes, returns (0, 0)."""
    from sqlalchemy import text

    fresh = [
        {
            "bank_program_id": 2,
            "airline_code": "AF",
            "bonus_pct": 25,
            "starts_at": date(2026, 6, 6),
            "ends_at": date(2026, 6, 30),
            "notes": None,
        }
    ]
    deleted, inserted = reconcile(pg_conn, fresh, dry_run=True)
    assert (deleted, inserted) == (0, 0)
    # Table unchanged — stale AS bonus still present
    assert pg_conn.execute(text("SELECT COUNT(*) FROM pp.transfer_bonuses")).scalar() == 1
