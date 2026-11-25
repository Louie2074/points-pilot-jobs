"""Standalone Southwest Rapid Rewards award browser scrape for the points-pilot-jobs runner.

Southwest's shopping endpoint is gated by an F5/Shape per-request JS sensor that blocks Fly/httpx,
so the nodriver browser scrape runs here on GitHub's Azure runner IPs — a warmed nodriver Chrome
session mints a valid token per in-page fetch (see ``scrapers/southwest.py``). Scrapes popular
focus-city routes (both directions) over a near-term date window, normalizes, and upserts into
Postgres ``pp.flights``, then exits. Suitable for a manual workflow_dispatch or a cron. Tunable via
env: SOUTHWEST_SCRAPE_DAYS (default 5); single-route on-demand mode via
SOUTHWEST_ROUTE_ORIGIN/DEST/DATES.

The run plan + scrape loop + run event are shared — see ``browser_scrape_common.py``.
"""

import logging
import os
import sys
from datetime import date

import browser_scrape_common as common
from config.settings import CRON_MAX_LEGS_PER_SHARD
from pipeline.obs import flush_then_hard_exit

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
    stream=sys.stdout,
)
logger = logging.getLogger("southwest_browser_scrape")

# Cron routes now live in config/routes.py (seeded into the scored queue via seed_from_config);
# the daily cron drains the scored due-batch instead of a static list.
MAX_LEGS_PER_SHARD = CRON_MAX_LEGS_PER_SHARD["southwest"]
SCRAPE_DAYS = int(os.getenv("SOUTHWEST_SCRAPE_DAYS", "5"))  # near-term window, scraped every day

# On-demand single-route mode (set by the workflow_dispatch inputs). Empty in the daily cron.
ROUTE_ORIGIN = os.getenv("SOUTHWEST_ROUTE_ORIGIN", "").strip()
ROUTE_DEST = os.getenv("SOUTHWEST_ROUTE_DEST", "").strip()
ROUTE_DATES = os.getenv("SOUTHWEST_ROUTE_DATES", "").strip()

# Cron sharding: split the scored due-batch across N parallel runs (default single, unsharded).
SHARDS = max(1, int(os.getenv("SOUTHWEST_SHARDS", "1")))
SHARD_INDEX = int(os.getenv("SOUTHWEST_SHARD_INDEX", "0"))


def _parse_dates_csv(csv: str):  # re-exported for tests
    return common.parse_dates_csv(csv, logger)


def _build_plan(route_origin, route_dest, route_dates_csv, scrape_days, today,
                shard_index=0, shards=1):  # re-exported for the on-demand tests
    # Routes no longer live here (cron drains the queue); the on-demand single-route path
    # never consults the route list, so pass an empty list.
    return common.build_plan(
        [], route_origin, route_dest, route_dates_csv, scrape_days, today,
        shard_index, shards, logger,
    )


def _run_cron(shard_index: int, shards: int) -> common.ScrapeOutcome:
    """Drain this shard's slice of the scored queue (seed → due-batch → stride → cap)."""
    from scrapers.southwest import SouthwestScraper

    scraper = SouthwestScraper()
    route_jobs, _flat = common.build_queue_plan(
        "southwest", shard_index=shard_index, shards=shards,
        max_legs=MAX_LEGS_PER_SHARD, scrape_days=SCRAPE_DAYS, today=date.today(),
    )
    # Reach the SCRAPE_DAYS horizon (90) with a bounded dense-near + sparse/coarse-tail window
    # instead of build_queue_plan's every-day dates — mirrors alaska_scrape.py. Southwest's
    # dense_days/sparse_step are tuned leaner than Delta's (~20 dates vs 14 every-day before)
    # to respect its F5/Shape per-session ceiling and the 150-min job timeout.
    dates = common.dense_sparse_dates(
        date.today(), scraper.dense_days, scraper.sparse_step, SCRAPE_DAYS
    )
    logger.info(
        "Cron queue mode (shard %d/%d): %d due routes × %d dates (max offset %dd)",
        shard_index, shards, len(route_jobs), len(dates),
        (max(dates) - date.today()).days if dates else 0,
    )
    return common.run_scrape(
        scraper, [], dates,
        source="southwest", service="point-pilot-southwest", airline="WN",
        logger=logger, route_jobs=route_jobs,
    )


def main() -> common.ScrapeOutcome:
    try:
        from config.settings import PriorityTier  # noqa: F401 — also triggers env validation
    except RuntimeError as exc:
        logger.critical("Configuration error: %s", exc)
        sys.exit(1)

    from pipeline.obs import install_log_shipping
    from pp_db.autocommit import migrate

    install_log_shipping("point-pilot-southwest")
    migrate()  # idempotent; brings a fresh DB to the current schema version (no-op on prod)
    logger.info("Schema ready")

    if ROUTE_ORIGIN and ROUTE_DEST:
        # On-demand single-route mode (workflow_dispatch): no queue marking.
        from scrapers.southwest import SouthwestScraper

        pairs, dates = _build_plan(
            ROUTE_ORIGIN, ROUTE_DEST, ROUTE_DATES, SCRAPE_DAYS, date.today(), SHARD_INDEX, SHARDS
        )
        logger.info(
            "On-demand single-route mode: %s→%s × %d dates", ROUTE_ORIGIN, ROUTE_DEST, len(dates)
        )
        return common.run_scrape(
            SouthwestScraper(), pairs, dates,
            source="southwest", service="point-pilot-southwest", airline="WN",
            logger=logger,
        )
    else:
        return _run_cron(SHARD_INDEX, SHARDS)


if __name__ == "__main__":
    outcome = main()
    # nodriver leaves keepalive/aclose tasks on its loop that keep the interpreter alive, so the
    # process never exits and the GH Actions step hangs until timeout. main() has already
    # scraped/upserted/shipped its metric, so flush briefly then hard-exit.
    flush_then_hard_exit(outcome.exit_code, delay_s=3.0)
