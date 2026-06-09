"""Standalone Delta browser scrape for the points-pilot-jobs GitHub Actions runner.

GitHub's Azure runner IPs clear Delta's Akamai edge block (HTTP 200) where Fly's IP gets 444,
so the nodriver browser scrape runs here. The packages under scrapers/ config/ db/ pipeline/
are vendored from points-pilot-scrapers (see VENDORED_DELTA.md) so this repo is self-contained —
no cross-repo checkout / PAT needed. (scrapers/browser.py + scrapers/delta.py are canonical here;
the rest are copies of the scraper repo's shared modules.)

One-shot: scrapes the most-popular Delta routes (both directions) over a near-term date window
via one warmed Chrome session, normalizes, and upserts into MotherDuck `flights`, then exits.
Suitable for a manual workflow_dispatch or a cron. Tunable via env: DELTA_SCRAPE_DAYS (default 5).
"""

import logging
import os
import sys
import time
import urllib.request
from datetime import date, datetime, timedelta, timezone

DELTA_HEARTBEAT_URL = os.getenv("DELTA_HEARTBEAT_URL", "")  # optional GH-Actions run heartbeat


class _SuppressHashlibWarnings(logging.Filter):
    """Drop the blake2b/blake2s errors some Python builds' hashlib emits on import."""

    def filter(self, record: logging.LogRecord) -> bool:
        return "blake2" not in record.getMessage()


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logging.getLogger().handlers[0].addFilter(_SuppressHashlibWarnings())
logger = logging.getLogger("delta_browser_scrape")

# Most-popular Delta routes (ATL megahub + busiest transcons); both directions are scraped.
DELTA_ROUTES: list[tuple[str, str]] = [
    ("ATL", "LAX"),
    ("ATL", "MCO"),
    ("ATL", "LGA"),
    ("JFK", "LAX"),
    ("ATL", "SEA"),
    ("ATL", "DEN"),
    ("ATL", "FLL"),
    ("ATL", "BOS"),
    ("LAX", "SEA"),
    ("ATL", "DFW"),
]
SCRAPE_DAYS = int(os.getenv("DELTA_SCRAPE_DAYS", "5"))  # near-term window, scraped every day

# On-demand single-route mode (set by the workflow_dispatch inputs). Empty in the daily cron.
ROUTE_ORIGIN = os.getenv("DELTA_ROUTE_ORIGIN", "").strip()
ROUTE_DEST = os.getenv("DELTA_ROUTE_DEST", "").strip()
ROUTE_DATES = os.getenv("DELTA_ROUTE_DATES", "").strip()


def _parse_dates_csv(csv: str) -> list[date]:
    """Parse a comma-separated list of ISO YYYY-MM-DD dates, dropping blanks/invalid ones."""
    out: list[date] = []
    for tok in csv.split(","):
        tok = tok.strip()
        if not tok:
            continue
        try:
            out.append(date.fromisoformat(tok))
        except ValueError:
            logger.warning("ignoring invalid date %r in DELTA_ROUTE_DATES", tok)
    return out


def _build_plan(
    route_origin: str,
    route_dest: str,
    route_dates_csv: str,
    scrape_days: int,
    today: date,
) -> tuple[list[tuple[str, str]], list[date]]:
    """Return (pairs, dates) for this run.

    Single-route mode (origin AND dest provided): just that route in the requested
    direction, over the supplied dates (or the near-term window if none given).
    Cron mode (no route): every popular route in both directions over the window.
    A partial route (only one of origin/dest) is treated as cron mode.
    """
    if route_origin and route_dest:
        pairs = [(route_origin.upper(), route_dest.upper())]
        dates = _parse_dates_csv(route_dates_csv)
        if not dates:
            dates = [today + timedelta(days=i) for i in range(scrape_days)]
        return pairs, dates

    if bool(route_origin) != bool(route_dest):
        logger.warning(
            "partial route (origin=%r dest=%r) — ignoring and running cron mode",
            route_origin,
            route_dest,
        )

    pairs = []
    for origin, dest in DELTA_ROUTES:
        pairs.append((origin, dest))
        pairs.append((dest, origin))
    dates = [today + timedelta(days=i) for i in range(scrape_days)]
    return pairs, dates


def _ping_heartbeat() -> None:
    """Ping the Better Stack heartbeat so a missed daily Delta run raises an alert.
    No-op unless DELTA_HEARTBEAT_URL is set."""
    if not DELTA_HEARTBEAT_URL:
        return
    try:
        urllib.request.urlopen(DELTA_HEARTBEAT_URL, timeout=10).close()
    except Exception as exc:  # noqa: BLE001 — monitoring must never break the run
        logger.warning("heartbeat ping failed: %s", exc)


