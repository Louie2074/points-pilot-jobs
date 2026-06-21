#!/usr/bin/env python3
"""
transfer_partners — scrape thriftytraveler.com for bank→airline transfer
partners and ratios, and snapshot-replace the `transfer_partners` table.

Sole owner of `pp.transfer_partners` in Postgres. Full-table snapshot: delete all,
insert the freshly-scraped rows for the managed banks (atomic — one transaction).
Runs on a GitHub Actions cron (twice monthly) or on-demand via workflow_dispatch.

Coverage is gated to airlines already tracked (AIRLINE_MAP). Hotel rows and
unmapped airlines are skipped + logged. Marriott (id 6) and Rove are skipped.

Fail-closed: HTTP non-2xx or "no managed bank tables found at all" raises → non-zero
exit → workflow failure. A bank section that maps to zero rows just contributes
nothing (pure snapshot).

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
from decimal import ROUND_HALF_UP, Decimal

import nodriver as uc
from bs4 import BeautifulSoup
from sqlalchemy import Connection, text

from obs import flush, install_log_shipping, ping_heartbeat, ship_metric
from pp_db import queries as pq

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

# Airline matchers — gated to the already-tracked IATA set. The site appends and
# varies program suffixes PER BANK ("Alaska Airlines Mileage Plan", "British
# Airways Avios" vs "British Airways Executive Club", bare "Singapore", "United
# MileagePlus"…), so an exact-string lookup silently drops tracked airlines.
# Instead each airline carries a list of DISTINCTIVE keywords matched as whole
# words (\bkeyword\b) against the lowercased "Program" cell — robust to suffix
# drift. Keywords are collision-checked against every untracked partner the page
# lists (Emirates, Qantas, Aeromexico, EVA Air, Finnair, Thai, TAP, Spirit, Japan
# Airlines, Virgin Red…) so none of them match a tracked airline. Unmatched rows
# are skipped + logged. program_name values match the prior hardcoded banks.py.
#
# Notes on a few deliberate keyword choices:
#   BA  → "british airways" only (NOT "avios" — shared by BA/Iberia/Aer Lingus/Qatar).
#   VS  → "virgin atlantic" only (NOT bare "virgin" — would wrongly grab "Virgin Red").
#   NH  → whole-word "ana" via \b…\b (matches "ANA Mileage Club", not "Avianca"/"Qantas").
#   AF  → "flying blue" (NOT "flying" — "Virgin Atlantic Flying Club" must stay VS).
AIRLINE_MATCHERS: list[tuple[str, str, list[str]]] = [
    ("AA", "AAdvantage", ["aadvantage", "american airlines"]),
    ("AC", "Aeroplan", ["aeroplan", "air canada"]),
    ("AF", "Flying Blue", ["flying blue", "air france"]),
    ("AS", "Mileage Plan", ["alaska"]),
    ("AV", "LifeMiles", ["avianca", "lifemiles"]),
    ("B6", "TrueBlue", ["jetblue", "trueblue"]),
    ("BA", "British Airways Avios", ["british airways"]),
    ("CX", "Asia Miles", ["cathay"]),
    ("DL", "SkyMiles", ["delta", "skymiles"]),
    ("EI", "Aer Lingus AerClub", ["aer lingus"]),
    ("EY", "Etihad Guest", ["etihad"]),
    ("HA", "HawaiianMiles", ["hawaiian"]),
    ("IB", "Iberia Plus", ["iberia"]),
    ("NH", "ANA Mileage Club", ["all nippon", "ana"]),
    ("QR", "Privilege Club", ["qatar", "privilege club"]),
    ("SQ", "KrisFlyer", ["singapore", "krisflyer"]),
    ("TK", "Miles&Smiles", ["turkish"]),
    ("UA", "MileagePlus", ["mileageplus", "united"]),
    ("VS", "Virgin Atlantic", ["virgin atlantic"]),
    ("WN", "Rapid Rewards", ["southwest", "rapid rewards"]),
]

# Precompile one whole-word alternation regex per airline.
_AIRLINE_PATTERNS: list[tuple[str, str, re.Pattern]] = [
    (code, name, re.compile(r"\b(?:" + "|".join(re.escape(k) for k in kws) + r")\b"))
    for code, name, kws in AIRLINE_MATCHERS
]


def _match_airline(program_raw: str) -> tuple[str, str] | None:
    """Map a site "Program" cell to (airline_code, canonical program_name), or
    None if it isn't one of the tracked airlines. Whole-word keyword match against
    the lowercased cell; first matcher wins (keywords are mutually exclusive)."""
    text = program_raw.lower()
    for code, name, pattern in _AIRLINE_PATTERNS:
        if pattern.search(text):
            return code, name
    return None


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


_HEADING_RE = re.compile(r"^h[1-4]$")


def _find_bank_table(soup: BeautifulSoup, marker: str):
    """Return this bank section's own <table>, or None.

    Walks forward from the heading whose text contains `marker` and returns the
    first <table> — but STOPS at the next bank-section heading (one whose text
    contains "transfer partners"), returning None if that boundary is reached
    first. This prevents a section that renders without its own table from
    silently stealing the *next* section's table and misattributing its rows to
    the wrong bank. Empty/unrelated headings (the page has an empty <h2> after the
    Amex heading, plus "How to Earn …" subheadings) are skipped — only the
    "* Transfer Partners" headings delimit sections.
    """
    for heading in soup.find_all(_HEADING_RE):
        if marker not in heading.get_text(strip=True).lower():
            continue
        for el in heading.find_all_next():
            if el.name == "table":
                return el
            if (
                el.name
                and _HEADING_RE.fullmatch(el.name)
                and ("transfer partners" in el.get_text(strip=True).lower())
            ):
                return None  # next section reached before any table → this one has none
        return None
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

            mapped_airline = _match_airline(program_raw)
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
                    marker,
                    program_raw,
                    ratio_raw,
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
            marker,
            bank_id,
            rows_total,
            airline_rows,
            mapped,
            skipped_hotel,
            skipped_unmapped,
            ratio_dropped,
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
    conn: Connection,
    records: list[dict],
    dry_run: bool = False,
) -> tuple[int, int]:
    """Full-table snapshot-replace of ``pp.transfer_partners`` (this job is the sole owner).

    Deletes EVERY row, then inserts the freshly-scraped records. Returns
    (rows_deleted, rows_inserted). dry_run → no writes, returns (0, 0).

    ``conn`` is a SQLAlchemy Connection inside a single transaction (opened by ``main()`` via
    ``get_engine().begin()``), so the DELETE + re-INSERT commit atomically — readers never see an
    empty table, and a mid-insert failure rolls the whole snapshot back (strictly better than the
    old non-atomic DuckDB autocommit, which mattered because this is the sole owner of the table).
    """
    if dry_run:
        count = conn.execute(text("SELECT COUNT(*) FROM pp.transfer_partners")).scalar()
        logger.info("[dry-run] Would delete %d row(s) and insert %d row(s).", count, len(records))
        return 0, 0

    deleted = conn.execute(text("SELECT COUNT(*) FROM pp.transfer_partners")).scalar()
    conn.execute(text("DELETE FROM pp.transfer_partners"))

    inserted = 0
    for r in records:
        pq.upsert_transfer_partner(
            conn,
            bank_program_id=r["bank_program_id"],
            airline_code=r["airline_code"],
            program_name=r["program_name"],
            transfer_ratio=r["transfer_ratio"],
            min_transfer=r["min_transfer"],
            transfer_increment=r["transfer_increment"],
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


async def _wait_for_tables(page, min_tables: int, timeout_s: float) -> int:
    """Poll the rendered DOM until it holds at least `min_tables` <table> elements
    AND the count is stable across two reads (page has finished hydrating), or
    until `timeout_s` elapses. Returns the last table count seen.

    This page is large (~450 KB) and renders its partner tables via JS after the
    initial load, so a fixed sleep races the render — a too-short wait yields zero
    tables. Condition-based waiting adapts to however long the render takes and is
    robust to the page getting heavier over time.
    """
    deadline = time.monotonic() + timeout_s
    prev = -1
    count = 0
    while time.monotonic() < deadline:
        await asyncio.sleep(1.0)
        try:
            count = int(await page.evaluate("document.querySelectorAll('table').length") or 0)
        except Exception as exc:  # noqa: BLE001 — keep polling through transient eval hiccups
            logger.debug("table-count probe failed: %s", exc)
            count = 0
        if count >= min_tables and count == prev:
            break
        prev = count
    return count


async def _connect_browser(port: int, attempts: int = 12, delay_s: float = 1.0):
    """Connect to the manually-launched Chrome's CDP endpoint, retrying while it
    finishes binding the debug port.

    On a cold CI runner Chrome can take a variable time to start listening, so a
    single `uc.start()` after a fixed sleep races that startup and intermittently
    raises "Failed to connect to browser" (observed once in a real GH Actions run).
    Retrying the connect for up to ~`attempts * delay_s` seconds removes that flake;
    the happy path connects on the first attempt with no added latency.
    """
    last_exc: Exception | None = None
    for i in range(attempts):
        try:
            return await uc.start(host="127.0.0.1", port=port)
        except Exception as exc:  # noqa: BLE001 — retry transient startup races
            last_exc = exc
            logger.debug("browser connect attempt %d/%d failed: %s", i + 1, attempts, exc)
            await asyncio.sleep(delay_s)
    raise RuntimeError(
        f"could not connect to Chrome on port {port} after {attempts} attempts"
    ) from last_exc


async def _fetch_with_nodriver(url: str, min_tables: int = 8, timeout_s: float = 30.0) -> str:
    """Fetch *url* using a headless Chrome CDP session (WAF bypass).

    Waits for the page's tables to finish rendering (condition-based, see
    `_wait_for_tables`) rather than sleeping a fixed interval. `min_tables` is the
    full expected table count once rendered — 8: the 6 managed bank sections plus
    the 2 skipped ones (Rove, Marriott). Waiting for the full set (not just the 6
    managed) guarantees every managed section has rendered before we scrape,
    regardless of DOM/hydration order. On timeout it returns whatever rendered —
    parse_partners then fail-closes if no managed table is present.
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
            "--user-data-dir=/tmp/tp-scrape-profile",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(2)  # give Chrome a head start before the first connect attempt
    try:
        browser = await _connect_browser(port)
        page = await browser.get(url)
        count = await _wait_for_tables(page, min_tables=min_tables, timeout_s=timeout_s)
        logger.info("page rendered with %d <table> element(s) before scrape", count)
        html = await page.get_content()
        browser.stop()  # sync method — no await
        return html
    finally:
        proc.terminate()


def fetch_page(url: str = SOURCE_URL) -> str:
    """Fetch the transfer-partners page via headless Chrome (nodriver)."""
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
    stats: dict = {}
    ok = False
    try:
        html = fetch_page()
        records, stats = parse_partners(html)
        logger.info(
            "Parsed %d transfer-partner row(s) across %d bank(s) (banks_missing=%d).",
            len(records),
            stats["banks_found"],
            stats["banks_missing"],
        )

        from pp_db.engine import get_engine

        with get_engine().begin() as conn:
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
                "service": "point-pilot-jobs",
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
            ping_heartbeat(PARTNERS_HEARTBEAT_URL, logger)


if __name__ == "__main__":
    raise SystemExit(main())
