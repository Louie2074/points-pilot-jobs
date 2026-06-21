"""Unit tests for transfer_partners.py.

parse_partners: pure HTML → list[dict], tested with a fixture mirroring the
thriftytraveler.com structure (per-bank heading + table: Program | Type |
Transfer Ratio | Transfer Time). No network/DB — always runs.

reconcile: full-table snapshot-replace, now against the real ``pp`` Postgres container (the
MotherDuck→Supabase cutover ported ``reconcile`` from DuckDB to a SQLAlchemy Connection on
``pp.transfer_partners``). Those tests skip if ``DATABASE_URL`` is unset and run inside a
rolled-back transaction so they leave the table untouched.
"""

from __future__ import annotations

import os

import pytest

from transfer_partners import (
    _match_airline,
    _parse_ratio,
    parse_partners,
    reconcile,
)

_NEEDS_PG = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL unset — reconcile test needs a live pp schema",
)

# ---------------------------------------------------------------------------
# _match_airline — distinctive whole-word keyword match against the site's
# varied, per-bank "Program" cell text. Names below are verbatim from the live
# thriftytraveler.com page.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "program,code,program_name",
    [
        # Tracked airlines under suffixed / per-bank-varying display names that an
        # exact-string lookup dropped (the bug that motivated keyword matching).
        ("Alaska Airlines Mileage Plan", "AS", "Mileage Plan"),  # Bilt→Alaska — key case
        ("United MileagePlus", "UA", "MileagePlus"),
        ("British Airways Avios", "BA", "British Airways Avios"),
        ("British Airways Executive Club", "BA", "British Airways Avios"),
        ("Air France/KLM Flying Blue", "AF", "Flying Blue"),
        ("Aer Lingus AerClub", "EI", "Aer Lingus AerClub"),
        ("Aer Lingus Avios", "EI", "Aer Lingus AerClub"),
        ("Cathay Pacific AsiaMiles", "CX", "Asia Miles"),
        ("Iberia Avios", "IB", "Iberia Plus"),
        ("Turkish Miles & Smiles", "TK", "Miles&Smiles"),
        ("Southwest Rapid Rewards", "WN", "Rapid Rewards"),
        ("Virgin Atlantic Flying Club", "VS", "Virgin Atlantic"),
        ("Singapore", "SQ", "KrisFlyer"),  # bare, no "Air"/"Airlines" suffix
        ("ANA Mileage Club", "NH", "ANA Mileage Club"),  # whole-word "ana"
    ],
)
def test_match_airline_tracked_variants(program, code, program_name):
    assert _match_airline(program) == (code, program_name)


@pytest.mark.parametrize(
    "program",
    [
        "Emirates",  # EK — untracked
        "Emirates Skywards",
        "Qantas",  # QF — must NOT match \\bana\\b
        "AeroMexico",  # AM — must NOT match "aeroplan"
        "EVA Air",
        "Finnair",
        "Thai Airways",  # must NOT match "british airways"
        "TAP Air Portugal",
        "Spirit",
        "Japan Airlines (JAL)",  # JL — untracked
        "Virgin Red",  # NOT Virgin Atlantic — must stay unmatched
    ],
)
def test_match_airline_untracked_returns_none(program):
    assert _match_airline(program) is None


def test_match_airline_ana_does_not_grab_avianca():
    """Avianca resolves to AV, never NH (the \\bana\\b keyword must not over-match)."""
    assert _match_airline("Avianca LifeMiles") == ("AV", "LifeMiles")
    assert _match_airline("Avianca") == ("AV", "LifeMiles")


# ---------------------------------------------------------------------------
# _parse_ratio — site writes "bank : partner"; internal ratio = bank / partner
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("1:1", 1.0),
        ("2:1.5", 1.33),  # Capital One → Emirates-style
        ("5:4", 1.25),  # Amex → Cathay-style
        ("1,000:800", 1.25),  # thousands separators
        ("1:1.6", 0.63),  # Amex → AeroMexico-style (bank cheaper than partner)
    ],
)
def test_parse_ratio_valid(raw, expected):
    assert _parse_ratio(raw) == expected


@pytest.mark.parametrize("raw", ["1:0", "0:1", "-1:1", "abc", "1", "100:1"])
def test_parse_ratio_rejected_returns_none(raw):
    # zero/negative/garbage/out-of-band (>10 or <0.1) → None (row dropped + warned)
    assert _parse_ratio(raw) is None


