#!/usr/bin/env python3
"""
transfer_partners — scrape thriftytraveler.com for bank→airline transfer
partners and ratios, and snapshot-replace the `transfer_partners` table.

Sole owner of `transfer_partners` in MotherDuck. Full-table snapshot: delete all,
insert the freshly-scraped rows for the managed banks. Runs on a GitHub Actions
cron (twice monthly) or on-demand via workflow_dispatch.

Coverage is gated to airlines already tracked (AIRLINE_MAP). Hotel rows and
unmapped airlines are skipped + logged. Marriott (id 6) and Rove are skipped.

Fail-closed: HTTP non-2xx or "no managed bank tables found at all" raises → non-zero
exit → workflow failure. A bank section that maps to zero rows just contributes
nothing (pure snapshot).

Requires MOTHERDUCK_TOKEN. BETTERSTACK_SOURCE_TOKEN enables metrics/log shipping.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import re
import subprocess
import time
import urllib.request
from decimal import ROUND_HALF_UP, Decimal

import duckdb
import nodriver as uc
from bs4 import BeautifulSoup

from obs import flush, install_log_shipping, ship_metric

logger = logging.getLogger("transfer_partners")

# Optional Better Stack heartbeat — a missed run then raises an alert. No-op unless set.
PARTNERS_HEARTBEAT_URL = os.getenv("TRANSFER_PARTNERS_HEARTBEAT_URL", "")

SOURCE_URL = "https://thriftytraveler.com/guides/points/credit-card-transfer-partners/"

# Site section heading marker (lowercased substring) → bank_programs.id.
# Rove + Marriott deliberately absent — their sections are skipped.
BANK_SECTIONS: list[tuple[str, int]] = [
    ("chase", 1),
    ("american express", 2),
    ("capital one", 3),
    ("citi", 4),
    ("bilt", 5),
    ("wells fargo", 7),
]

# Normalized site "Program" cell → (airline_code, canonical program_name).
# Gated to the already-tracked IATA set. program_name values match the prior
# hardcoded banks.py. Unmapped names are skipped (logged).
AIRLINE_MAP: dict[str, tuple[str, str]] = {
    "american airlines": ("AA", "AAdvantage"),
    "air canada": ("AC", "Aeroplan"),
    "air canada aeroplan": ("AC", "Aeroplan"),
    "aeroplan": ("AC", "Aeroplan"),
    "air france": ("AF", "Flying Blue"),
    "air france/klm": ("AF", "Flying Blue"),
    "air france klm": ("AF", "Flying Blue"),
    "flying blue": ("AF", "Flying Blue"),
    "alaska": ("AS", "Mileage Plan"),
    "alaska airlines": ("AS", "Mileage Plan"),
    "mileage plan": ("AS", "Mileage Plan"),
    "avianca": ("AV", "LifeMiles"),
    "avianca lifemiles": ("AV", "LifeMiles"),
    "lifemiles": ("AV", "LifeMiles"),
    "jetblue": ("B6", "TrueBlue"),
    "jetblue trueblue": ("B6", "TrueBlue"),
    "trueblue": ("B6", "TrueBlue"),
    "british airways": ("BA", "British Airways Avios"),
    "cathay pacific": ("CX", "Asia Miles"),
    "asia miles": ("CX", "Asia Miles"),
    "delta": ("DL", "SkyMiles"),
    "delta air lines": ("DL", "SkyMiles"),
    "aer lingus": ("EI", "Aer Lingus AerClub"),
    "etihad": ("EY", "Etihad Guest"),
    "etihad airways": ("EY", "Etihad Guest"),
    "hawaiian": ("HA", "HawaiianMiles"),
    "hawaiian airlines": ("HA", "HawaiianMiles"),
    "iberia": ("IB", "Iberia Plus"),
    "ana": ("NH", "ANA Mileage Club"),
    "all nippon airways": ("NH", "ANA Mileage Club"),
    "qatar airways": ("QR", "Privilege Club"),
    "qatar": ("QR", "Privilege Club"),
    "singapore air": ("SQ", "KrisFlyer"),
    "singapore airlines": ("SQ", "KrisFlyer"),
    "krisflyer": ("SQ", "KrisFlyer"),
    "turkish airlines": ("TK", "Miles&Smiles"),
    "turkish": ("TK", "Miles&Smiles"),
    "united": ("UA", "MileagePlus"),
    "united airlines": ("UA", "MileagePlus"),
    "mileageplus": ("UA", "MileagePlus"),
    "virgin atlantic": ("VS", "Virgin Atlantic"),
    "southwest": ("WN", "Rapid Rewards"),
    "southwest airlines": ("WN", "Rapid Rewards"),
    "rapid rewards": ("WN", "Rapid Rewards"),
}

MIN_TRANSFER = 1000
TRANSFER_INCREMENT = 1000

# Sane band for a bank-points-per-mile ratio. Outside → treat as parse garbage.
_RATIO_MIN = 0.1
_RATIO_MAX = 10.0


def _parse_ratio(raw: str) -> float | None:
    """Parse a site ratio cell ("bank : partner") into internal transfer_ratio
    (bank points per 1 partner mile = left / right), rounded to 2 dp.

    Returns None for anything unparseable, non-positive, or outside the sane band
    (_RATIO_MIN.._RATIO_MAX) — caller drops the row and logs a WARNING.
    """
    if ":" not in raw:
        return None
    left_s, _, right_s = raw.partition(":")
    try:
        left = float(left_s.replace(",", "").strip())
        right = float(right_s.replace(",", "").strip())
    except ValueError:
        return None
    if left <= 0 or right <= 0:
        return None
    ratio = float(Decimal(str(left / right)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    if ratio < _RATIO_MIN or ratio > _RATIO_MAX:
        return None
    return ratio


def parse_partners(html: str) -> tuple[list[dict], dict]:
    raise NotImplementedError


def reconcile(conn, records: list[dict], dry_run: bool = False) -> tuple[int, int]:
    raise NotImplementedError
