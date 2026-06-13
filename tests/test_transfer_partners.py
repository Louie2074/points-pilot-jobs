"""Unit tests for transfer_partners.py — no network, no MotherDuck required.

parse_partners: pure HTML → list[dict], tested with a fixture mirroring the
thriftytraveler.com structure (per-bank heading + table: Program | Type |
Transfer Ratio | Transfer Time).

reconcile: full-table snapshot-replace, tested with an in-memory DuckDB.
"""

from __future__ import annotations

import duckdb
import pytest

from transfer_partners import _parse_ratio, parse_partners, reconcile


# ---------------------------------------------------------------------------
# _parse_ratio — site writes "bank : partner"; internal ratio = bank / partner
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("1:1", 1.0),
        ("2:1.5", 1.33),       # Capital One → Emirates-style
        ("5:4", 1.25),         # Amex → Cathay-style
        ("1,000:800", 1.25),   # thousands separators
        ("1:1.6", 0.63),       # Amex → AeroMexico-style (bank cheaper than partner)
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
