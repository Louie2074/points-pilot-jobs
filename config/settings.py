"""
Global settings, loaded from environment variables via .env.

All tunable constants live here — nothing is hard-coded elsewhere.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

# Load .env from project root (safe to call multiple times)
load_dotenv(Path(__file__).parent.parent / ".env")


def _require(key: str) -> str:
    """Raise a clear error if a required env var is missing."""
    val = os.getenv(key)
    if not val:
        raise RuntimeError(
            f"Missing required environment variable: {key}\n"
            f"Copy .env.example to .env and fill in your values."
        )
    return val


def _get(key: str, default: str) -> str:
    return os.getenv(key, default)


# ---------------------------------------------------------------------------
# MotherDuck
# ---------------------------------------------------------------------------
# MOTHERDUCK_TOKEN is auto-read by the duckdb package — just ensure it's set.
MOTHERDUCK_TOKEN: str = _require("MOTHERDUCK_TOKEN")
MOTHERDUCK_DB: str = "md:point_pilot"

# ---------------------------------------------------------------------------
# Scraping behaviour
# ---------------------------------------------------------------------------
# Minimum seconds between requests. Paced on EVERY request as uniform(delay, 2×delay).
# Bumped 2→6: a gentler sustained rate is far less likely to trip Alaska's volume-based WAF.
SCRAPER_MIN_DELAY_S: float = float(_get("SCRAPER_MIN_DELAY_S", "6.0"))
SCRAPER_MAX_RETRIES: int = 4
# Consecutive 403/406 responses (WAF blocks) before the scraper aborts the run
# and backs off, instead of hammering a banned IP.
SCRAPER_BLOCK_THRESHOLD: int = int(_get("SCRAPER_BLOCK_THRESHOLD", "6"))
# Upper bound (seconds) on the per-request escalating cool-down after a 403/406.
SCRAPER_COOLDOWN_MAX_S: float = float(_get("SCRAPER_COOLDOWN_MAX_S", "300"))
SCRAPE_DAYS_AHEAD: int = int(_get("SCRAPE_DAYS_AHEAD", "30"))
ON_DEMAND_SCRAPE_DAYS: int = int(_get("ON_DEMAND_SCRAPE_DAYS", "30"))  # used by POST /v1/search
# Date-window sampling (background scheduler only). Scrape every day for the first
# SCRAPE_DENSE_DAYS, then every SCRAPE_SPARSE_STEP days out to SCRAPE_DAYS_AHEAD.
# Cuts requests-per-route (~33% at the defaults) → lower WAF pressure on a single IP.
# Near-term dates are kept dense because that's where most award booking happens.
SCRAPE_DENSE_DAYS: int = int(_get("SCRAPE_DENSE_DAYS", "14"))
SCRAPE_SPARSE_STEP: int = int(_get("SCRAPE_SPARSE_STEP", "3"))

# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------
# Award-refresh cadence (minutes). All live jobs (both award refreshes + the cash refresh)
# run on this same interval, staggered onto one worker — see pipeline/scheduler.py.
SCHEDULER_REFRESH_INTERVAL_MIN: int = int(_get("SCHEDULER_REFRESH_INTERVAL_MIN", "180"))
# After a run aborts on AlaskaBlockedError, suppress all scraping for this many minutes so a
# retry doesn't walk straight back into the ban. With the 180-min interval the next tick is
# already well past this, so it mainly matters if the interval is lowered.
SCRAPER_BLOCK_COOLDOWN_MIN: int = int(_get("SCRAPER_BLOCK_COOLDOWN_MIN", "90"))
# Force a full re-scrape of every route on startup (resets all next_scrape_at to now).
# Default OFF: the scheduler already picks up genuinely-due routes on its first run, so
# a blanket reset only re-scrapes fresh data and fires a burst that trips Alaska's WAF.
# Turn on for a one-off full refresh.
FORCE_RESCRAPE_ON_START: bool = _get("FORCE_RESCRAPE_ON_START", "false").lower() in (
    "1",
    "true",
    "yes",
)

# ---------------------------------------------------------------------------
# Observability
# ---------------------------------------------------------------------------
# Better Stack heartbeat ping URL — the refresh job pings this after each run so
# a dead scraper is detected (period 60m / grace 20m). Unset = no-op.
SCRAPER_HEARTBEAT_URL: str = _get("SCRAPER_HEARTBEAT_URL", "")
# Emit one structured `scrape_route` metric per route per run (records upserted,
# dates covered/failed, per-cabin breakdown) so coverage gaps and per-route drops
# are queryable in Better Stack. ON by default; set false to cut metric volume.
SCRAPER_EMIT_ROUTE_METRICS: bool = _get("SCRAPER_EMIT_ROUTE_METRICS", "true").lower() in (
    "1",
    "true",
    "yes",
)


# ---------------------------------------------------------------------------
# Priority tiers
# ---------------------------------------------------------------------------
class PriorityTier:
    HIGH = "HIGH"
    MED = "MED"
    LOW = "LOW"

    # Search-count thresholds for tier promotion
    PROMOTE_TO_MED: int = 3
    PROMOTE_TO_HIGH: int = 10


# Re-scrape intervals per tier (hours). Used to decide WHEN the scheduler
# refreshes a route; the row's expires_at column is stamped with the same
# interval but the API no longer filters by it — see db/queries.get_flights.
# Lengthened (4/12/24 → 8/24/48): a full pass over all routes already takes
# several hours on one paced IP, so the old HIGH=4h TTL was unachievable and just
# kept every route perpetually "due". Honest TTLs cut redundant re-scrape volume.
TTL_HOURS: dict[str, int] = {
    PriorityTier.HIGH: int(_get("TTL_HIGH_H", "8")),
    PriorityTier.MED: int(_get("TTL_MED_H", "24")),
    PriorityTier.LOW: int(_get("TTL_LOW_H", "48")),
}

# ---------------------------------------------------------------------------
# Alaska cash-fare scraper (CPP) — scraper-only; runs on the shared 3h cadence (staggered)
# ---------------------------------------------------------------------------
CASH_TTL_HOURS: int = int(_get("CASH_TTL_HOURS", "48"))
CASH_TOP_ROUTES: int = int(_get("CASH_TOP_ROUTES", "20"))
CASH_REFRESH_INTERVAL_MIN: int = int(_get("CASH_REFRESH_INTERVAL_MIN", "180"))
CASH_SCRAPE_DAYS: int = int(_get("CASH_SCRAPE_DAYS", "7"))

# ---------------------------------------------------------------------------
# Adaptive scheduling (vendored from scraper; Phase 2 cron unification)
# ---------------------------------------------------------------------------
# Cap on inline on-demand scrape dates (imported by queue_manager).
MAX_INLINE_SCRAPE_DATES: int = int(_get("MAX_INLINE_SCRAPE_DATES", "5"))
DEMAND_HALF_LIFE_DAYS: float = float(_get("DEMAND_HALF_LIFE_DAYS", "14"))
CHANGE_RATE_ALPHA: float = float(_get("CHANGE_RATE_ALPHA", "0.3"))
CHANGE_RATE_SEED: float = float(_get("CHANGE_RATE_SEED", "0.5"))
DEMAND_REF: float = float(_get("DEMAND_REF", "10"))
SCORE_W_DEMAND: float = float(_get("SCORE_W_DEMAND", "0.5"))
SCORE_W_OVERDUE: float = float(_get("SCORE_W_OVERDUE", "0.3"))
SCORE_W_CHANGE: float = float(_get("SCORE_W_CHANGE", "0.2"))
SCORE_FETCH_MULTIPLE: int = int(_get("SCORE_FETCH_MULTIPLE", "4"))

CADENCE_BOUNDS_H: dict[str, tuple[int, int]] = {
    PriorityTier.HIGH: (int(_get("CADENCE_HIGH_LO_H", "8")), int(_get("CADENCE_HIGH_HI_H", "24"))),
    PriorityTier.MED: (int(_get("CADENCE_MED_LO_H", "24")), int(_get("CADENCE_MED_HI_H", "72"))),
    PriorityTier.LOW: (int(_get("CADENCE_LOW_LO_H", "48")), int(_get("CADENCE_LOW_HI_H", "144"))),
}
CADENCE_STEP_H: dict[str, int] = {
    PriorityTier.HIGH: int(_get("CADENCE_STEP_HIGH_H", "8")),
    PriorityTier.MED: int(_get("CADENCE_STEP_MED_H", "24")),
    PriorityTier.LOW: int(_get("CADENCE_STEP_LOW_H", "48")),
}

# Cron per-shard leg cap (directed routes per shard per run). Sized below each airline's
# per-session WAF ceiling so shards × cap stays under it (e.g. Delta 3 shards × 9 ≈ 27).
CRON_MAX_LEGS_PER_SHARD: dict[str, int] = {
    "delta": int(_get("DELTA_MAX_LEGS_PER_SHARD", "9")),
    "southwest": int(_get("SOUTHWEST_MAX_LEGS_PER_SHARD", "20")),
    "turkish": int(_get("TURKISH_MAX_LEGS_PER_SHARD", "20")),
    "etihad": int(_get("ETIHAD_MAX_LEGS_PER_SHARD", "20")),
}
