"""Standalone Alaska Mileage Plan award scrape for the points-pilot-jobs runner.

Alaska is a plain httpx scraper (no browser). Migrated off the always-on point-pilot-scraper Fly
box to free sharded GitHub Actions crons — Azure runner IPs cleared Alaska's Fastly WAF (probe
2026-06-21). Drains this shard's slice of the scored queue over a dense-near + sparse-tail window,
upserts pp.flights, then exits. Sharding via ALASKA_SHARDS / ALASKA_SHARD_INDEX (GH Actions matrix).
The API box still runs the on-demand inline Alaska scrape independently. Shared run plan/loop/metric
live in browser_scrape_common.py.
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
logger = logging.getLogger("alaska_scrape")

MAX_LEGS_PER_SHARD = CRON_MAX_LEGS_PER_SHARD["alaska"]
SCRAPE_DAYS = int(os.getenv("ALASKA_SCRAPE_DAYS", "30"))  # full horizon; dense near + sparse tail
SHARDS = max(1, int(os.getenv("ALASKA_SHARDS", "1")))
SHARD_INDEX = int(os.getenv("ALASKA_SHARD_INDEX", "0"))


def _run_cron(shard_index: int, shards: int) -> common.ScrapeOutcome:
    """Drain this shard's slice of the scored queue over the dense/sparse window."""
    from scrapers.alaska import AlaskaScraper

    scraper = AlaskaScraper()
    route_jobs, _flat = common.build_queue_plan(
        "alaska", shard_index=shard_index, shards=shards,
        max_legs=MAX_LEGS_PER_SHARD, scrape_days=SCRAPE_DAYS, today=date.today(),
    )
    dates = common.dense_sparse_dates(
        date.today(), scraper.dense_days, scraper.sparse_step, SCRAPE_DAYS
    )
    logger.info(
        "Cron queue mode (shard %d/%d): %d due routes × %d dates",
        shard_index, shards, len(route_jobs), len(dates),
    )
    return common.run_scrape(
        scraper, [], dates,
        source="alaska", service="point-pilot-alaska", airline="AS",
        logger=logger, route_jobs=route_jobs,
    )


def main() -> common.ScrapeOutcome:
    try:
        from config.settings import PriorityTier  # noqa: F401 — triggers env validation
    except RuntimeError as exc:
        logger.critical("Configuration error: %s", exc)
        sys.exit(1)

    from pipeline.obs import install_log_shipping
    from pp_db.autocommit import migrate

    install_log_shipping("point-pilot-alaska")
    migrate()  # idempotent; no-op on prod
    logger.info("Schema ready")
    return _run_cron(SHARD_INDEX, SHARDS)


if __name__ == "__main__":
    outcome = main()
    # Parity with the browser entrypoints' hard-exit convention; harmless for httpx.
    flush_then_hard_exit(outcome.exit_code)
