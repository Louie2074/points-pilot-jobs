"""Unit tests for transfer_bonuses.py — no network, no MotherDuck required.

parse_bonuses: pure HTML → list[dict], tested with a minimal fixture that mirrors
the travel-on-points.com table structure (4 cols: Point Program, Bonus Rate,
Airline / Hotel Program, End Date).

reconcile: snapshot-replace logic, tested with an in-memory DuckDB.
"""

from __future__ import annotations

from datetime import date

import duckdb
import pytest

from transfer_bonuses import parse_bonuses, reconcile

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
def mem_conn():
    """In-memory DuckDB seeded with the minimal schema and two transfer_partners rows."""
    conn = duckdb.connect(":memory:")
    conn.execute("SET TimeZone='UTC'")
    conn.execute("CREATE SEQUENCE IF NOT EXISTS transfer_bonuses_id_seq")
    conn.execute("""
        CREATE TABLE transfer_partners (
            bank_program_id  SMALLINT     NOT NULL,
            airline_code     VARCHAR(10)  NOT NULL,
            PRIMARY KEY (bank_program_id, airline_code)
        )
    """)
    conn.execute("""
        CREATE TABLE transfer_bonuses (
            id               BIGINT      PRIMARY KEY DEFAULT nextval('transfer_bonuses_id_seq'),
            bank_program_id  SMALLINT    NOT NULL,
            airline_code     VARCHAR(10) NOT NULL,
            bonus_pct        INTEGER     NOT NULL CHECK (bonus_pct > 0),
            starts_at        DATE        NOT NULL,
            ends_at          DATE        NOT NULL,
            notes            VARCHAR,
            created_at_utc   TIMESTAMP   NOT NULL DEFAULT now(),
            updated_at_utc   TIMESTAMP   NOT NULL DEFAULT now(),
            CHECK (ends_at >= starts_at),
            UNIQUE (bank_program_id, airline_code, starts_at)
        )
    """)
    # Two tracked airlines: AS (Alaska) and AF (Air France)
    conn.execute("INSERT INTO transfer_partners (bank_program_id, airline_code) VALUES (5, 'AS')")
    conn.execute("INSERT INTO transfer_partners (bank_program_id, airline_code) VALUES (2, 'AF')")
    return conn


def test_reconcile_replaces_existing(mem_conn):
    """Existing bonus is deleted; fresh record is inserted."""
    mem_conn.execute(
        "INSERT INTO transfer_bonuses "
        "(bank_program_id, airline_code, bonus_pct, starts_at, ends_at) "
        "VALUES (5, 'AS', 30, '2026-01-01', '2026-01-31')"
    )
    assert mem_conn.execute("SELECT COUNT(*) FROM transfer_bonuses").fetchone()[0] == 1

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
    deleted, inserted = reconcile(mem_conn, fresh)
    assert deleted == 1
    assert inserted == 1
    rows = mem_conn.execute(
        "SELECT bank_program_id, airline_code, bonus_pct FROM transfer_bonuses"
    ).fetchall()
    assert rows == [(2, "AF", 25)]


def test_reconcile_zero_bonuses_clears_table(mem_conn):
    """Empty records list → DELETE fires, nothing inserted. Valid (no active bonuses)."""
    mem_conn.execute(
        "INSERT INTO transfer_bonuses "
        "(bank_program_id, airline_code, bonus_pct, starts_at, ends_at) "
        "VALUES (5, 'AS', 30, '2026-01-01', '2026-01-31')"
    )
    deleted, inserted = reconcile(mem_conn, [])
    assert deleted == 1
    assert inserted == 0
    assert mem_conn.execute("SELECT COUNT(*) FROM transfer_bonuses").fetchone()[0] == 0


def test_reconcile_dry_run_leaves_table_unchanged(mem_conn):
    """--dry-run: no DB changes, returns (0, 0)."""
    mem_conn.execute(
        "INSERT INTO transfer_bonuses "
        "(bank_program_id, airline_code, bonus_pct, starts_at, ends_at) "
        "VALUES (5, 'AS', 30, '2026-01-01', '2026-01-31')"
    )
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
    deleted, inserted = reconcile(mem_conn, fresh, dry_run=True)
    assert (deleted, inserted) == (0, 0)
    # Table unchanged — stale AS bonus still present
    assert mem_conn.execute("SELECT COUNT(*) FROM transfer_bonuses").fetchone()[0] == 1
