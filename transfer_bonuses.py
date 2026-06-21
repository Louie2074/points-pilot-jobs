#!/usr/bin/env python3
"""
transfer_bonuses — scrape travel-on-points.com for current transfer bonuses.

Snapshot-replaces the `pp.transfer_bonuses` table in Postgres for all airlines
tracked in `transfer_partners` (atomic — one transaction). Runs on GitHub Actions
cron (twice monthly) or on-demand via workflow_dispatch.

Fail-closed: HTTP non-2xx or parse error → raises → non-zero exit → workflow
failure notification. Zero bonuses is valid — deletes all tracked bonuses and
inserts nothing (no active bonuses right now).

Requires DATABASE_URL (Postgres). BETTERSTACK_SOURCE_TOKEN enables metrics/log shipping.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import re
import subprocess
import time
from datetime import date, datetime

import nodriver as uc
from bs4 import BeautifulSoup
from sqlalchemy import Connection, text

from obs import flush, install_log_shipping, ping_heartbeat, ship_metric
from pp_db import queries as pq

logger = logging.getLogger("transfer_bonuses")

# Optional Better Stack heartbeat — a missed run then raises an alert.
# No-op unless BONUSES_HEARTBEAT_URL is set.
BONUSES_HEARTBEAT_URL = os.getenv("BONUSES_HEARTBEAT_URL", "")



SOURCE_URL = "https://travel-on-points.com/current-point-transfer-bonuses/"

# Site's "Transfer From" cell text → bank_programs.id in MotherDuck.
# Keys are lowercased for case-insensitive lookup.
BANK_MAP: dict[str, int] = {
    "american express": 2,
    "amex": 2,
    "amex membership rewards": 2,
    "chase": 1,
    "chase ultimate rewards": 1,
    "capital one": 3,
    "capital one miles": 3,
    "citi": 4,
    "citi thankyou": 4,
    "citi thankyou points": 4,
    "citi thankyou rewards": 4,
    "bilt": 5,
    "bilt rewards": 5,
    "marriott bonvoy": 6,
    "marriott": 6,
    "wells fargo": 7,
    "wells fargo rewards": 7,
}

# Airline/hotel destination text (extracted from the details sentence) →
# transfer_partners.airline_code. Hotel programs (Marriott Bonvoy, Wyndham, etc.)
# are absent from this map — rows that don't match are silently skipped.
AIRLINE_MAP: dict[str, str] = {
    "air canada aeroplan": "AC",
    "aeroplan": "AC",
    "air france/klm flying blue": "AF",
    "air france klm flying blue": "AF",  # frequentmiler omits the slash
    "air france": "AF",
    "flying blue": "AF",
    "alaska airlines": "AS",
    "alaska": "AS",
    "mileage plan": "AS",
    "american airlines": "AA",
    "avianca lifemiles": "AV",
    "lifemiles": "AV",
    "jetblue": "B6",
    "jetblue trueblue": "B6",
    "trueblue": "B6",
    "british airways": "BA",
    "british airways executive club": "BA",
    "cathay pacific asia miles": "CX",
    "cathay pacific": "CX",
    "asia miles": "CX",
    "delta skymiles": "DL",
    "delta": "DL",
    "aer lingus": "EI",
    "etihad": "EY",
    "hawaiian": "HA",
    "iberia": "IB",
    "ana": "NH",
    "all nippon airways": "NH",
    "qatar airways": "QR",
    "qatar privilege club avios": "QR",  # frequentmiler uses this name
    "singapore airlines krisflyer": "SQ",
    "singapore airlines": "SQ",
    "krisflyer": "SQ",
    "turkish airlines miles&smiles": "TK",
    "turkish airlines miles & smiles": "TK",  # frequentmiler uses spaces around &
    "turkish airlines": "TK",
    "united mileageplus": "UA",
    "united": "UA",
    "mileageplus": "UA",
    "virgin atlantic flying club": "VS",
    "virgin atlantic": "VS",
    "southwest rapid rewards": "WN",
    "southwest": "WN",
}


def parse_bonuses(html: str, today: date | None = None) -> list[dict]:
    """Parse the first <table> on the page into a list of bonus records.

    Page columns (travel-on-points.com):
        col 0 — Point Program   (bank/program name)
        col 1 — Bonus Rate      ("25%")
        col 2 — Airline / Hotel Program
        col 3 — End Date        ("6/30/26")

    Each returned record is a dict with keys:
        bank_program_id (int), airline_code (str), bonus_pct (int),
        starts_at (date), ends_at (date), notes (str | None)

    Rows whose bank or airline destination is not in the respective map are
    silently skipped (hotel programs, unknown bank programs). Raises ValueError
    if no <table> is found — the page structure changed.
    """
    if today is None:
        today = date.today()

    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table")
    if table is None:
        raise ValueError("No <table> found on the page — page structure may have changed")

    records: list[dict] = []
    rows = table.find_all("tr")
    for row in rows[1:]:  # rows[0] is the header
        cells = [td.get_text(strip=True) for td in row.find_all(["td", "th"])]
        if len(cells) < 4:
            continue
        bank_raw, bonus_raw, airline_raw, end_date_raw = (cells[0], cells[1], cells[2], cells[3])

        # Bank lookup
        bank_id = BANK_MAP.get(bank_raw.lower().strip())
        if bank_id is None:
            logger.debug("Skipping unknown bank %r", bank_raw)
            continue

        # Airline lookup — strip trailing asterisks/footnote markers first
        airline_clean = re.sub(r"[*†‡§]+$", "", airline_raw).strip()
        airline_code = AIRLINE_MAP.get(airline_clean.lower())
        if airline_code is None:
            logger.debug("Skipping non-airline destination %r", airline_raw)
            continue

        # Bonus pct — "25%" → 25
        try:
            bonus_pct = int(bonus_raw.strip().rstrip("%"))
        except ValueError:
            logger.warning("Unexpected bonus_rate %r — skipping row", bonus_raw)
            continue

        # End date — "6/30/26" → date(2026, 6, 30)
        try:
            ends_at = datetime.strptime(end_date_raw.strip(), "%m/%d/%y").date()
        except ValueError:
            logger.warning("Unexpected end_date %r — skipping row", end_date_raw)
            continue

        # Store original cell text in notes if it was altered (e.g. trailing *)
        notes: str | None = airline_raw if airline_raw != airline_clean else None

        records.append(
            {
                "bank_program_id": bank_id,
                "airline_code": airline_code,
                "bonus_pct": bonus_pct,
                "starts_at": today,
                "ends_at": ends_at,
                "notes": notes,
            }
        )

    return records


def reconcile(
    conn: Connection,
    records: list[dict],
    dry_run: bool = False,
) -> tuple[int, int]:
    """Snapshot-replace ``pp.transfer_bonuses`` for all airlines tracked in transfer_partners.

    Deletes every row whose airline_code appears in transfer_partners, then
    inserts the freshly-scraped records. Returns (rows_deleted, rows_inserted).

    In dry_run mode: no DELETE/INSERT — returns (0, 0) and logs what would happen.

    ``conn`` is a SQLAlchemy Connection inside a single transaction (opened by ``main()`` via
    ``get_engine().begin()``), so the DELETE + re-INSERT commit atomically — readers never see an
    empty/partial table, and a mid-insert failure rolls the whole snapshot back.
    """
    if dry_run:
        count = conn.execute(
            text(
                "SELECT COUNT(*) FROM pp.transfer_bonuses "
                "WHERE airline_code IN (SELECT DISTINCT airline_code FROM pp.transfer_partners)"
            )
        ).scalar()
        logger.info(
            "[dry-run] Would delete %d row(s) and insert %d row(s).",
            count,
            len(records),
        )
        return 0, 0

    deleted = conn.execute(
        text(
            "SELECT COUNT(*) FROM pp.transfer_bonuses "
            "WHERE airline_code IN (SELECT DISTINCT airline_code FROM pp.transfer_partners)"
        )
    ).scalar()
    conn.execute(
        text(
            "DELETE FROM pp.transfer_bonuses "
            "WHERE airline_code IN (SELECT DISTINCT airline_code FROM pp.transfer_partners)"
        )
    )

    inserted = 0
    for r in records:
        pq.upsert_transfer_bonus(
            conn,
            bank_program_id=r["bank_program_id"],
            airline_code=r["airline_code"],
            bonus_pct=r["bonus_pct"],
            starts_at=r["starts_at"],
            ends_at=r["ends_at"],
            notes=r["notes"],
        )
    inserted = len(records)

    logger.info("Deleted %d row(s), inserted %d row(s).", deleted, inserted)
    return deleted, inserted


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
    """Fetch *url* using a headless Chrome CDP session (WAF bypass).

    Launches Chrome on a fixed debug port, connects via nodriver (pure CDP —
    no WebDriver protocol, so navigator.webdriver is genuinely absent), waits
    for JS rendering, then returns the page HTML.
    """
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
            "--user-data-dir=/tmp/tb-scrape-profile",
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
    """Fetch the bonuses page via headless Chrome (nodriver) to bypass WAF."""
    return asyncio.run(_fetch_with_nodriver(url))


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
    install_log_shipping("point-pilot-jobs")

    started = time.monotonic()
    deleted = inserted = 0
    ok = False
    try:
        html = fetch_page()
        records = parse_bonuses(html)
        logger.info("Parsed %d matching bonus row(s).", len(records))

        from pp_db.engine import get_engine

        with get_engine().begin() as conn:
            deleted, inserted = reconcile(conn, records, dry_run=args.dry_run)
        ok = True
        return 0
    except Exception:
        logger.exception("transfer_bonuses failed")
        return 1
    finally:
        ship_metric(
            {
                "event": "transfer_bonuses_run",
                "service": "point-pilot-jobs",
                "job": "transfer_bonuses",
                "ok": ok,
                "deleted": deleted,
                "inserted": inserted,
                "dry_run": args.dry_run,
                "duration_s": round(time.monotonic() - started, 3),
            }
        )
        flush()
        # Heartbeat only on a successful real run (dry-runs are manual).
        if ok and not args.dry_run:
            ping_heartbeat(BONUSES_HEARTBEAT_URL, logger)


if __name__ == "__main__":
    raise SystemExit(main())