# ---------------------------------------------------------------------------
# HTML fixture — mirrors thriftytraveler.com: per-bank <h2> heading + <table>
# with columns Program | Type | Transfer Ratio | Transfer Time. Contains:
#   - Chase → Singapore Air (airline, 1:1) and World of Hyatt (HOTEL, skip)
#   - Amex → Cathay Pacific (airline, 5:4)
#   - Bilt → Alaska (airline, 1:1)
#   - Marriott section (must be ignored — not a managed bank)
#   - Rove section (must be ignored)
#   - Chase → Emirates (airline, 1:1) — EK not in TRACKED set, must be skipped
# ---------------------------------------------------------------------------
HTML_FIXTURE = """\
<html><body>
<h2>Chase Transfer Partners</h2>
<table>
<tr><th>Program</th><th>Type</th><th>Transfer Ratio</th><th>Transfer Time</th></tr>
<tr><td>Singapore Air</td><td>Airline</td><td>1:1</td><td>1-2 days</td></tr>
<tr><td>World of Hyatt</td><td>Hotel</td><td>1:1</td><td>Instant</td></tr>
<tr><td>Emirates</td><td>Airline</td><td>1:1</td><td>Instant</td></tr>
</table>
<h2>American Express Transfer Partners</h2>
<table>
<tr><th>Program</th><th>Type</th><th>Transfer Ratio</th><th>Transfer Time</th></tr>
<tr><td>Cathay Pacific</td><td>Airline</td><td>5:4</td><td>Instant</td></tr>
</table>
<h2>Bilt Transfer Partners</h2>
<table>
<tr><th>Program</th><th>Type</th><th>Transfer Ratio</th><th>Transfer Time</th></tr>
<tr><td>Alaska</td><td>Airline</td><td>1:1</td><td>Instant</td></tr>
</table>
<h2>Marriott Transfer Partners</h2>
<table>
<tr><th>Program</th><th>Type</th><th>Transfer Ratio</th><th>Transfer Time</th></tr>
<tr><td>Alaska</td><td>Airline</td><td>3:1</td><td>2 days</td></tr>
</table>
<h2>Rove Transfer Partners</h2>
<table>
<tr><th>Program</th><th>Type</th><th>Transfer Ratio</th><th>Transfer Time</th></tr>
<tr><td>United</td><td>Airline</td><td>1:1</td><td>Instant</td></tr>
</table>
</body></html>
"""


def _by_key(records):
    return {(r["bank_program_id"], r["airline_code"]): r for r in records}


def test_parse_maps_airline_rows_to_banks():
    """Chase→SQ, Amex→CX, Bilt→AS land under the right bank ids with right ratios."""
    records, _stats = parse_partners(HTML_FIXTURE)
    recs = _by_key(records)
    assert recs[(1, "SQ")]["transfer_ratio"] == 1.0
    assert recs[(1, "SQ")]["program_name"] == "KrisFlyer"
    assert recs[(2, "CX")]["transfer_ratio"] == 1.25  # 5:4
    assert recs[(5, "AS")]["transfer_ratio"] == 1.0
    # min/increment defaults attached
    assert recs[(5, "AS")]["min_transfer"] == 1000
    assert recs[(5, "AS")]["transfer_increment"] == 1000


def test_parse_skips_hotels_marriott_rove_and_untracked():
    """Hotels, the Marriott + Rove sections, and untracked airlines (Emirates) are dropped."""
    records, stats = parse_partners(HTML_FIXTURE)
    keys = {(r["bank_program_id"], r["airline_code"]) for r in records}
    # Exactly the three valid airline rows survive
    assert keys == {(1, "SQ"), (2, "CX"), (5, "AS")}
    # No bank id 6 (Marriott) and no airline 'EK'/'UA'-from-Rove leaked in
    assert all(r["bank_program_id"] != 6 for r in records)
    assert all(r["airline_code"] != "EK" for r in records)
    # Stats expose the debugging breakdown shipped in the metric.
    assert stats["banks_found"] == 3  # chase, amex, bilt (marriott/rove not managed)
    assert stats["banks_missing"] == 3  # citi, capital one, wells fargo absent in fixture
    assert stats["rows_skipped_hotel"] == 1  # World of Hyatt
    assert stats["rows_skipped_unmapped"] == 1  # Emirates (untracked)


def test_parse_no_managed_tables_raises():
    """A page with no managed bank tables → ValueError (structure changed)."""
    with pytest.raises(ValueError, match="no managed bank tables"):
        parse_partners("<html><body><p>nothing here</p></body></html>")


