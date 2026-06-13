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


def _find_bank_table(soup: BeautifulSoup, marker: str):
    """Return the first <table> following a heading whose text contains `marker`
    (case-insensitive). None if no such heading/table is found."""
    for heading in soup.find_all(re.compile(r"^h[1-4]$")):
        if marker in heading.get_text(strip=True).lower():
            table = heading.find_next("table")
            if table is not None:
                return table
    return None


def parse_partners(html: str) -> tuple[list[dict], dict]:
    """Parse each managed bank's transfer-partner table into records.

    Returns (records, stats). Each record: {bank_program_id, airline_code,
    program_name, transfer_ratio, min_transfer, transfer_increment}. `stats` holds
    the aggregate debugging breakdown shipped in the run metric: banks_found,
    banks_missing, airline_rows_seen, rows_skipped_hotel, rows_skipped_unmapped,
    rows_ratio_dropped.

    Skips Hotel-type rows, unmapped/untracked airlines, and rows with an
    unparseable ratio (logged). Raises ValueError if NO managed bank table is
    found at all (the page structure changed).
    """
    soup = BeautifulSoup(html, "html.parser")
    records: list[dict] = []
    banks_found = 0
    tot_airline = tot_hotel = tot_unmapped = tot_ratio_dropped = 0

    for marker, bank_id in BANK_SECTIONS:
        table = _find_bank_table(soup, marker)
        if table is None:
            logger.warning("bank=%s id=%d: table_found=False", marker, bank_id)
            continue
        banks_found += 1

        rows = table.find_all("tr")
        rows_total = airline_rows = mapped = skipped_hotel = 0
        skipped_unmapped = ratio_dropped = 0
        unmapped_names: list[str] = []

        for row in rows[1:]:  # rows[0] is the header
            cells = [td.get_text(strip=True) for td in row.find_all(["td", "th"])]
            if len(cells) < 3:
                continue
            rows_total += 1
            program_raw, type_raw, ratio_raw = cells[0], cells[1], cells[2]

            if type_raw.strip().lower() != "airline":
                skipped_hotel += 1
                continue
            airline_rows += 1

            mapped_airline = AIRLINE_MAP.get(program_raw.lower().strip())
            if mapped_airline is None:
                skipped_unmapped += 1
                unmapped_names.append(program_raw)
                continue
            airline_code, program_name = mapped_airline

            ratio = _parse_ratio(ratio_raw)
            if ratio is None:
                ratio_dropped += 1
                logger.warning(
                    "bank=%s program=%r: unparseable ratio %r — dropped",
                    marker, program_raw, ratio_raw,
                )
                continue

            records.append(
                {
                    "bank_program_id": bank_id,
                    "airline_code": airline_code,
                    "program_name": program_name,
                    "transfer_ratio": ratio,
                    "min_transfer": MIN_TRANSFER,
                    "transfer_increment": TRANSFER_INCREMENT,
                }
            )
            mapped += 1

        logger.info(
            "bank=%s id=%d table_found=True rows_total=%d airline_rows=%d "
            "mapped=%d skipped_hotel=%d skipped_unmapped=%d ratio_dropped=%d",
            marker, bank_id, rows_total, airline_rows, mapped,
            skipped_hotel, skipped_unmapped, ratio_dropped,
        )
        if unmapped_names:
            logger.info("bank=%s unmapped airline programs: %s", marker, unmapped_names)

        tot_airline += airline_rows
        tot_hotel += skipped_hotel
        tot_unmapped += skipped_unmapped
        tot_ratio_dropped += ratio_dropped

    if banks_found == 0:
        raise ValueError("no managed bank tables found — page structure may have changed")

    stats = {
        "banks_found": banks_found,
        "banks_missing": len(BANK_SECTIONS) - banks_found,
        "airline_rows_seen": tot_airline,
        "rows_skipped_hotel": tot_hotel,
        "rows_skipped_unmapped": tot_unmapped,
        "rows_ratio_dropped": tot_ratio_dropped,
    }
    return records, stats


def reconcile(conn, records: list[dict], dry_run: bool = False) -> tuple[int, int]:
    raise NotImplementedError
