"""Standalone Turkish Airlines Miles&Smiles award browser scrape for the points-pilot-jobs runner.

GitHub's Azure runner IPs clear Turkish's TLS-fingerprint block + PerimeterX where Fly/httpx
can't, so the nodriver browser scrape runs here (like Delta/Southwest). Scrapes popular US↔IST
award routes over a near-term date window via one warmed Chrome session (in-page availability
fetch — see ``scrapers/turkish.py``), normalizes, and upserts into Postgres ``pp.flights``, then
exits. Manual workflow_dispatch or daily cron. Tunable via env: TURKISH_SCRAPE_DAYS (default 3);
single-route on-demand mode via TURKISH_ROUTE_ORIGIN/DEST/DATES.

The run plan + scrape loop + run event are shared — see ``browser_scrape_common.py``.
"""

import logging
import os
import sys
from datetime import date

import browser_scrape_common as common
from config.routes import TURKISH_MED_ROUTES
from config.settings import CRON_MAX_LEGS_PER_SHARD
from pipeline.obs import flush_then_hard_exit

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
    stream=sys.stdout,
)
logger = logging.getLogger("turkish_browser_scrape")

# Cron routes now live in config/routes.py (seeded into the scored queue via seed_from_config);
# the daily cron drains the scored due-batch instead of a static list.
MAX_LEGS_PER_SHARD = CRON_MAX_LEGS_PER_SHARD["turkish"]
SCRAPE_DAYS = int(os.getenv("TURKISH_SCRAPE_DAYS", "3"))  # near-term window, scraped every day
RECOVERY_ROUTE = TURKISH_MED_ROUTES[0]

# On-demand single-route mode (workflow_dispatch inputs); empty in the daily cron.
ROUTE_ORIGIN = os.getenv("TURKISH_ROUTE_ORIGIN", "").strip()
ROUTE_DEST = os.getenv("TURKISH_ROUTE_DEST", "").strip()
ROUTE_DATES = os.getenv("TURKISH_ROUTE_DATES", "").strip()

# Cron sharding: split the scored due-batch across N parallel runs on separate IPs (default 1).
SHARDS = max(1, int(os.getenv("TURKISH_SHARDS", "1")))
SHARD_INDEX = int(os.getenv("TURKISH_SHARD_INDEX", "0"))


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
    from scrapers.turkish import TurkishScraper

    route_jobs, dates = common.build_queue_plan(
        "turkish", shard_index=shard_index, shards=shards,
        max_legs=MAX_LEGS_PER_SHARD, scrape_days=SCRAPE_DAYS, today=date.today(),
    )
    pairs: list[tuple[str, str]] = []
    run_route_jobs = route_jobs
    if not route_jobs and shard_index == 0:
        source_snapshot = common.freshness("turkish", logger)
        if source_snapshot.get("turkish_rows") == 0:
            # Recovery work is deliberately outside the scored queue: one configured route/date
            # proves the upstream contract without marking unrelated queue jobs successful. A
            # failure remains eligible for the next daily shard-0 fallback.
            pairs = [RECOVERY_ROUTE]
            dates = [date.today()]
            run_route_jobs = None
            logger.warning(
                "No Turkish rows and no due queue work — shard 0 running recovery probe %s→%s",
                *RECOVERY_ROUTE,
            )
    logger.info(
        "Cron mode (shard %d/%d): %d due routes, %d recovery routes × %d dates",
        shard_index, shards, len(route_jobs), len(pairs), len(dates),
    )
    return common.run_scrape(
        TurkishScraper(), pairs, dates,
        source="turkish", service="point-pilot-turkish", airline="TK",
        logger=logger, route_jobs=run_route_jobs,
    )


def main() -> common.ScrapeOutcome:
    try:
        from config.settings import PriorityTier  # noqa: F401 — also triggers env validation
    except RuntimeError as exc:
        logger.critical("Configuration error: %s", exc)
        sys.exit(1)

    from pipeline.obs import install_log_shipping
    from pp_db.autocommit import migrate

    install_log_shipping("point-pilot-turkish")
    migrate()  # idempotent; brings a fresh DB to the current schema version (no-op on prod)
    logger.info("Schema ready")

    if ROUTE_ORIGIN and ROUTE_DEST:
        # On-demand single-route mode (workflow_dispatch): no queue marking.
        from scrapers.turkish import TurkishScraper

        pairs, dates = _build_plan(
            ROUTE_ORIGIN, ROUTE_DEST, ROUTE_DATES, SCRAPE_DAYS, date.today(), SHARD_INDEX, SHARDS
        )
        logger.info("On-demand mode: %s→%s × %d dates", ROUTE_ORIGIN, ROUTE_DEST, len(dates))
        return common.run_scrape(
            TurkishScraper(), pairs, dates,
            source="turkish", service="point-pilot-turkish", airline="TK",
            logger=logger,
        )
    else:
        return _run_cron(SHARD_INDEX, SHARDS)


if __name__ == "__main__":
    outcome = main()
    # nodriver leaves keepalive/aclose tasks on its loop that keep the interpreter alive, so the
    # process never exits and the GH Actions step hangs until timeout. main() has already
    # scraped/upserted/shipped its metric, so flush briefly then hard-exit (same as delta).
    flush_then_hard_exit(outcome.exit_code, delay_s=3.0)
