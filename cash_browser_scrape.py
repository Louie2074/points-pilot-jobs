"""Sharded one-shot Google Flights CASH scraper for the points-pilot-jobs runner.

The always-on Fly app (``google_flights_main.py``) does one serial Chrome run→sleep loop. This
entrypoint instead does ONE bounded, sharded batch and exits, so it can run as a GitHub-Actions
shard matrix in parallel (Google Flights serves the Azure runner IP cleanly at volume). It reuses
the SAME cash primitives as ``run_once`` — coverage → scrape → match → upsert ``cash_fares`` — but:

  * partitions the route set by shard (``CASH_SHARDS`` / ``CASH_SHARD_INDEX``) so each run takes a
    disjoint slice (a whole route stays on one shard — see ``get_top_cash_routes``),
  * scrapes ALL cabins every run (GA has headroom; no ``cabins_for_run`` demotion),
  * enforces a wall-clock budget (``CASH_RUN_BUDGET_S``) so a rare nodriver listener-race hang can
    never run past the GH-Actions step timeout, and
  * hard-exits with ``os._exit(0)`` after a brief flush (nodriver leaves pending asyncio tasks that
    otherwise keep the interpreter alive — same reason the award browser scrapes do).

Leave ``google_flights_main.py`` (the Fly path) untouched — both upsert ``pp.cash_fares`` on the
same natural key, so GA can bake in parallel with Fly with no double-write.
"""

from __future__ import annotations

import logging
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone

from config.settings import (
    CASH_CABINS,
    CASH_MATCH_TOLERANCE_MIN,
    CASH_SCRAPE_DAYS,
    CASH_TOP_ROUTES,
    CASH_TTL_HOURS,
    CASH_ZERO_REPROBE_DAYS,
    GFLIGHTS_HEARTBEAT_URL,
)
from pipeline.cash_matcher import match_cash_fares
from pipeline.obs import install_log_shipping, ship_cash_run
from pp_db.autocommit import (
    get_flights_for_match,
    get_top_cash_routes,
    upsert_cash_coverage,
    upsert_cash_fares,
)
from scrapers.base import ScraperBlockedError
from scrapers.google_flights import GoogleFlightsScraper

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
    stream=sys.stdout,
)
logger = logging.getLogger("cash_browser_scrape")


def _shard_config() -> tuple[int, int, int]:
    """(shard_index, shard_count, run_budget_s) from env, read at call time so tests/CI can set
    them per-run. The GH-Actions matrix supplies CASH_SHARDS + CASH_SHARD_INDEX."""
    shard_count = max(1, int(os.getenv("CASH_SHARDS", "1")))
    shard_index = int(os.getenv("CASH_SHARD_INDEX", "0"))
    run_budget_s = int(os.getenv("CASH_RUN_BUDGET_S", "5400"))  # default 90 min
    return shard_index, shard_count, run_budget_s


def _ping_heartbeat() -> None:
    """Ping the Better Stack heartbeat so a stalled run raises an alert (parity with the Fly
    runner's heartbeat). No-op unless GFLIGHTS_HEARTBEAT_URL is set."""
    if not GFLIGHTS_HEARTBEAT_URL:
        return
    try:
        urllib.request.urlopen(GFLIGHTS_HEARTBEAT_URL, timeout=10).close()
    except Exception as exc:  # noqa: BLE001 — monitoring must never break the run
        logger.warning("heartbeat ping failed: %s", exc)


def main() -> None:
    install_log_shipping("point-pilot-gflights")
    # No migrate(): the pp schema is Alembic-managed; the award browser scrapes also skip it.

    shard_index, shard_count, run_budget_s = _shard_config()
    start = time.monotonic()
    now = datetime.now(timezone.utc)
    scraper = GoogleFlightsScraper()
    total, zero, failed, blocked = 0, 0, 0, False
    routes: list = []
    try:
        routes = get_top_cash_routes(
            CASH_TOP_ROUTES,
            CASH_SCRAPE_DAYS,
            CASH_TTL_HOURS,
            cabins=CASH_CABINS,
            shard_index=shard_index,
            shard_count=shard_count,
        )
        logger.info(
            "cash run (shard %d/%d): %d route/date/cabin units",
            shard_index,
            shard_count,
            len(routes),
        )
        for origin, dest, travel, cabin in routes:
            # Budget guard: stop cleanly before each unit so a rare listener-race hang on the
            # previous scrape can't push the run past run_budget_s.
            if time.monotonic() - start >= run_budget_s:
                logger.warning(
                    "run budget %ds elapsed — stopping cleanly (rows so far persist)",
                    run_budget_s,
                )
                break
            try:
                fares = scraper.scrape_fares(origin, dest, travel, cabin=cabin)
            except ScraperBlockedError:
                logger.warning("blocked — aborting run (rows so far persist)")
                blocked = True
                break
            except Exception as exc:  # noqa: BLE001 — one route/date must not sink the run
                logger.error("scrape error %s->%s %s %s: %s", origin, dest, travel, cabin, exc)
                failed += 1
                continue
            award = get_flights_for_match(origin, dest, travel, cabin=cabin)
            recs = match_cash_fares(
                fares,
                award,
                origin=origin,
                destination=dest,
                travel_date=travel,
                now=now,
                ttl_hours=CASH_TTL_HOURS,
                tolerance_min=CASH_MATCH_TOLERANCE_MIN,
                cabin=cabin,
            )
            if recs:
                upsert_cash_fares(recs)
                total += len(recs)
                logger.info("%s->%s %s %s: %d fares", origin, dest, travel, cabin, len(recs))
            else:
                zero += 1
            # Negative memory: a zero-yield (route,date,cabin) is skipped for
            # CASH_ZERO_REPROBE_DAYS so it stops consuming a scrape slot every run.
            upsert_cash_coverage(
                origin,
                dest,
                travel,
                cabin=cabin,
                fare_count=len(recs or []),
                reprobe_days=CASH_ZERO_REPROBE_DAYS,
            )
    finally:
        scraper.close()

    duration_s = round(time.monotonic() - start, 1)
    ship_cash_run(
        routes=len(routes),
        fares=total,
        routes_zero=zero,
        dates_failed=failed,
        blocked=blocked,
        duration_s=duration_s,
    )
    _ping_heartbeat()
    logger.info(
        "cash run done (shard %d/%d): %d fares (zero=%d failed=%d blocked=%s) in %ss",
        shard_index,
        shard_count,
        total,
        zero,
        failed,
        blocked,
        duration_s,
    )


if __name__ == "__main__":
    main()
    # nodriver leaves a pending asyncio task after browser teardown that keeps the interpreter
    # alive, so the process never exits on its own and the GH-Actions step hangs until its
    # timeout. main() has scraped, upserted, shipped its metric, and pinged the heartbeat by now,
    # so flush briefly then hard-exit (same pattern as the award browser scrapes).
    time.sleep(3)
    os._exit(0)
