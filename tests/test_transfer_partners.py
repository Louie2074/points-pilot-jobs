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
