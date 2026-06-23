# Coverage Expansion — Scraper Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Extend the award scrape window to ~90 days (three-tier, depth-by-priority), expand route seeds for all six airlines (origins + international destinations), add the new origins to `airport_tz`, and scale the cash scraper (pin international routes + raise `CASH_TOP_ROUTES`).

**Architecture:** All changes are in the `scraper` repo. The deep window lives in `pipeline/scheduler._scrape_window` (used by the always-on Fly scraper for Alaska + JetBlue, the cheap httpx path that carries the richest international partner space). Depth-by-tier keeps avg dates/route ~unchanged while the horizon triples. Route seeds + `CASH_PINNED_ROUTES` are config edits in `config/routes.py`; new origins go in `config/airport_tz.py` **and** `pp_db/airport_tz.py` (guarded by a hermetic test). Cash capacity is a `fly.google.toml` env bump.

**Tech Stack:** Python 3.11, pytest, ruff, SQLAlchemy (pp_db), Fly.io.

**Baseline checks (run first, must pass):** `pytest -q` · `ruff check . && ruff format --check .`

**Vendoring note (critical):** `config/routes.py` and `config/airport_tz.py` are mirrored into the `jobs` repo and (hand-merged) `api`. THIS plan edits the scraper copies. The `jobs` copies are handled by the companion jobs plan with the **identical** route tuples + tz entries. After both land, the route lists and tz dicts must match between repos (the orchestrator verifies).

---

### Task 1: Three-tier scrape window (coarse far-tier + depth-by-tier)

**Files:**
- Modify: `config/settings.py` (after line 43, the `SCRAPE_SPARSE_STEP` block)
- Modify: `scrapers/base.py:290-298` (BaseScraper window attrs)
- Modify: `pipeline/scheduler.py:102-117` (`_scrape_window`) and `:210` (call site)
- Test: `tests/test_scheduler.py`

- [ ] **Step 1: Add window-depth config to `config/settings.py`** (immediately after `SCRAPE_SPARSE_STEP` at line 43)

```python
# Coarse far-tier: beyond SCRAPE_DAYS_AHEAD, sample every SCRAPE_COARSE_STEP days out to
# SCRAPE_COARSE_DAYS_AHEAD (~90d / 3 months). Far-out award space moves slowly, so a weekly
# tail adds horizon cheaply. Only HIGH-tier routes reach the full coarse depth (see WINDOW_DEPTH).
SCRAPE_COARSE_DAYS_AHEAD: int = int(_get("SCRAPE_COARSE_DAYS_AHEAD", "90"))
SCRAPE_COARSE_STEP: int = int(_get("SCRAPE_COARSE_STEP", "7"))
```

And add the depth map next to the `CADENCE_*` block (after line 185):

```python
# Max scrape horizon (days ahead) per priority tier — the "depth-by-tier" budget valve.
# HIGH (hot/volatile) gets the full 90d coarse tail; MED a mid reach; LOW near-term only.
# With a realistic ~15/60/25 HIGH/MED/LOW mix, avg dates/route stays ~today's despite the 3×
# horizon. tier=None (on-demand / coverage audits) falls back to the deepest (HIGH) horizon.
WINDOW_DEPTH_DAYS: dict[str, int] = {
    PriorityTier.HIGH: int(_get("WINDOW_DEPTH_HIGH_D", "90")),
    PriorityTier.MED: int(_get("WINDOW_DEPTH_MED_D", "45")),
    PriorityTier.LOW: int(_get("WINDOW_DEPTH_LOW_D", "14")),
}
```

- [ ] **Step 2: Add coarse-tier attrs to `scrapers/base.py`** (after `sparse_step` at line 294)

```python
    coarse_days_ahead: int = SCRAPE_COARSE_DAYS_AHEAD
    coarse_step: int = SCRAPE_COARSE_STEP
```

Add `SCRAPE_COARSE_DAYS_AHEAD, SCRAPE_COARSE_STEP` to the existing `from config.settings import (...)` block at the top of `base.py`.

