"""Standalone JetBlue TrueBlue award scrape for the points-pilot-jobs runner.

JetBlue is a plain httpx scraper (no browser). Migrated off the always-on point-pilot-scraper Fly
box to free sharded GitHub Actions crons (probe 2026-06-21: clean from Azure IPs). Drains this
shard's slice of the scored queue over a dense-near + sparse-tail window, upserts pp.flights, then
exits. Sharding via JETBLUE_SHARDS / JETBLUE_SHARD_INDEX. The API box still runs the on-demand
inline JetBlue scrape independently. Shared logic lives in browser_scrape_common.py.
"""

import logging
import os
import sys
import time
from datetime import date

import browser_scrape_common as common
from config.settings import CRON_MAX_LEGS_PER_SHARD

JETBLUE_HEARTBEAT_URL = os.getenv("JETBLUE_HEARTBEAT_URL", "")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
    stream=sys.stdout,
)
logger = logging.getLogger("jetblue_scrape")

MAX_LEGS_PER_SHARD = CRON_MAX_LEGS_PER_SHARD["jetblue"]
SCRAPE_DAYS = int(os.getenv("JETBLUE_SCRAPE_DAYS", "30"))
SHARDS = max(1, int(os.getenv("JETBLUE_SHARDS", "1")))
SHARD_INDEX = int(os.getenv("JETBLUE_SHARD_INDEX", "0"))


def _run_cron(shard_index: int, shards: int) -> None:
    from scrapers.jetblue import JetBlueScraper

    scraper = JetBlueScraper()
    route_jobs, _flat = common.build_queue_plan(
        "jetblue", shard_index=shard_index, shards=shards,
        max_legs=MAX_LEGS_PER_SHARD, scrape_days=SCRAPE_DAYS, today=date.today(),
    )
    dates = common.dense_sparse_dates(
        date.today(), scraper.dense_days, scraper.sparse_step, SCRAPE_DAYS
    )
    logger.info(
        "Cron queue mode (shard %d/%d): %d due routes × %d dates",
        shard_index, shards, len(route_jobs), len(dates),
    )
    common.run_scrape(
        scraper, [], dates,
        source="jetblue", service="point-pilot-jetblue", airline="B6",
        heartbeat_url=JETBLUE_HEARTBEAT_URL, logger=logger, route_jobs=route_jobs,
    )


def main() -> None:
    try:
        from config.settings import PriorityTier  # noqa: F401 — triggers env validation
    except RuntimeError as exc:
        logger.critical("Configuration error: %s", exc)
        sys.exit(1)

    from pipeline.obs import install_log_shipping
    from pp_db.autocommit import migrate

    install_log_shipping("point-pilot-jetblue")
    migrate()
    logger.info("Schema ready")
    _run_cron(SHARD_INDEX, SHARDS)


if __name__ == "__main__":
    main()
    time.sleep(1)
    os._exit(0)
