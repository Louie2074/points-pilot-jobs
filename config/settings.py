"""
Global settings, loaded from environment variables via .env.

All tunable constants live here — nothing is hard-coded elsewhere.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

# Load .env from project root (safe to call multiple times)
load_dotenv(Path(__file__).parent.parent / ".env")


def _get(key: str, default: str) -> str:
    return os.getenv(key, default)


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
# Separate heartbeat for the Google Flights cash scraper (its own Fly app). Unset = no-op.
GFLIGHTS_HEARTBEAT_URL: str = _get("GFLIGHTS_HEARTBEAT_URL", "")
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
# Google Flights cash-fare scraper (CPP) — sharded GH-Actions cron (cash_browser_scrape.py)
# ---------------------------------------------------------------------------
# Cash freshness TTL (hours). 72 (was 48) cuts the per-unit re-scrape rate by a third so the
# whole coverable universe fits current throughput across the full 30-day horizon (the 15-30d
# tail was starved at 48h). Cash fares drift little day-to-day, so 72h is fine for a CPP signal;
# pair with a freshness label in the UI. 96h is held in reserve if the universe outgrows capacity.
CASH_TTL_HOURS: int = int(_get("CASH_TTL_HOURS", "72"))
CASH_TOP_ROUTES: int = int(_get("CASH_TOP_ROUTES", "80"))
CASH_REFRESH_INTERVAL_MIN: int = int(_get("CASH_REFRESH_INTERVAL_MIN", "180"))
# Minimum rest (minutes) between cash runs when sleeping a fixed period. Floors the post-run
# sleep so an over-long run can't make the next run start back-to-back (a new regime; Google has
# no WAF wall here, but we still want a breather).
CASH_MIN_REST_MIN: int = int(_get("CASH_MIN_REST_MIN", "10"))
# Cash horizon (days ahead). Awards are searched up to ~30 days out densely (beyond that the
# award scrapers sample only a sparse tail), so 30 covers essentially the whole matchable award
# window — vs 7, which left every search >1 week out with no CPP. The eligible universe (~2.4k
# economy-nonstop route/dates) still fits the gflights ceiling: 48h TTL ⇒ ~1.2k scrapes/day vs
# ~1.6k/day capacity (CASH_TOP_ROUTES=400 × 4 runs); the query orders by date ASC so near-term
# stays freshest and the tail fills with leftover slots.
CASH_SCRAPE_DAYS: int = int(_get("CASH_SCRAPE_DAYS", "30"))
# Cash↔award departure-time match tolerance (minutes). Most carriers match to the minute, but
# Delta's Google time runs ~10-30 min off the award time, so 20 captures that skew while staying
# well under the inter-flight gap (~60 min). Same-carrier nonstop siblings average 112-472 min
# apart (so a wrong second flight almost never sits inside 20 min); ties break deterministically
# in cash_matcher. A systematic tz bug still surfaces as 0 matches, not silent wrong matches.
CASH_MATCH_TOLERANCE_MIN: int = int(_get("CASH_MATCH_TOLERANCE_MIN", "20"))
# Negative memory: a (route,date) that yielded ZERO matchable cash is skipped for this many days
# before it's re-probed, so zero-yield pairs stop consuming a scrape slot every run.
CASH_ZERO_REPROBE_DAYS: int = int(_get("CASH_ZERO_REPROBE_DAYS", "3"))
# Cabins the cash scraper covers. economy/business/first via the NL text query ("First class …"
# for first); premium economy via the tfs protobuf (NL can't select it). Premium economy AND first
# are the large, low-yield cabins, so they are DEMOTED (scraped every CASH_PE_EVERY_N-th run) to
# fit the existing capacity rather than ~doubling per-run load.
CASH_CABINS: tuple[str, ...] = tuple(
    c.strip()
    for c in _get("CASH_CABINS", "economy,business,premium_economy,first").split(",")
    if c.strip()
)
# Demote the slow cabins (premium economy + first): scrape them only every Nth cash run (not every
# run). They are the slowest, lowest-yield cabins, so this frees slots for the econ/biz tail
# without zeroing their coverage. 1 = every run (no demotion). See cabins_for_run.
CASH_PE_EVERY_N: int = int(_get("CASH_PE_EVERY_N", "4"))

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

# Cron per-shard leg cap (directed routes per shard per run). Each shard runs on a FRESH
# GH-Actions runner IP, so this is the per-shard (per-IP, per-session) limit, sized below each
# airline's per-session WAF ceiling — NOT a budget shared across shards. Delta's ceiling is ~27
# directed legs/session (a live run blocked on leg 28); 18 keeps a ~10-leg margin × 3 shards =
# 54 legs/day. Env-overridable so it can be dialed back from the `blocked` metric without a deploy.
CRON_MAX_LEGS_PER_SHARD: dict[str, int] = {
    "delta": int(_get("DELTA_MAX_LEGS_PER_SHARD", "18")),
    # Southwest's F5/Shape WAF blocks by IP REPUTATION, not leg count: ~2/3 of GH-Actions Azure IPs
    # get 403'd within a few routes, ~1/3 run clean (validated 2026-06-19 — shards blocked at 3 / 5
    # routes while a third did its full cap unblocked). So the cap only bounds the CLEAN shards;
    # keep it generous (20) to maximize their coverage. The 3-shard fan-out is the real mitigation
    # (more IP draws → better odds of a clean one). A 10 cap was tried and REDUCED coverage (18 vs 36).
    "southwest": int(_get("SOUTHWEST_MAX_LEGS_PER_SHARD", "20")),
    "turkish": int(_get("TURKISH_MAX_LEGS_PER_SHARD", "20")),
    "etihad": int(_get("ETIHAD_MAX_LEGS_PER_SHARD", "20")),
    # Alaska: 115 MED pairs (230 directed legs after the POI-20 lever #3 partner-business intl
    # expansion). 40/shard × 3 shards = 120 candidate pool/run; the 3×/day cron + never-scraped
    # floor (queue_manager.get_due_batch) drains the catalogue with margin. httpx headroom is wide
    # (40 legs × 29 dates ≈ 1160 req/IP/run runs clean — Azure probe 0 blocks).
    "alaska": int(_get("ALASKA_MAX_LEGS_PER_SHARD", "40")),
    # JetBlue: 46 MED pairs (92 directed legs after the Mint business expansion, POI-20 lever #3).
    # 36/shard × 4 shards = 144 candidate pool/run covers the 92 directed set plus the LOW
    # on-demand tail with margin after the measured four-shard Actions density bump.
    # 36 legs × 24 dates ≈ 864 req/IP/run — below Alaska's clean 1160, so WAF-safe on httpx.
    "jetblue": int(_get("JETBLUE_MAX_LEGS_PER_SHARD", "36")),
}