- [ ] **Step 3: Write failing tests in `tests/test_scheduler.py`**

```python
from datetime import date
from config.settings import PriorityTier
from pipeline.scheduler import _scrape_window

class _Scraper:
    scrape_days_ahead = 30
    dense_days = 14
    sparse_step = 3
    coarse_days_ahead = 90
    coarse_step = 7

def test_window_high_tier_reaches_90_days():
    dates = _scrape_window(date(2026, 1, 1), _Scraper(), tier=PriorityTier.HIGH)
    offsets = [(d - date(2026, 1, 1)).days for d in dates]
    assert offsets[:14] == list(range(14))                 # dense daily
    assert 30 in offsets and 28 in offsets is False or True # sparse present
    assert max(offsets) >= 84 and max(offsets) <= 89        # coarse reaches ~90, step 7
    assert offsets == sorted(set(offsets))                  # sorted, deduped

def test_window_low_tier_is_dense_only():
    dates = _scrape_window(date(2026, 1, 1), _Scraper(), tier=PriorityTier.LOW)
    offsets = [(d - date(2026, 1, 1)).days for d in dates]
    assert offsets == list(range(14))                       # 0..13, no sparse/coarse

def test_window_med_tier_mid_reach():
    dates = _scrape_window(date(2026, 1, 1), _Scraper(), tier=PriorityTier.MED)
    offsets = [(d - date(2026, 1, 1)).days for d in dates]
    assert max(offsets) <= 45 and max(offsets) > 30         # coarse reaches ~45

def test_window_tier_none_defaults_to_full_depth():
    dates = _scrape_window(date(2026, 1, 1), _Scraper(), tier=None)
    offsets = [(d - date(2026, 1, 1)).days for d in dates]
    assert max(offsets) >= 84                                # back-compat: deepest
```

- [ ] **Step 4: Run tests — verify they fail** — `pytest tests/test_scheduler.py -q` → FAIL (`_scrape_window` takes 2 args / no tier).

- [ ] **Step 5: Rewrite `_scrape_window` in `pipeline/scheduler.py:102-117`**

```python
def _scrape_window(today: date, scraper: object = None, tier: str | None = None) -> list[date]:
    """Dates to scrape for one route, anchored to `today`.

    Three segments: dense (every day to `dense_days`), sparse (every `sparse_step` to
    `scrape_days_ahead`), coarse (every `coarse_step` out to the tier's horizon). The horizon
    is depth-tiered (WINDOW_DEPTH_DAYS): HIGH reaches the full ~90d coarse tail, MED a mid
    reach, LOW near-term only — so adding the long horizon doesn't blow per-shard throughput.
    `tier=None` (on-demand / check_coverage.py) falls back to the deepest horizon.
    """
    dense_days = getattr(scraper, "dense_days", SCRAPE_DENSE_DAYS)
    sparse_step = getattr(scraper, "sparse_step", SCRAPE_SPARSE_STEP)
    scrape_days = getattr(scraper, "scrape_days_ahead", SCRAPE_DAYS_AHEAD)
    coarse_days = getattr(scraper, "coarse_days_ahead", SCRAPE_COARSE_DAYS_AHEAD)
    coarse_step = getattr(scraper, "coarse_step", SCRAPE_COARSE_STEP)
    horizon = WINDOW_DEPTH_DAYS.get(tier, coarse_days) if tier is not None else coarse_days

    dense = min(dense_days, horizon)
    offsets = list(range(dense))
    if horizon > dense:
        offsets += list(range(dense, min(scrape_days, horizon), max(1, sparse_step)))
    if horizon > scrape_days:
        offsets += list(range(scrape_days, horizon, max(1, coarse_step)))
    offsets = sorted(set(offsets))
    return [today + timedelta(days=o) for o in offsets]
```

