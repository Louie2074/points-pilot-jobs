#!/usr/bin/env python3
"""
transfer_bonuses — scrape frequentmiler.com for current transfer bonuses.

Snapshot-replaces the `transfer_bonuses` table in MotherDuck for all airlines
tracked in `transfer_partners`. Runs on GitHub Actions cron (twice monthly) or
on-demand via workflow_dispatch.

Fail-closed: HTTP non-2xx or parse error → raises → non-zero exit → workflow
failure notification. Zero bonuses is valid — deletes all tracked bonuses and
inserts nothing (no active bonuses right now).

Requires MOTHERDUCK_TOKEN. BETTERSTACK_SOURCE_TOKEN enables metrics/log shipping.
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import time
from datetime import date, datetime

import duckdb
import httpx
from bs4 import BeautifulSoup

from obs import flush, install_log_shipping, ship_metric

logger = logging.getLogger("transfer_bonuses")

SOURCE_URL = "https://frequentmiler.com/current-point-transfer-bonuses/"

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
    "air france klm flying blue": "AF",   # frequentmiler omits the slash
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
    "qatar privilege club avios": "QR",   # frequentmiler uses this name
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

    Page columns (frequentmiler.com):
        col 0 — Transfer From  (bank/program name)
        col 1 — Transfer Bonus Details  (full sentence: "N% transfer bonus from X to Y")
        col 2 — Start Date  (Excel serial prefix + MM/DD/YY, e.g. "4617406/02/26")
        col 3 — End Date    (same format)

    Each returned record is a dict with keys:
        bank_program_id (int), airline_code (str), bonus_pct (int),
        starts_at (date), ends_at (date), notes (str | None)

    Rows whose bank or airline destination is not in the respective map are
    silently skipped (hotel programs, unknown bank programs). Raises ValueError
    if no <table> is found — the page structure changed.

    The `today` parameter is kept for API compatibility; it is used as a fallback
    starts_at only when the start-date cell cannot be parsed.
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
        bank_raw, details_raw, start_date_raw, end_date_raw = (
            cells[0], cells[1], cells[2], cells[3]
        )

        # Bank lookup
        bank_id = BANK_MAP.get(bank_raw.lower().strip())
        if bank_id is None:
            logger.debug("Skipping unknown bank %r", bank_raw)
            continue

        # Airline — extracted from the details sentence "N% transfer bonus from X to Airline"
        airline_m = re.search(r" to (.+)$", details_raw)
        if not airline_m:
            logger.warning("No airline in details %r — skipping row", details_raw[:80])
            continue
        airline_raw = airline_m.group(1).strip()
        airline_code = AIRLINE_MAP.get(airline_raw.lower())
        if airline_code is None:
            logger.debug("Skipping non-airline destination %r", airline_raw)
            continue

        # Bonus pct — "25% transfer bonus…" → 25
        pct_m = re.search(r"(\d+)%", details_raw)
        if not pct_m:
            logger.warning("No bonus pct in details %r — skipping row", details_raw[:80])
            continue
        bonus_pct = int(pct_m.group(1))

        # Dates — cells contain an Excel serial prefix followed by MM/DD/YY,
        # e.g. "4617406/02/26". Extract the human-readable suffix.
        start_m = re.search(r"(\d{1,2}/\d{2}/\d{2})$", start_date_raw)
        try:
            starts_at = (
                datetime.strptime(start_m.group(1), "%m/%d/%y").date()
                if start_m else today
            )
        except ValueError:
            logger.warning("Unexpected start_date %r — using today", start_date_raw)
            starts_at = today

        end_m = re.search(r"(\d{1,2}/\d{2}/\d{2})$", end_date_raw)
        try:
            ends_at = (
                datetime.strptime(end_m.group(1), "%m/%d/%y").date()
                if end_m else None
            )
        except ValueError:
            logger.warning("Unexpected end_date %r — skipping row", end_date_raw)
            continue
        if ends_at is None:
            logger.warning("Missing end_date in %r — skipping row", end_date_raw)
            continue

        records.append({
            "bank_program_id": bank_id,
            "airline_code": airline_code,
            "bonus_pct": bonus_pct,
            "starts_at": starts_at,
            "ends_at": ends_at,
            "notes": None,
        })

    return records


def reconcile(
    conn: duckdb.DuckDBPyConnection,
    records: list[dict],
    dry_run: bool = False,
) -> tuple[int, int]:
    """Snapshot-replace transfer_bonuses for all airlines tracked in transfer_partners.

    Deletes every row whose airline_code appears in transfer_partners, then
    inserts the freshly-scraped records. Returns (rows_deleted, rows_inserted).

    In dry_run mode: no DELETE/INSERT — returns (0, 0) and logs what would happen.
    """
    if dry_run:
        count = conn.execute(
            "SELECT COUNT(*) FROM transfer_bonuses "
            "WHERE airline_code IN (SELECT DISTINCT airline_code FROM transfer_partners)"
        ).fetchone()[0]
        logger.info(
            "[dry-run] Would delete %d row(s) and insert %d row(s).",
            count, len(records),
        )
        return 0, 0

    deleted = conn.execute(
        "DELETE FROM transfer_bonuses "
        "WHERE airline_code IN (SELECT DISTINCT airline_code FROM transfer_partners)"
    ).fetchone()[0]

    inserted = 0
    if records:
        conn.executemany(
            """
            INSERT INTO transfer_bonuses
                (bank_program_id, airline_code, bonus_pct, starts_at, ends_at, notes,
                 created_at_utc, updated_at_utc)
            VALUES (?, ?, ?, ?, ?, ?, now(), now())
            """,
            [
                (
                    r["bank_program_id"], r["airline_code"], r["bonus_pct"],
                    r["starts_at"], r["ends_at"], r["notes"],
                )
                for r in records
            ],
        )
        inserted = len(records)

    logger.info("Deleted %d row(s), inserted %d row(s).", deleted, inserted)
    return deleted, inserted


def fetch_page(url: str = SOURCE_URL, timeout: int = 15) -> str:
    """Fetch the bonuses page. Raises httpx.HTTPStatusError on non-2xx."""
    resp = httpx.get(
        url,
        headers={"User-Agent": "Mozilla/5.0 (compatible; PointPilot/1.0)"},
        timeout=timeout,
        follow_redirects=True,
    )
    resp.raise_for_status()
    return resp.text


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
    ok = False
    try:
        html = fetch_page()
        records = parse_bonuses(html)
        logger.info("Parsed %d matching bonus row(s).", len(records))

        conn = connect()
        deleted, inserted = reconcile(conn, records, dry_run=args.dry_run)
        ok = True
        return 0
    except Exception:
        logger.exception("transfer_bonuses failed")
        return 1
    finally:
        ship_metric({
            "event": "transfer_bonuses_run",
            "service": "points-pilot-jobs",
            "job": "transfer_bonuses",
            "ok": ok,
            "deleted": deleted,
            "inserted": inserted,
            "dry_run": args.dry_run,
            "duration_s": round(time.monotonic() - started, 3),
        })
        flush()


if __name__ == "__main__":
    raise SystemExit(main())
