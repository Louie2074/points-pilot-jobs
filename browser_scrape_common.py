"""Shared helpers for the per-airline browser-scrape entrypoints (delta / southwest / turkish /
etihad ``*_browser_scrape.py``).

Each entrypoint is a thin config (its route list, ``<AIRLINE>_*`` env vars, and its Scraper class);
the run plan, scrape loop, and Grafana run events live here. To add a new no-login airline,
write ``scrapers/<airline>.py`` (a BrowserScraper subclass) and a ~40-line entrypoint that calls
``run_scrape()`` — see ``etihad_browser_scrape.py`` as the reference, and ``CLAUDE.md`` for the
full onboarding playbook.

The entrypoints re-export ``parse_dates_csv``/``build_plan`` as their module-level
``_parse_dates_csv``/``_build_plan`` so the existing per-airline unit tests keep importing them.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Literal

# Wall-clock budget for a single cron run. The shared scrape loop stops cleanly between routes
# once this is reached so a long shard (AS/B6) can never overrun the GitHub-Actions 60-minute job
# cap and get hard-cancelled mid-run (which drops the scrape_run event and leaves
# in-flight routes unmarked). Routes not reached simply stay due for the next run. 45 min leaves
# ~15 min of headroom under the 60-min workflow timeout.
DEFAULT_CRON_TIME_BUDGET_S = 2700.0

ScrapeStatus = Literal["healthy", "partial", "blocked", "failed"]


@dataclass(frozen=True)
class ScrapeOutcome:
    """Structured result shared by metrics, logs, and entrypoint process exits."""

    status: ScrapeStatus
    due_routes: int
    records: int
    errors: int
    routes_scraped: int
    routes_unchanged: int
    routes_zero: int
    blocked: bool
    stopped_early: bool
    upstream_requests: int

    @property
    def exit_code(self) -> int:
        return 1 if self.status in {"blocked", "failed"} else 0


def _default_time_budget_s() -> float:
    try:
        return float(os.getenv("CRON_TIME_BUDGET_S", str(DEFAULT_CRON_TIME_BUDGET_S)))
    except ValueError:
        return DEFAULT_CRON_TIME_BUDGET_S


@dataclass
class _PairJob:
    """Minimal ``.origin``/``.dest`` carrier so the on-demand path feeds ``run_scrape``'s unified
    loop the same shape as a queue-mode ``RouteJob`` (which also exposes ``.origin``/``.dest``)."""

    origin: str
    dest: str


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


def dense_sparse_dates(
    today: date,
    dense_days: int,
    sparse_step: int,
    max_day: int,
    *,
    sparse_max: int = 30,
    coarse_step: int = 7,
) -> list[date]:
    """Three-tier scrape window (mirrors ``scraper/pipeline/scheduler.py`` ``_scrape_window``):
    every day for the first ``dense_days`` offsets, then every ``sparse_step``-th day out to
    ``sparse_max`` (the 30d near-term boundary), then — when ``max_day`` reaches past it — every
    ``coarse_step``-th day out to ``max_day`` EXCLUSIVE. The coarse tail lets the httpx airlines
    (AS/B6, ``<AIRLINE>_SCRAPE_DAYS=90``) cover the 3-month horizon cheaply; with ``max_day<=30``
    (the browser airlines) no coarse dates are generated, so their profile is unchanged."""
    dense = min(dense_days, max_day)
    offsets = list(range(dense))
    offsets += list(range(dense, min(sparse_max, max_day), max(1, sparse_step)))
    if max_day > sparse_max:
        offsets += list(range(sparse_max, max_day, max(1, coarse_step)))
    offsets = sorted(set(offsets))
    return [today + timedelta(days=n) for n in offsets]


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
    """(pairs, dates) for the single-route on-demand path.

    Single-route on-demand mode when both origin+dest are given (uses the CSV dates if provided,
    else the near-term window); only runs on shard 0 so an on-demand dispatch that spawns every
    matrix shard doesn't scrape the route twice. Cron scheduling lives in ``build_queue_plan``
    (the scored queue), so callers always pass ``routes=[]`` and never reach the cron path here —
    a partial or absent route yields an empty plan.
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
            "partial route (origin=%r dest=%r) — no plan (cron drains the queue)",
            route_origin, route_dest,
        )

    return [], []


