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


def reconcile(
    conn: duckdb.DuckDBPyConnection,
    records: list[dict],
    dry_run: bool = False,
) -> tuple[int, int]:
    """Full-table snapshot-replace of transfer_partners (this job is the sole owner).

    Deletes EVERY row, then inserts the freshly-scraped records. Returns
    (rows_deleted, rows_inserted). dry_run → no writes, returns (0, 0).
    """
    if dry_run:
        count = conn.execute("SELECT COUNT(*) FROM transfer_partners").fetchone()[0]
        logger.info(
            "[dry-run] Would delete %d row(s) and insert %d row(s).", count, len(records)
        )
        return 0, 0

    deleted = conn.execute("DELETE FROM transfer_partners").fetchone()[0]

    inserted = 0
    if records:
        conn.executemany(
            """
            INSERT INTO transfer_partners
                (bank_program_id, airline_code, program_name,
                 transfer_ratio, min_transfer, transfer_increment)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    r["bank_program_id"],
                    r["airline_code"],
                    r["program_name"],
                    r["transfer_ratio"],
                    r["min_transfer"],
                    r["transfer_increment"],
                )
                for r in records
            ],
        )
        inserted = len(records)

    logger.info("Deleted %d row(s), inserted %d row(s).", deleted, inserted)
    return deleted, inserted


def _ping_heartbeat() -> None:
    if not PARTNERS_HEARTBEAT_URL:
        return
    try:
        urllib.request.urlopen(PARTNERS_HEARTBEAT_URL, timeout=10).close()
    except Exception as exc:  # noqa: BLE001 — monitoring must never break the run
        logger.warning("heartbeat ping failed: %s", exc)


def _find_chrome() -> str:
    """Return path to Chrome/Chromium binary, searching common locations."""
    import shutil

    candidates = [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",  # macOS
        "/usr/bin/google-chrome-stable",  # GHA ubuntu-latest after setup-chrome
        "/usr/bin/google-chrome",
        "/usr/bin/chromium-browser",
        "/usr/bin/chromium",
    ]
    for path in candidates:
        if os.path.isfile(path):
            return path
    for name in ("google-chrome", "google-chrome-stable", "chromium-browser", "chromium"):
        found = shutil.which(name)
        if found:
            return found
    raise RuntimeError(
        "Chrome/Chromium not found. Install Google Chrome or set up browser-actions/setup-chrome."
    )


async def _fetch_with_nodriver(url: str, wait_secs: int = 5) -> str:
    """Fetch *url* using a headless Chrome CDP session (WAF bypass)."""
    port = 9222
    chrome_bin = _find_chrome()
    proc = subprocess.Popen(
        [
            chrome_bin,
            "--headless=new",
            "--no-sandbox",
            "--disable-gpu",
            "--disable-dev-shm-usage",
            f"--remote-debugging-port={port}",
            "--remote-debugging-host=127.0.0.1",
            "--user-data-dir=/tmp/tp-scrape-profile",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(3)  # wait for Chrome to bind the debug port
    try:
        browser = await uc.start(host="127.0.0.1", port=port)
        page = await browser.get(url)
        await asyncio.sleep(wait_secs)  # allow JS/redirect to settle
        html = await page.get_content()
        browser.stop()  # sync method — no await
        return html
    finally:
        proc.terminate()


def fetch_page(url: str = SOURCE_URL) -> str:
    """Fetch the transfer-partners page via headless Chrome (nodriver)."""
    return asyncio.run(_fetch_with_nodriver(url))


def connect() -> duckdb.DuckDBPyConnection:
    """Open a UTC-pinned MotherDuck connection to the point_pilot database."""
    if not os.environ.get("MOTHERDUCK_TOKEN"):
        raise RuntimeError("MOTHERDUCK_TOKEN is not set — cannot connect to MotherDuck.")
    conn = duckdb.connect("md:point_pilot")
    conn.execute("SET TimeZone='UTC'")
    return conn


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch + parse, but skip DELETE/INSERT. Reports what would change.",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        force=True,
    )
    install_log_shipping("points-pilot-jobs")

    started = time.monotonic()
    deleted = inserted = 0
    stats: dict = {}
    ok = False
    try:
        html = fetch_page()
        records, stats = parse_partners(html)
        logger.info(
            "Parsed %d transfer-partner row(s) across %d bank(s) (banks_missing=%d).",
            len(records), stats["banks_found"], stats["banks_missing"],
        )

        conn = connect()
        deleted, inserted = reconcile(conn, records, dry_run=args.dry_run)
        ok = True
        return 0
    except Exception:
        logger.exception("transfer_partners failed")
        return 1
    finally:
        ship_metric(
            {
                "event": "transfer_partners_run",
                "service": "points-pilot-jobs",
                "job": "transfer_partners",
                "ok": ok,
                "deleted": deleted,
                "inserted": inserted,
                "dry_run": args.dry_run,
                "duration_s": round(time.monotonic() - started, 3),
                # Debugging breakdown (empty {} if the fetch/parse failed before stats).
                **stats,
            }
        )
        flush()
        # Heartbeat only on a successful real run (dry-runs are manual).
        if ok and not args.dry_run:
            _ping_heartbeat()


if __name__ == "__main__":
    raise SystemExit(main())