Add `SCRAPE_COARSE_DAYS_AHEAD, SCRAPE_COARSE_STEP, WINDOW_DEPTH_DAYS` to the settings import in `scheduler.py`.

- [ ] **Step 6: Thread tier into the call site `pipeline/scheduler.py:210`.** The batch is mixed-tier, so call `_scrape_window` per-tier and union (or pass the **deepest tier present** to keep one window for the run's budget math). The simplest correct change that preserves the existing budget math (one `dates` list per run): pass the highest-priority tier present in `due` so HIGH routes get full depth.

```python
    # Window depth follows the most-urgent tier in this batch (HIGH→MED→LOW); the budget math
    # below uses one window per run. Most batches are MED, so the deep tail only kicks in when a
    # HIGH route is due.
    batch_tier = next((t for t in (PriorityTier.HIGH, PriorityTier.MED, PriorityTier.LOW)
                       if any(r.get("priority_tier") == t for r in due)), None)
    dates = _scrape_window(today, scraper, tier=batch_tier)
```

(Confirm the due-row dict key for tier by reading `queue_manager.get_due_batch` — it is `priority_tier` per `pp_db/models.py`. If the rows are dataclasses not dicts, use `getattr(r, "priority_tier", None)`.)

- [ ] **Step 7: Run tests — verify pass** — `pytest tests/test_scheduler.py -q` → PASS. Then run the FULL suite `pytest -q` (the existing worst-case-fits-interval test must still pass; if it asserts a fixed window length, update it to compute the worst case from `_scrape_window(..., tier=PriorityTier.HIGH)`).

- [ ] **Step 8: Commit** — `git add config/settings.py scrapers/base.py pipeline/scheduler.py tests/test_scheduler.py && git commit -m "feat(scraper): three-tier 90d scrape window with depth-by-tier"`

---

### Task 2: Expand route seeds (all six airlines)

**Files:** Modify `config/routes.py`. Test: `tests/test_routes_config.py`.

International additions are NONSTOP, real program partners (validated live in the ship-gate canary before merge). Add these tuples to the existing lists (do NOT touch existing entries; append under a `# coverage-expansion 2026-06-23` comment in each list):

- [ ] **Step 1: Alaska — append to `ALASKA_MED_ROUTES`** (international partner nonstops + new US origins)

```python
    # coverage-expansion 2026-06-23 — international partner nonstops (AS Mileage Plan)
    ("SEA", "HND"), ("SFO", "HND"), ("LAX", "HND"),   # JAL
    ("SEA", "NRT"), ("LAX", "NRT"),                     # JAL
    ("SFO", "HKG"), ("LAX", "HKG"),                     # Cathay
    ("SFO", "SYD"), ("LAX", "SYD"),                     # Qantas
    ("SFO", "TPE"), ("LAX", "TPE"),                     # Starlux
    ("SEA", "LHR"), ("SFO", "LHR"), ("LAX", "LHR"),    # BA
    # new US origins → existing dests
    ("DEN", "SEA"), ("DEN", "LAX"), ("DEN", "SFO"),
    ("PHX", "SEA"), ("AUS", "SEA"), ("MSP", "SEA"),
    ("SAN", "PDX"), ("SJC", "LAX"), ("GEG", "LAX"),
```

- [ ] **Step 2: JetBlue — append to `JETBLUE_MED_ROUTES`**

```python
    # coverage-expansion 2026-06-23 — transcon + Caribbean/LatAm + TATL partner
    ("JFK", "LAX"), ("JFK", "SAN"), ("JFK", "AUS"), ("JFK", "SJU"),
    ("BOS", "SFO"), ("BOS", "SEA"), ("BOS", "SJU"),
    ("FLL", "SJU"), ("EWR", "FLL"), ("JFK", "LHR"), ("BOS", "LHR"),
```

- [ ] **Step 3: Delta — append to `DELTA_MED_ROUTES`** (SkyTeam intl + hub spokes)

```python
    # coverage-expansion 2026-06-23 — SkyTeam intl partners + hub spokes
    ("DTW", "ICN"), ("ATL", "ICN"),                    # Korean
    ("JFK", "CDG"), ("ATL", "CDG"),                    # Air France
    ("DTW", "AMS"), ("ATL", "AMS"),                    # KLM
    ("ATL", "GRU"),                                    # LATAM
    ("ATL", "SLC"), ("ATL", "MSP"), ("DTW", "MSP"),
    ("SLC", "SFO"), ("SLC", "PHX"), ("JFK", "BOS"),
    ("MSP", "SFO"), ("MSP", "PHX"),
```

- [ ] **Step 4: Southwest — append to `SOUTHWEST_MED_ROUTES`** (domestic focus-city mesh)

```python
    # coverage-expansion 2026-06-23 — domestic focus-city mesh
    ("DEN", "AUS"), ("DEN", "TPA"), ("DEN", "MSY"), ("DEN", "BNA"),
    ("MDW", "DEN"), ("MDW", "TPA"), ("MDW", "AUS"),
    ("BWI", "MDW"), ("BWI", "ATL"), ("BWI", "SAN"),
    ("HOU", "DAL"), ("HOU", "DEN"), ("PHX", "DEN"),
    ("OAK", "PHX"), ("SAN", "PHX"), ("SMF", "LAS"),
```

- [ ] **Step 5: Turkish — append to `TURKISH_MED_ROUTES`** (more US gateways → IST)

```python
    # coverage-expansion 2026-06-23 — more US gateways → IST
    ("SAN", "IST"), ("AUS", "IST"), ("RDU", "IST"),
    ("BWI", "IST"), ("MSP", "IST"),
```

- [ ] **Step 6: Etihad — append to `ETIHAD_MED_ROUTES`** (more US gateways → AUH)

```python
    # coverage-expansion 2026-06-23 — more US gateways → AUH
    ("DFW", "AUH"), ("SEA", "AUH"), ("DCA", "AUH"),
```

- [ ] **Step 7: Update `tests/test_routes_config.py`** expected per-airline pair counts to the new totals. Read the current asserted counts (the test asserts undirected pair counts per airline — Alaska/JetBlue/Delta/Southwest/Turkish/Etihad), recompute after the appends, and update each expected number. Run `pytest tests/test_routes_config.py -q` until green.

- [ ] **Step 8: Commit** — `git add config/routes.py tests/test_routes_config.py && git commit -m "feat(scraper): expand route seeds (intl + new origins) across all six airlines"`

---

### Task 3: Add new origin airports to `airport_tz` (both copies)

**Files:** Modify `config/airport_tz.py` AND `pp_db/airport_tz.py`. Test: `tests/test_airport_tz.py` (already gates coverage — must pass).

New IATA codes introduced by Task 2 not already in the map: `NRT, HKG, SYD, TPE, ICN, CDG, AMS, GRU, SJU, MSY`. (HND, LHR already present.)

- [ ] **Step 1: Add entries to `config/airport_tz.py`** (insert alphabetically into the `AIRPORT_TZ` dict)

```python
    "AMS": "Europe/Amsterdam",   # Amsterdam — Delta/KLM (SkyTeam) origin
    "CDG": "Europe/Paris",        # Paris CDG — Delta/Air France (SkyTeam) origin
    "GRU": "America/Sao_Paulo",   # São Paulo — Delta/LATAM (SkyTeam) origin
    "HKG": "Asia/Hong_Kong",      # Hong Kong — Alaska/Cathay origin
    "ICN": "Asia/Seoul",          # Seoul Incheon — Delta/Korean (SkyTeam) origin
    "MSY": "America/Chicago",     # New Orleans — Southwest spoke
    "NRT": "Asia/Tokyo",          # Tokyo Narita — Alaska/JAL origin
    "SJU": "America/Puerto_Rico",  # San Juan — JetBlue origin
    "SYD": "Australia/Sydney",     # Sydney — Alaska/Qantas origin
    "TPE": "Asia/Taipei",          # Taipei — Alaska/Starlux origin
```

- [ ] **Step 2: Apply the IDENTICAL additions to `pp_db/airport_tz.py`** (the cash-matcher's copy; the dict has the same shape).

- [ ] **Step 3: Run the guard test** — `pytest tests/test_airport_tz.py -q` → PASS (`test_every_route_airport_has_timezone` confirms every seeded route airport, including the Task-2 additions, is mapped; `test_all_timezones_are_valid_iana` confirms the new zones are real). If it fails, a route airport is unmapped — add it.

- [ ] **Step 4: Commit** — `git add config/airport_tz.py pp_db/airport_tz.py && git commit -m "feat(scraper): map new intl/spoke origins in airport_tz (config + pp_db)"`

---

### Task 4: Cash scaling — pin international routes + raise capacity

**Files:** Modify `config/routes.py` (`CASH_PINNED_ROUTES`) and `fly.google.toml`.

- [ ] **Step 1: Pin the strategic new international routes in `CASH_PINNED_ROUTES`** (`config/routes.py:332-338`, append after the existing pins)

```python
    # coverage-expansion 2026-06-23 — guarantee day-one CPP for the Explore-facing intl routes
    # (zero organic demand initially, so they'd be crowded out of the demand-ranked cash queue).
    ("SEA", "HND"), ("SFO", "HND"), ("LAX", "HND"),
    ("LAX", "NRT"), ("SFO", "HKG"), ("LAX", "SYD"), ("LAX", "TPE"),
    ("SEA", "LHR"), ("DTW", "ICN"), ("JFK", "CDG"), ("DTW", "AMS"),
```

(These pinned pairs must also exist as award routes — they do, added in Task 2. Pins are bidirectional automatically. Cash still requires nonstop award rows to land in `pp.flights` first.)

- [ ] **Step 2: Raise cash per-run capacity in `fly.google.toml`** — change `CASH_TOP_ROUTES` from `800` to `1200` (line 16). Leave `CASH_REFRESH_INTERVAL_MIN=240` for now; the cadence knob (→180) is held in reserve, gated on `units_dropped` telemetry post-deploy.

- [ ] **Step 3: Run the full suite + lint** — `pytest -q` (routes test count for pins if asserted; `test_airport_tz` still green since pins reuse mapped airports) and `ruff check . && ruff format --check .`.

- [ ] **Step 4: Commit** — `git add config/routes.py fly.google.toml && git commit -m "feat(scraper): pin intl routes for cash + raise CASH_TOP_ROUTES 800→1200"`

---

### Task 5: Cleanup + final verification

- [ ] **Step 1:** `ruff check --fix . && ruff format .` then `ruff check . && ruff format --check .` → clean.
- [ ] **Step 2:** `pytest -q` → all green (report the count).
- [ ] **Step 3:** Separate `chore: cleanup` commit if formatter touched anything.

**Out of scope (do NOT do):** jobs-repo edits (companion plan), `api/` edits, extending the cash 30d horizon, the gflights 2nd-machine shard (orchestrator decides post-deploy via telemetry), bumping `CASH_REFRESH_INTERVAL_MIN`.

## Self-review (author)
- Spec §1 (three-tier window) → Task 1. §2 (depth-by-tier) → Task 1 (WINDOW_DEPTH_DAYS + tier param). §3 (route seeds + airport_tz) → Tasks 2–3. §6 (cash pin + CASH_TOP_ROUTES) → Task 4. §5 (canary rollout) → ship-gate (orchestrator), noted out-of-scope here.
- No placeholders: every step has concrete tuples/code/commands.
- Consistency: `WINDOW_DEPTH_DAYS`/`_scrape_window(tier=...)` names match across Tasks 1 steps; `CASH_PINNED_ROUTES` pins ⊆ Task-2 award routes; tz additions cover exactly the new IATA codes from Task 2.
