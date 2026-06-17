"""Shared helpers for the per-airline browser-scrape entrypoints (delta / southwest / turkish /
etihad ``*_browser_scrape.py``).

Each entrypoint is a thin config (its route list, ``<AIRLINE>_*`` env vars, and its Scraper class);
the run plan, the scrape loop, the Better Stack metric, the freshness snapshot, and the heartbeat
ping are identical across all of them and live here. To add a new no-login airline scraper you
write ``scrapers/<airline>.py`` (a BrowserScraper subclass) and a ~40-line entrypoint that calls
``run_scrape()`` — see ``etihad_browser_scrape.py`` as the reference, and ``CLAUDE.md`` for the
full onboarding playbook.

The entrypoints re-export ``parse_dates_csv``/``build_plan`` as their module-level
``_parse_dates_csv``/``_build_plan`` so the existing per-airline unit tests keep importing them.
"""

from __future__ import annotations

import logging
import time
import urllib.request
from datetime import date, datetime, timedelta, timezone


def parse_dates_csv(csv: str, logger: logging.Logger | None = None) -> list[date]:
    """Parse a comma-separated ISO-date string (the ``*_ROUTE_DATES`` workflow input) into dates,
    dropping blanks/invalid tokens (logged as warnings)."""
    out: list[date] = []
    for tok in csv.split(","):
        tok = tok.strip()
        if not tok:
            continue
        try:
            out.append(date.fromisoformat(tok))
        except ValueError:
            if logger:
                logger.warning("ignoring invalid date %r", tok)
    return out


def build_plan(
    routes: list[tuple[str, str]],
    route_origin: str,
    route_dest: str,
    route_dates_csv: str,
    scrape_days: int,
    today: date,
    shard_index: int = 0,
    shards: int = 1,
    logger: logging.Logger | None = None,
) -> tuple[list[tuple[str, str]], list[date]]:
    """(pairs, dates) for this run.

    Single-route on-demand mode when both origin+dest are given (uses the CSV dates if provided,
    else the near-term window); otherwise cron mode over this shard's stride of ``routes`` in both
    directions. Sharding splits ``routes[shard_index::shards]`` so N parallel runs cover disjoint
    routes (``shards=1`` = the whole list, unsharded). On-demand mode only runs on shard 0.
    """
    if route_origin and route_dest:
        if shard_index != 0:
            return [], []
        dates = parse_dates_csv(route_dates_csv, logger) or [
            today + timedelta(days=i) for i in range(scrape_days)
        ]
        return [(route_origin.upper(), route_dest.upper())], dates

    if bool(route_origin) != bool(route_dest) and logger:
        logger.warning(
            "partial route (origin=%r dest=%r) — running cron mode", route_origin, route_dest
        )

    pairs: list[tuple[str, str]] = []
    for origin, dest in routes[shard_index::shards]:
        pairs.append((origin, dest))
        pairs.append((dest, origin))
    dates = [today + timedelta(days=i) for i in range(scrape_days)]
    return pairs, dates


def ping_heartbeat(url: str, logger: logging.Logger) -> None:
    """Ping a Better Stack/uptime heartbeat URL (no-op if unset). Monitoring must never break it."""
    if not url:
        return
    try:
        urllib.request.urlopen(url, timeout=10).close()
    except Exception as exc:  # noqa: BLE001
        logger.warning("heartbeat ping failed: %s", exc)


def freshness(source: str, logger: logging.Logger) -> dict:
    """``{<source>_rows, <source>_newest_age_h}`` snapshot for the metric. Best-effort/no-raise."""
    try:
        from db.connection import get_connection

        total, newest = (
            get_connection()
            .execute("SELECT count(*), max(scraped_at_utc) FROM flights WHERE source = ?", [source])
            .fetchone()
        )
        age_h = None
        if newest is not None:
            if newest.tzinfo is None:
                newest = newest.replace(tzinfo=timezone.utc)
            age_h = round((datetime.now(timezone.utc) - newest).total_seconds() / 3600, 1)
        return {f"{source}_rows": int(total or 0), f"{source}_newest_age_h": age_h}
    except Exception as exc:  # noqa: BLE001
        logger.warning("freshness snapshot failed: %s", exc)
        return {}


def run_scrape(
    scraper,
    pairs: list[tuple[str, str]],
    dates: list[date],
    *,
    source: str,
    service: str,
    airline: str,
    heartbeat_url: str,
    logger: logging.Logger,
) -> int:
    """Run the scrape loop (every pair × date), upsert valid+stamped rows, then ship the
    ``scrape_run`` metric + heartbeat. Aborts the run after the scraper raises ScraperBlockedError
    (rows already upserted persist). Always tears the scraper + DB connection down. Returns the
    total rows upserted."""
    from config.settings import PriorityTier
    from db.connection import close_connection
    from db.queries import upsert_flights
    from pipeline.normalizer import filter_valid, stamp_expiry
    from pipeline.obs import ship_metric
    from scrapers.base import ScraperBlockedError

    started = time.monotonic()
    total = error_count = routes_scraped = 0
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
                    logger.warning("blocked (%s) — aborting run (rows so far persist)", exc)
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
            "service": service,
            "airline": airline,
            "due_routes": len(pairs),
            "routes_scraped": routes_scraped,
            "records": total,
            "errors": error_count,
            "duration_s": duration_s,
            "blocked": blocked,
            **freshness(source, logger),
        }
    )
    ping_heartbeat(heartbeat_url, logger)
    logger.info(
        "=== done — %d %s records upserted (routes=%d errors=%d blocked=%s) in %ss ===",
        total, source, routes_scraped, error_count, blocked, duration_s,
    )
    return total
