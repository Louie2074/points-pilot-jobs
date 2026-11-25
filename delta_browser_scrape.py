"""Standalone Delta SkyMiles award browser scrape for the points-pilot-jobs runner.

Delta's award site is Akamai-walled to Fly/httpx (HTTP 444), so the nodriver browser scrape runs
here on GitHub's Azure runner IPs (which clear Akamai). Scrapes popular Delta hub routes over a
near-term date window via one warmed Chrome session (in-page availability fetch — see
``scrapers/delta.py``), normalizes, and upserts into Postgres ``pp.flights``, then exits. Suitable
for a manual workflow_dispatch or a cron. Tunable via env: DELTA_SCRAPE_DAYS (default 5);
single-route on-demand mode via DELTA_ROUTE_ORIGIN/DEST/DATES; cron sharding via DELTA_SHARDS /
DELTA_SHARD_INDEX. While HTTP 444 persists, the scheduled workflow is a one-route recovery probe.

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
logger = logging.getLogger("delta_browser_scrape")

# Cron routes now live in config/routes.py (seeded into the scored queue via seed_from_config);
# the daily cron drains the scored due-batch instead of a static list.
MAX_LEGS_PER_SHARD = CRON_MAX_LEGS_PER_SHARD["delta"]
SCRAPE_DAYS = int(os.getenv("DELTA_SCRAPE_DAYS", "5"))  # near-term window, scraped every day

# On-demand single-route mode (set by the workflow_dispatch inputs). Empty in the daily cron.
ROUTE_ORIGIN = os.getenv("DELTA_ROUTE_ORIGIN", "").strip()
ROUTE_DEST = os.getenv("DELTA_ROUTE_DEST", "").strip()
ROUTE_DATES = os.getenv("DELTA_ROUTE_DATES", "").strip()

# Cron sharding remains configurable, but the scheduled workflow currently runs one one-route
# recovery probe while Delta returns HTTP 444. Manual single-route dispatch remains available.
SHARDS = max(1, int(os.getenv("DELTA_SHARDS", "1")))
SHARD_INDEX = int(os.getenv("DELTA_SHARD_INDEX", "0"))


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
    from scrapers.delta import DeltaScraper

    scraper = DeltaScraper()
    route_jobs, _flat = common.build_queue_plan(
        "delta", shard_index=shard_index, shards=shards,
        max_legs=MAX_LEGS_PER_SHARD, scrape_days=SCRAPE_DAYS, today=date.today(),
    )
    # Reach the SCRAPE_DAYS horizon (90) with a bounded dense-near + sparse/coarse-tail window
    # instead of build_queue_plan's every-day dates — mirrors alaska_scrape.py. With the
    # scraper's dense_days/sparse_step this is ~27 dates (< the prior 30 every-day), so the
    # per-shard leg×date volume stays under Delta's Akamai ceiling while covering 60-90d out.
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
        source="delta", service="point-pilot-delta", airline="DL",
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

    install_log_shipping("point-pilot-delta")
    migrate()  # idempotent; brings a fresh DB to the current schema version (no-op on prod)
    logger.info("Schema ready")

    if ROUTE_ORIGIN and ROUTE_DEST:
        # On-demand single-route mode (workflow_dispatch): no queue marking.
        from scrapers.delta import DeltaScraper

        pairs, dates = _build_plan(
            ROUTE_ORIGIN, ROUTE_DEST, ROUTE_DATES, SCRAPE_DAYS, date.today(), SHARD_INDEX, SHARDS
        )
        logger.info(
            "On-demand single-route mode: %s→%s × %d dates", ROUTE_ORIGIN, ROUTE_DEST, len(dates)
        )
        return common.run_scrape(
            DeltaScraper(), pairs, dates,
            source="delta", service="point-pilot-delta", airline="DL",
            logger=logger,
        )
    else:
        return _run_cron(SHARD_INDEX, SHARDS)


if __name__ == "__main__":
    outcome = main()
    # nodriver leaves a pending asyncio task (Connection.aclose) after browser teardown that keeps
    # the interpreter alive, so the process never exits on its own and the GitHub Actions step hangs
    # until its timeout. Flush the run event before hard-exiting.
    flush_then_hard_exit(outcome.exit_code, delay_s=3.0)
