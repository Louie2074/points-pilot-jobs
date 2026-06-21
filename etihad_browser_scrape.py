"""Standalone Etihad Guest award browser scrape for the points-pilot-jobs runner.

GitHub's Azure runner IPs clear Etihad's Akamai + Imperva ABP where Fly/httpx can't, so the
nodriver browser scrape runs here (like Delta/Southwest/Turkish). Drives the public award
deep-link per US↔AUH route over a near-term date window in one warmed Chrome session, DOM-scrapes
the rendered fare-selection cards (see ``scrapers/etihad.py``), normalizes, and upserts into
Postgres ``pp.flights``, then exits. Manual workflow_dispatch or daily cron. Tunable via env:
ETIHAD_SCRAPE_DAYS (default 3); single-route on-demand mode via ETIHAD_ROUTE_ORIGIN/DEST/DATES.

The run plan + scrape loop + metric + heartbeat are shared — see ``browser_scrape_common.py``.
"""

import logging
import os
import sys
import time
from datetime import date

import browser_scrape_common as common
from config.settings import CRON_MAX_LEGS_PER_SHARD

ETIHAD_HEARTBEAT_URL = os.getenv("ETIHAD_HEARTBEAT_URL", "")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
    stream=sys.stdout,
)
logger = logging.getLogger("etihad_browser_scrape")

# Cron routes now live in config/routes.py (seeded into the scored queue via seed_from_config);
# the daily cron drains the scored due-batch instead of a static list.
MAX_LEGS_PER_SHARD = CRON_MAX_LEGS_PER_SHARD["etihad"]
SCRAPE_DAYS = int(os.getenv("ETIHAD_SCRAPE_DAYS", "3"))  # near-term window, scraped every day

# On-demand single-route mode (workflow_dispatch inputs); empty in the daily cron.
ROUTE_ORIGIN = os.getenv("ETIHAD_ROUTE_ORIGIN", "").strip()
ROUTE_DEST = os.getenv("ETIHAD_ROUTE_DEST", "").strip()
ROUTE_DATES = os.getenv("ETIHAD_ROUTE_DATES", "").strip()

# Cron sharding: split the scored due-batch across N parallel runs on separate IPs (default single).
SHARDS = max(1, int(os.getenv("ETIHAD_SHARDS", "1")))
SHARD_INDEX = int(os.getenv("ETIHAD_SHARD_INDEX", "0"))


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


def _run_cron(shard_index: int, shards: int) -> None:
    """Drain this shard's slice of the scored queue (seed → due-batch → stride → cap)."""
    from scrapers.etihad import EtihadScraper

    route_jobs, dates = common.build_queue_plan(
        "etihad", shard_index=shard_index, shards=shards,
        max_legs=MAX_LEGS_PER_SHARD, scrape_days=SCRAPE_DAYS, today=date.today(),
    )
    logger.info(
        "Cron queue mode (shard %d/%d): %d due routes × %d dates",
        shard_index, shards, len(route_jobs), len(dates),
    )
    common.run_scrape(
        EtihadScraper(), [], dates,
        source="etihad", service="point-pilot-etihad", airline="EY",
        heartbeat_url=ETIHAD_HEARTBEAT_URL, logger=logger, route_jobs=route_jobs,
    )


def main() -> None:
    try:
        from config.settings import PriorityTier  # noqa: F401 — also triggers env validation
    except RuntimeError as exc:
        logger.critical("Configuration error: %s", exc)
        sys.exit(1)

    from pipeline.obs import install_log_shipping
    from pp_db.autocommit import migrate

    install_log_shipping("point-pilot-etihad")
    migrate()  # idempotent; brings a fresh DB to the current schema version (no-op on prod)
    logger.info("Schema ready")

    if ROUTE_ORIGIN and ROUTE_DEST:
        # On-demand single-route mode (workflow_dispatch): no queue marking.
        from scrapers.etihad import EtihadScraper

        pairs, dates = _build_plan(
            ROUTE_ORIGIN, ROUTE_DEST, ROUTE_DATES, SCRAPE_DAYS, date.today(), SHARD_INDEX, SHARDS
        )
        logger.info("On-demand mode: %s→%s × %d dates", ROUTE_ORIGIN, ROUTE_DEST, len(dates))
        common.run_scrape(
            EtihadScraper(), pairs, dates,
            source="etihad", service="point-pilot-etihad", airline="EY",
            heartbeat_url=ETIHAD_HEARTBEAT_URL, logger=logger,
        )
    else:
        _run_cron(SHARD_INDEX, SHARDS)


if __name__ == "__main__":
    main()
    # nodriver leaves keepalive/aclose tasks on its loop that keep the interpreter alive, so the
    # process never exits and the GH Actions step hangs until timeout. main() has already
    # scraped/upserted/shipped its metric, so flush briefly then hard-exit (same as delta/turkish).
    time.sleep(3)
    os._exit(0)