def test_parse_tableless_section_does_not_steal_next_table():
    """A managed bank heading with no table of its own must NOT grab the next
    section's table and misattribute its rows (section-bounded _find_bank_table)."""
    html = """\
<html><body>
<h2>Citi Transfer Partners</h2>
<h2>Bilt Transfer Partners</h2>
<table>
<tr><th>Program</th><th>Type</th><th>Transfer Ratio</th><th>Transfer Time</th></tr>
<tr><td>Alaska Airlines Mileage Plan</td><td>Airline</td><td>1:1</td><td>Instant</td></tr>
</table>
</body></html>
"""
    records, stats = parse_partners(html)
    keys = {(r["bank_program_id"], r["airline_code"]) for r in records}
    assert keys == {(5, "AS")}  # Bilt→Alaska only; Citi got nothing (no false (4, "AS"))
    assert stats["banks_found"] == 1


def test_parse_section_with_empty_intervening_heading():
    """An empty/unrelated heading between a bank heading and its table is skipped
    (the live page has an empty <h2> after 'American Express Transfer Partners')."""
    html = """\
<html><body>
<h2>American Express Transfer Partners</h2>
<h2></h2>
<table>
<tr><th>Program</th><th>Type</th><th>Transfer Ratio</th><th>Transfer Time</th></tr>
<tr><td>Cathay Pacific Asia Miles</td><td>Airline</td><td>5:4</td><td>Instant</td></tr>
</table>
</body></html>
"""
    records, _stats = parse_partners(html)
    keys = {(r["bank_program_id"], r["airline_code"]) for r in records}
    assert keys == {(2, "CX")}  # Amex→Cathay found despite the empty heading


@pytest.fixture()
def pg_conn():
    """A pp_db engine Connection inside a transaction, pre-seeded with two stale rows (incl. a
    Marriott id-6 row the snapshot must drop). Rolled back at teardown so the real
    ``pp.transfer_partners`` table is left untouched — mirrors production, where ``reconcile`` runs
    inside ``get_engine().begin()``."""
    from sqlalchemy import text

    from pp_db.engine import get_engine

    conn = get_engine().connect()
    trans = conn.begin()
    # Clear any rows so the seeded "stale" set is exactly what we assert on.
    conn.execute(text("DELETE FROM pp.transfer_partners"))
    conn.execute(
        text(
            "INSERT INTO pp.transfer_partners "
            "(bank_program_id, airline_code, program_name, transfer_ratio, min_transfer, "
            " transfer_increment) VALUES "
            "(6, 'AS', 'Mileage Plan', 3.0, 3000, 3000), "
            "(1, 'UA', 'MileagePlus', 1.0, 1000, 1000)"
        )
    )
    try:
        yield conn
    finally:
        trans.rollback()
        conn.close()


def _sample_records():
    return [
        {
            "bank_program_id": 1,
            "airline_code": "SQ",
            "program_name": "KrisFlyer",
            "transfer_ratio": 1.0,
            "min_transfer": 1000,
            "transfer_increment": 1000,
        },
        {
            "bank_program_id": 2,
            "airline_code": "CX",
            "program_name": "Asia Miles",
            "transfer_ratio": 1.25,
            "min_transfer": 1000,
            "transfer_increment": 1000,
        },
    ]


@_NEEDS_PG
def test_reconcile_full_snapshot_drops_marriott(pg_conn):
    """All prior rows (incl. Marriott id 6) deleted; only the new records remain."""
    from sqlalchemy import text

    deleted, inserted = reconcile(pg_conn, _sample_records())
    assert deleted == 2
    assert inserted == 2
    rows = pg_conn.execute(
        text(
            "SELECT bank_program_id, airline_code FROM pp.transfer_partners "
            "ORDER BY bank_program_id, airline_code"
        )
    ).fetchall()
    assert [tuple(r) for r in rows] == [(1, "SQ"), (2, "CX")]  # no id 6, no stale UA


@_NEEDS_PG
def test_reconcile_dry_run_leaves_table_unchanged(pg_conn):
    """--dry-run: returns (0, 0), no writes; stale rows still present."""
    from sqlalchemy import text

    deleted, inserted = reconcile(pg_conn, _sample_records(), dry_run=True)
    assert (deleted, inserted) == (0, 0)
    assert pg_conn.execute(text("SELECT COUNT(*) FROM pp.transfer_partners")).scalar() == 2