def build_queue_plan(
    airline: str,
    *,
    shard_index: int,
    shards: int,
    max_legs: int,
    scrape_days: int,
    today: date,
):
    """(route_jobs, dates) for a cron queue-mode run.

    Seeds the airline's routes (idempotent upsert of all airlines via
    ``QueueManager.seed_from_config()``), reads the scored due-batch, takes this shard's stride
    (``due[shard_index::shards]``) so N parallel runs cover disjoint routes, and caps it at
    ``max_legs`` directed routes. The returned ``RouteJob``s carry the tier / interval_h /
    change_rate / last_cheapest needed for adaptive marking in ``run_scrape``.
    """
    from pipeline.queue_manager import QueueManager

    q = QueueManager(scraper=None)
    q.seed_from_config()  # idempotent upsert of ALL airlines incl. this one
    # get_due_batch widens by SCORE_FETCH_MULTIPLE internally — pass the plain intended batch size.
    due = q.get_due_batch(limit=max_legs * shards, airline=airline)
    mine = due[shard_index::shards][:max_legs]
    dates = [today + timedelta(days=i) for i in range(scrape_days)]
    return mine, dates


def freshness(source: str, logger: logging.Logger) -> dict:
    """Bounded source-row freshness snapshot for the metric. Best-effort/no-raise.

    The existing total-row and newest-age fields remain stable for dashboards. The unexpired-row
    count is the authoritative idle data health signal because source TTLs vary per stamped row.
    """
    try:
        from sqlalchemy import text

        from pp_db.engine import get_engine

        with get_engine().connect() as c:
            total, newest, unexpired = c.execute(
                text(
                    "SELECT count(*), max(scraped_at_utc), "
                    "count(*) FILTER (WHERE expires_at_utc > now()) "
                    "FROM pp.flights WHERE source = :source"
                ),
                {"source": source},
            ).fetchone()
        age_h = None
        if newest is not None:
            if newest.tzinfo is None:
                newest = newest.replace(tzinfo=timezone.utc)
            age_h = round((datetime.now(timezone.utc) - newest).total_seconds() / 3600, 1)
        return {
            f"{source}_rows": int(total or 0),
            f"{source}_newest_age_h": age_h,
            f"{source}_unexpired_rows": int(unexpired or 0),
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("freshness snapshot failed: %s", exc)
        return {}


def _has_fresh_source_data(snapshot: dict, source: str) -> bool:
    """Whether the snapshot proves this source has rows whose stamped expiry is in the future."""
    unexpired_rows = snapshot.get(f"{source}_unexpired_rows")
    return (
        isinstance(unexpired_rows, int)
        and not isinstance(unexpired_rows, bool)
        and unexpired_rows > 0
    )


def _tier_for_job(job, default: str) -> str:
    """The expiry tier for a scraped route: the queue RouteJob's adaptive tier in cron mode,
    or `default` for on-demand _PairJobs (which have no .tier). Fixes the prior flat-MED stamp
    that ignored HIGH (8h) / LOW (48h) windows."""
    return getattr(job, "tier", default)


def run_scrape(
    scraper,
    pairs: list[tuple[str, str]],
    dates: list[date],
    *,
    source: str,
    service: str,
    airline: str,
    logger: logging.Logger,
    route_jobs=None,
    time_budget_s: float | None = None,
) -> ScrapeOutcome:
    """Run the scrape loop (every route × date), upsert valid+stamped rows, then ship the
    ``scrape_run`` event. Aborts the run after the scraper raises ScraperBlockedError
    (rows already upserted persist). Always tears the scraper + DB connection down. Returns the
    structured outcome used by entrypoints to choose their final process exit code.

    ``time_budget_s`` (default ``CRON_TIME_BUDGET_S`` env / 45 min) bounds wall-clock: the loop
    checks it *between* routes (never mid-route) and stops cleanly once reached, so a long shard
    can't be hard-cancelled by the GitHub-Actions job timeout. Unreached routes stay due (in queue
    mode they're simply never marked); the ``stopped_early`` metric flag records when this fires.

    Two modes:
      * **On-demand** (``route_jobs is None``): iterate ``pairs`` (``(origin, dest)`` tuples),
        no queue marking — unchanged legacy behaviour.
      * **Cron queue mode** (``route_jobs`` given): iterate the scored ``RouteJob``s; after each
        route's NON-BLOCKED window scrape, mark it adaptively (change detection + AIMD cadence) so
        stable routes back off and volatile ones stay hot. A blocked route is NEVER marked (stays
        due). ``routes_unchanged`` (cheapest-by-cabin unchanged vs the prior scrape) is tracked and
        added to the metric.
    """
    from config.settings import SCRAPER_BLOCK_COOLDOWN_MIN, PriorityTier
    from pipeline.normalizer import filter_valid, stamp_expiry
    from pipeline.obs import ship_metric
    from pp_db.autocommit import close_connection, upsert_flights
    from scrapers.base import RequestStats, ScraperBlockedError

    queue_mode = route_jobs is not None
    blocked_route = None
    blocked_airline = None
    blocked_backoff_min = None
    queue_selected_routes = None
    queue_left_due_estimate = None
    queue_fill_ratio = None
    if queue_mode:
        from pipeline.queue_manager import QueueManager
        from pipeline.scoring import cheapest_by_cabin

        qm = QueueManager(scraper=None)
        iterable = list(route_jobs)
        first_job = iterable[0] if iterable else None
        if first_job and first_job.queue_due_count:
            due_count = first_job.queue_due_count
        else:
            due_count = len(iterable)
        try:
            queue_selected_routes = len(iterable)
            queue_left_due_estimate = max(0, due_count - queue_selected_routes)
            queue_fill_ratio = round(queue_selected_routes / due_count, 2) if due_count else None
        except Exception:  # noqa: BLE001
            queue_selected_routes = None
            queue_left_due_estimate = None
            queue_fill_ratio = None
    else:
        iterable = [_PairJob(o, d) for o, d in pairs]
        due_count = len(pairs)

    budget_s = _default_time_budget_s() if time_budget_s is None else time_budget_s
    started = time.monotonic()
    total = error_count = routes_scraped = routes_unchanged = routes_zero = 0
    routes_with_progress = 0
    blocked = stopped_early = False
    try:
        for job in iterable:
            if blocked:
                break
            if time.monotonic() - started >= budget_s:
                stopped_early = True
                assigned_count = len(iterable)
                logger.warning(
                    "time budget %.0fs reached after %d/%d assigned routes "
                    "(%d total due backlog) — stopping cleanly (unreached routes stay due)",
                    budget_s,
                    routes_scraped,
                    assigned_count,
                    due_count,
                )
                break
            origin, dest = job.origin, job.dest
            route_recs: list = []
            route_made_progress = False
            for travel in dates:
                try:
                    recs = scraper.scrape(origin, dest, travel)
                except ScraperBlockedError as exc:
                    logger.warning("blocked (%s) — aborting run (rows so far persist)", exc)
                    blocked = True
                    if queue_mode:
                        blocked_route = f"{origin}-{dest}"
                        blocked_airline = job.airline
                        blocked_backoff_min = SCRAPER_BLOCK_COOLDOWN_MIN
                        try:
                            qm.mark_blocked(
                                job, datetime.now(timezone.utc), SCRAPER_BLOCK_COOLDOWN_MIN
                            )
                        except Exception as mark_exc:  # noqa: BLE001
                            logger.error(
                                "Error recording block for %s→%s: %s", origin, dest, mark_exc
                            )
                            error_count += 1
                    break
                except Exception as exc:  # noqa: BLE001 — one route/date must not sink the run
                    logger.error("Error scraping %s→%s %s: %s", origin, dest, travel, exc)
                    error_count += 1
                    continue
                stamped = stamp_expiry(filter_valid(recs), _tier_for_job(job, PriorityTier.MED))
                if stamped:
                    upsert_flights(stamped)
                    route_recs.extend(stamped)
                    total += len(stamped)
                # A parsed valid-empty response is still meaningful progress. Set this only after
                # normalization/upsert succeeds so transport, parser, and DB failures cannot turn
                # an all-error route into a false success.
                route_made_progress = True
            if blocked:
                break  # do NOT mark — the blocked route stays due
            routes_scraped += 1
            if route_made_progress:
                routes_with_progress += 1
            if not route_recs:
                routes_zero += 1
            if queue_mode and route_made_progress:
                try:
                    now = datetime.now(timezone.utc)
                    changed = qm.mark_scraped(job, cheapest_by_cabin(route_recs), now)
                    if not changed:
                        routes_unchanged += 1
                except Exception as exc:  # noqa: BLE001 — report queue/DB failures honestly
                    logger.error("Error marking %s→%s scraped: %s", origin, dest, exc)
                    error_count += 1
            logger.info("%s→%s: %d records", origin, dest, len(route_recs))
    except Exception as exc:  # noqa: BLE001 — still emit the failed run metric
        logger.exception("Unexpected scrape-run failure: %s", exc)
        error_count += 1
    finally:
        try:
            scraper.close()
        except Exception as exc:  # noqa: BLE001
            logger.error("Scraper cleanup failed: %s", exc)
            error_count += 1
        try:
            close_connection()
        except Exception as exc:  # noqa: BLE001
            logger.error("Database cleanup failed: %s", exc)
            error_count += 1

    request_stats = scraper.request_stats() if hasattr(scraper, "request_stats") else RequestStats()
    upstream_requests = request_stats.attempts

    if blocked:
        status: ScrapeStatus = "blocked"
    elif error_count and routes_with_progress == 0:
        status = "failed"
    elif error_count or stopped_early:
        status = "partial"
    else:
        # Includes an empty shard assignment: matrix shards with no selected work are healthy
        # no-ops, not failed scrapes.
        status = "healthy"

    outcome = ScrapeOutcome(
        status=status,
        due_routes=due_count,
        records=total,
        errors=error_count,
        routes_scraped=routes_scraped,
        routes_unchanged=routes_unchanged,
        routes_zero=routes_zero,
        blocked=blocked,
        stopped_early=stopped_early,
        upstream_requests=upstream_requests,
    )

    duration_s = round(time.monotonic() - started, 1)
    freshness_snapshot = freshness(source, logger)
    # A healthy process alone does not prove ingestion health. Preserve the
    # validation signal on the run event so Grafana can alert on missing runs.
    data_health_confirmed = status == "healthy" and (
        routes_with_progress > 0 or _has_fresh_source_data(freshness_snapshot, source)
    )
    ship_metric(
        {
            "event": "scrape_run",
            "service": service,
            "airline": airline,
            "due_routes": due_count,
            "routes_scraped": routes_scraped,
            "routes_unchanged": routes_unchanged,
            "routes_zero": routes_zero,
            "records": total,
            "errors": error_count,
            "upstream_requests": upstream_requests,
            "duration_s": duration_s,
            "blocked": blocked,
            "stopped_early": stopped_early,
            "blocked_route": blocked_route,
            "blocked_airline": blocked_airline,
            "blocked_backoff_min": blocked_backoff_min,
            "status": status,
            "data_health_confirmed": data_health_confirmed,
            "queue_selected_routes": queue_selected_routes,
            "queue_left_due_estimate": queue_left_due_estimate,
            "queue_fill_ratio": queue_fill_ratio,
            **freshness_snapshot,
        }
    )
    logger.info(
        "=== done — status=%s %d %s records "
        "(routes=%d unchanged=%d errors=%d blocked=%s) in %ss ===",
        status, total, source, routes_scraped, routes_unchanged, error_count, blocked, duration_s,
    )
    return outcome
