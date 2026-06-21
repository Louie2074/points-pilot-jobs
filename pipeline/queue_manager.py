"""
Priority queue manager for the scraping pipeline.

Owns all logic for route tier management, promotion, and on-demand scraping.
The queue itself lives in the MotherDuck `routes_queue` table — persistent
across restarts with no in-memory state to lose.

Tier promotion thresholds (from settings):
  LOW  → MED  when search_count >= 3
  MED  → HIGH when search_count >= 10
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from config.settings import (
    CADENCE_BOUNDS_H,
    CADENCE_STEP_H,
    CHANGE_RATE_ALPHA,
    CHANGE_RATE_SEED,
    DEMAND_HALF_LIFE_DAYS,
    DEMAND_REF,
    MAX_INLINE_SCRAPE_DATES,
    SCORE_FETCH_MULTIPLE,
    SCORE_W_CHANGE,
    SCORE_W_DEMAND,
    SCORE_W_OVERDUE,
    SCRAPE_DAYS_AHEAD,
    TTL_HOURS,
    PriorityTier,
)
from pipeline import normalizer, scoring
from pp_db import autocommit as db
from scrapers.base import ScraperBlockedError

logger = logging.getLogger(__name__)


def _as_utc(ts: datetime | None) -> datetime | None:
    """Tag a naive DuckDB timestamp as UTC so it can be compared/subtracted against a tz-aware
    ``datetime.now(timezone.utc)`` (the table stores naive UTC). None passes through."""
    if ts is not None and ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts


def _seed_rate(x: float | None) -> float:
    """Default a missing change-rate to the seed (a fresh route has no observed change history)."""
    return CHANGE_RATE_SEED if x is None else x


def _stride_sample(dates: list[date], n: int) -> list[date]:
    """Pick at most ``n`` evenly-spaced dates from ``dates`` (order preserved, first and
    last always included). Returns ``dates`` unchanged when it already fits in ``n``."""
    if len(dates) <= n:
        return dates
    if n == 1:
        return [dates[0]]
    last = len(dates) - 1
    idxs = sorted({round(i * last / (n - 1)) for i in range(n)})
    return [dates[i] for i in idxs]


@dataclass
class RouteJob:
    """A single scraping job pulled from the priority queue."""

    origin: str
    dest: str
    airline: str
    tier: str
    search_count: int
    last_scraped_at: datetime | None
    next_scrape_at: datetime
    decayed_demand: float = 0.0
    last_search_at: datetime | None = None
    change_rate: float = CHANGE_RATE_SEED
    interval_h: float | None = None
    last_cheapest: str | None = None


class QueueManager:
    """
    Manages the routes_queue table as a durable priority queue.

    Public interface used by the scheduler and the LLM tool layer:
        add_route()          — idempotently add a route at a given tier
        get_due_batch()      — fetch routes ready to scrape
        mark_scraped()       — update a route after a successful scrape
        handle_user_search() — user query hook: increment count, promote, on-demand scrape
    """

    def __init__(self, scraper=None) -> None:
        """
        Args:
            scraper: Optional BaseScraper instance for on-demand scraping.
                     If None, on-demand scrapes in handle_user_search are skipped.
        """
        self._scraper = scraper

    # ---------------------------------------------------------------------------
    # Core queue operations
    # ---------------------------------------------------------------------------

    def add_route(
        self, origin: str, dest: str, tier: str = PriorityTier.LOW, airline: str = "alaska"
    ) -> None:
        """
        Add a route to the queue at the given tier (per airline). Idempotent — existing
        rows are not modified (so manual tier upgrades are preserved).
        """
        db.upsert_route(origin.upper(), dest.upper(), tier, airline=airline)
        logger.debug("Route added/confirmed: %s→%s [%s/%s]", origin, dest, airline, tier)

    def get_due_batch(self, limit: int = 50, airline: str | None = None) -> list[RouteJob]:
        """Return up to ``limit`` due routes, ordered by priority score (demand × overdue ×
        change-rate). Fetches SCORE_FETCH_MULTIPLE×limit most-overdue candidates from SQL, then
        score-sorts in Python and truncates — so the score reorders across more than just the
        single most-overdue slice. ``airline`` filters to one scraper's queue."""
        fetch = max(limit, limit * SCORE_FETCH_MULTIPLE)
        rows = db.get_due_routes(limit=fetch, airline=airline)
        now = datetime.now(timezone.utc)

        def _score(r: dict) -> float:
            tier = r["priority_tier"]
            lo = CADENCE_BOUNDS_H.get(tier, (TTL_HOURS[PriorityTier.LOW], 0))[0]
            interval_h = r["interval_h"] or lo
            eff_demand = scoring.decay_demand(
                r["decayed_demand"] or 0.0,
                _as_utc(r["last_search_at"]),
                now,
                DEMAND_HALF_LIFE_DAYS,
            )
            overdue = scoring.overdue_ratio(now, _as_utc(r["next_scrape_at"]), interval_h)
            return scoring.route_score(
                eff_demand,
                overdue,
                _seed_rate(r["change_rate"]),
                w_demand=SCORE_W_DEMAND,
                w_overdue=SCORE_W_OVERDUE,
                w_change=SCORE_W_CHANGE,
                demand_ref=DEMAND_REF,
            )

        rows.sort(key=_score, reverse=True)
        rows = rows[:limit]
        return [
            RouteJob(
                origin=r["origin"],
                dest=r["dest"],
                airline=r["airline"],
                tier=r["priority_tier"],
                search_count=r["search_count"],
                last_scraped_at=r["last_scraped_at"],
                next_scrape_at=r["next_scrape_at"],
                decayed_demand=r["decayed_demand"] or 0.0,
                last_search_at=r["last_search_at"],
                change_rate=_seed_rate(r["change_rate"]),
                interval_h=r["interval_h"],
                last_cheapest=r["last_cheapest"],
            )
            for r in rows
        ]

    def mark_scraped(self, job: RouteJob, new_cheapest: dict[str, int], now: datetime) -> bool:
        """Record an adaptive scrape outcome for ``job``. Detects whether the cheapest-points
        map moved vs the route's stored snapshot, updates the change-rate EWMA, applies the AIMD
        cadence within the tier's bounds, and persists. Returns whether the route changed.

        Call ONLY after a successful, non-blocked scrape — a block must leave the route due."""
        old_cheapest = json.loads(job.last_cheapest) if job.last_cheapest else None
        changed = scoring.did_change(new_cheapest, old_cheapest)
        new_rate = scoring.update_change_rate(job.change_rate, changed, CHANGE_RATE_ALPHA)
        lo, hi = CADENCE_BOUNDS_H.get(
            job.tier, (TTL_HOURS[PriorityTier.LOW], TTL_HOURS[PriorityTier.LOW])
        )
        step = CADENCE_STEP_H.get(job.tier, lo)
        prev_interval = job.interval_h or lo
        new_interval = scoring.update_interval(prev_interval, changed, lo=lo, hi=hi, step_h=step)
        next_scrape_at = now + timedelta(hours=new_interval)
        db.record_scrape_outcome(
            job.origin,
            job.dest,
            job.airline,
            interval_h=new_interval,
            change_rate=new_rate,
            last_cheapest=json.dumps(new_cheapest),
            next_scrape_at=next_scrape_at,
        )
        logger.debug(
            "Adaptive mark %s→%s [%s] changed=%s rate=%.2f interval=%.1fh",
            job.origin,
            job.dest,
            job.airline,
            changed,
            new_rate,
            new_interval,
        )
        return changed

    # ---------------------------------------------------------------------------
    # Tier promotion
    # ---------------------------------------------------------------------------

    def _maybe_promote(
        self, origin: str, dest: str, current_count: int, airline: str = "alaska"
    ) -> str | None:
        """
        Check if a route should be promoted based on search_count (per airline).
        Returns the new tier if promoted, else None.
        """
        route = db.get_route(origin, dest, airline=airline)
        if not route:
            return None

        current_tier = route["priority_tier"]
        new_tier = None

        if current_tier == PriorityTier.LOW and current_count >= PriorityTier.PROMOTE_TO_MED:
            new_tier = PriorityTier.MED
        elif current_tier == PriorityTier.MED and current_count >= PriorityTier.PROMOTE_TO_HIGH:
            new_tier = PriorityTier.HIGH

        # Alaska runs DAILY — every Alaska route is MED and must stay capped there. Its queue is
        # large and the shared single-IP scheduler is near its daily throughput ceiling, so a
        # MED→HIGH promotion (3×/day refresh) would oversubscribe the worker. LOW→MED still
        # applies, so on-demand Alaska routes still become daily. (2026-06-14 expansion.)
        if airline == "alaska" and new_tier == PriorityTier.HIGH:
            new_tier = None

        if new_tier:
            db.set_route_tier(origin, dest, new_tier, airline=airline)
            logger.info(
                "Route promoted: %s→%s %s → %s (search_count=%d)",
                origin,
                dest,
                current_tier,
                new_tier,
                current_count,
            )

        return new_tier

    # ---------------------------------------------------------------------------
    # User search hook (called by the LLM tool layer)
    # ---------------------------------------------------------------------------

    def handle_user_search(
        self,
        origin: str,
        dest: str,
        dates: list[date] | None = None,
    ) -> list:
        """
        Called when a user queries a route. Does three things:
        1. Adds route if not present (as LOW tier).
        2. Increments search_count; promotes tier if thresholds met.
        3. Scrapes any requested dates we don't already have FRESH data for.

        Args:
            origin: IATA origin code
            dest:   IATA destination code
            dates:  Optional list of specific dates to scrape on-demand.
                    If None, scrapes the next SCRAPE_DAYS_AHEAD days.

        Returns:
            List of FlightRecord from the on-demand scrape (may be empty).
        """
        origin = origin.upper()
        dest = dest.upper()
        # The route queue is per-airline; key it on this scraper's slug.
        airline = getattr(self._scraper, "source", None) or "alaska"

        # Step 1: ensure route exists
        self.add_route(origin, dest, PriorityTier.LOW, airline=airline)

        # Step 2: increment + maybe promote
        new_count = db.increment_search_count(origin, dest, airline=airline)
        db.bump_decayed_demand(
            origin, dest, airline, datetime.now(timezone.utc), DEMAND_HALF_LIFE_DAYS
        )
        route = db.get_route(origin, dest, airline=airline)
        tier = route["priority_tier"] if route else PriorityTier.LOW
        # _maybe_promote bumps the tier as a side-effect (db.set_route_tier) and returns the
        # new tier (or None). The on-demand path is demand-only — the scheduler owns cadence —
        # so we only use the tier to stamp the freshness TTL on any scraped rows below.
        effective_tier = self._maybe_promote(origin, dest, new_count, airline=airline) or tier

        # Step 3: figure out exactly which dates we still need to scrape.
        # We scrape a date if we don't already have any fresh row for it
        # (expires_at > now()). This means even if the route is overall
        # 'fresh' it'll still scrape dates the user asked about that
        # haven't been covered yet (e.g. they want June 6 but the prior
        # on-demand only went through June 5).
        results = []
        if self._scraper:
            requested_dates = dates or [
                datetime.now(timezone.utc).date() + timedelta(days=i)
                for i in range(SCRAPE_DAYS_AHEAD)
            ]
            covered_dates = db.get_fresh_scrape_dates(origin, dest, source=airline)
            missing_dates = [d for d in requested_dates if d not in covered_dates]

            if len(missing_dates) > MAX_INLINE_SCRAPE_DATES:
                logger.info(
                    "On-demand scrape for %s→%s — capping %d missing dates to %d (strided)",
                    origin,
                    dest,
                    len(missing_dates),
                    MAX_INLINE_SCRAPE_DATES,
                )
                missing_dates = _stride_sample(missing_dates, MAX_INLINE_SCRAPE_DATES)

            if missing_dates:
                logger.info(
                    "On-demand scrape for %s→%s — %d/%d requested dates missing fresh data",
                    origin,
                    dest,
                    len(missing_dates),
                    len(requested_dates),
                )
                blocked = False
                for d in missing_dates:
                    try:
                        raw_records = self._scraper.scrape(origin, dest, d)
                    except ScraperBlockedError:
                        # WAF block — stop scraping and return what we have rather
                        # than 500ing the user's request or hammering a banned IP.
                        logger.warning(
                            "On-demand scrape for %s→%s blocked by Alaska — returning partial data",
                            origin,
                            dest,
                        )
                        blocked = True
                        break
                    valid = normalizer.filter_valid(raw_records)
                    stamped = normalizer.stamp_expiry(valid, effective_tier)
                    if stamped:
                        db.upsert_flights(stamped)
                        results.extend(stamped)

                logger.info(
                    "On-demand scrape complete: %s→%s — %d records%s",
                    origin,
                    dest,
                    len(results),
                    " (blocked)" if blocked else "",
                )

        return results

    # ---------------------------------------------------------------------------
    # Seeding
    # ---------------------------------------------------------------------------

    def seed_from_config(self) -> int:
        """
        Seed the queue from config/routes.py. Idempotent.
        Returns the number of route entries added.
        """
        from config.routes import all_seeded_routes

        routes = all_seeded_routes()
        for origin, dest, airline, tier in routes:
            self.add_route(origin, dest, tier, airline=airline)

        logger.info("Queue seeded with %d route entries from config", len(routes))
        return len(routes)