def _delta_freshness() -> dict:
    """Snapshot how many / how fresh the Delta flight rows are (parity with the award
    scraper's _data_freshness). Best-effort — never breaks a run."""
    try:
        from db.connection import get_connection

        total, newest = (
            get_connection()
            .execute("SELECT count(*), max(scraped_at_utc) FROM flights WHERE source = 'delta'")
            .fetchone()
        )
        age_h = None
        if newest is not None:
            if newest.tzinfo is None:
                newest = newest.replace(tzinfo=timezone.utc)
            age_h = round((datetime.now(timezone.utc) - newest).total_seconds() / 3600, 1)
        return {"delta_rows": int(total or 0), "delta_newest_age_h": age_h}
    except Exception as exc:  # noqa: BLE001
        logger.warning("freshness snapshot failed: %s", exc)
        return {}


def main() -> None:
    try:
        from config.settings import PriorityTier  # noqa: F401 — also triggers env validation
    except RuntimeError as exc:
        logger.critical("Configuration error: %s", exc)
        sys.exit(1)

    from db.connection import close_connection
    from db.queries import upsert_flights
    from db.schema import migrate
    from pipeline.normalizer import filter_valid, stamp_expiry
    from pipeline.obs import install_log_shipping, ship_metric
    from scrapers.base import ScraperBlockedError
    from scrapers.delta import DeltaScraper

    install_log_shipping("point-pilot-delta")  # ship WARNING+ logs to Better Stack
    migrate()  # idempotent; ensures the flights table exists
    logger.info("Schema ready")

    pairs, dates = _build_plan(ROUTE_ORIGIN, ROUTE_DEST, ROUTE_DATES, SCRAPE_DAYS, date.today())
    if ROUTE_ORIGIN and ROUTE_DEST:
        logger.info(
            "On-demand single-route mode: %s→%s × %d dates", ROUTE_ORIGIN, ROUTE_DEST, len(dates)
        )
    else:
        logger.info("Cron mode: %d routes × %d dates", len(pairs), len(dates))

    scraper = DeltaScraper()
    started = time.monotonic()
    total = 0
    error_count = 0
    routes_scraped = 0
    blocked = False
    try:
        for origin, dest in pairs:
            if blocked:
                break
            route_recs = 0
            for travel in dates:
                try:
                    recs = scraper.scrape(origin, dest, travel)
                except ScraperBlockedError as exc:
                    logger.warning("Akamai blocked (%s) — aborting run (rows so far persist)", exc)
                    blocked = True
                    break
                except Exception as exc:  # noqa: BLE001 — one route/date must not sink the run
                    logger.error("Error scraping %s→%s %s: %s", origin, dest, travel, exc)
                    error_count += 1
                    continue
                stamped = stamp_expiry(filter_valid(recs), PriorityTier.MED)
                if stamped:
                    upsert_flights(stamped)
                    route_recs += len(stamped)
                    total += len(stamped)
            routes_scraped += 1
            logger.info("%s→%s: %d records", origin, dest, route_recs)
    finally:
        scraper.close()
        close_connection()

    duration_s = round(time.monotonic() - started, 1)
    ship_metric(
        {
            "event": "scrape_run",
            "service": "point-pilot-delta",
            "airline": "DL",
            "due_routes": len(pairs),
            "routes_scraped": routes_scraped,
            "records": total,
            "errors": error_count,
            "duration_s": duration_s,
            "blocked": blocked,
            **_delta_freshness(),
        }
    )
    _ping_heartbeat()
    logger.info(
        "=== done — %d Delta records upserted (routes=%d errors=%d blocked=%s) in %ss ===",
        total,
        routes_scraped,
        error_count,
        blocked,
        duration_s,
    )


if __name__ == "__main__":
    main()
    # nodriver leaves a pending asyncio task (Connection.aclose) after browser teardown
    # that keeps the interpreter alive, so the process never exits on its own and the GitHub
    # Actions step hangs until its 75-min timeout cancels it — every run, ~74 min after the
    # ~50s scrape actually finishes (confirmed in the 2026-06-08 run logs). main() has already
    # scraped, upserted, shipped its metric, and pinged the heartbeat by this point, so give
    # the best-effort metric POST a moment to flush, then hard-exit past the stuck task.
    time.sleep(3)
    os._exit(0)
