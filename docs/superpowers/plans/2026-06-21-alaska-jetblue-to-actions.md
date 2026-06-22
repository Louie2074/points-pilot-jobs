# Alaska + JetBlue → GitHub Actions sharded crons — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move the scheduled Alaska + JetBlue award refresh off the always-on `point-pilot-scraper` Fly box onto sharded GitHub Actions crons in the public `jobs/` repo, mirroring the award browser-scraper pattern (but httpx — no Chrome).

**Architecture:** AS/B6 are httpx (`HttpScraper`). `scrapers/base.py` (incl. `HttpScraper`), `config/`, `pipeline/`, and `pp_db/` are **already vendored** in `jobs/`, and AS/B6 routes are **already seeded** (`config/routes.py:291`). So the work is: vendor the two scraper *classes*, add their leg-cap entries, fix the shared cron path's per-tier expiry stamping, add a dense/sparse date-window helper, and write two thin entry points + two plain-`ubuntu-latest` workflows. The API's on-demand inline AS/B6 scrape is untouched (separate process on the API box). Cutover (decommission the Fly box) is a gated final step after a parallel-run soak.

**Tech Stack:** Python 3.11, httpx, pytest, `browser_scrape_common` (`build_queue_plan`/`run_scrape`), GitHub Actions, `pp_db` (Supabase Postgres).

**Source spec:** `docs/superpowers/specs/2026-06-21-alaska-jetblue-to-actions-design.md`.

---

## File structure

| File | Change | Responsibility |
|---|---|---|
| `jobs/scrapers/alaska.py` | **create** (vendor from `scraper/`) | Alaska httpx scraper class |
| `jobs/scrapers/jetblue.py` | **create** (vendor from `scraper/`) | JetBlue httpx scraper class |
| `jobs/config/settings.py` | modify (`CRON_MAX_LEGS_PER_SHARD`) | per-shard leg caps for alaska/jetblue |
| `jobs/browser_scrape_common.py` | modify (`run_scrape`) + add helpers | per-tier expiry stamping fix + `dense_sparse_dates` |
| `jobs/tests/test_browser_scrape_common.py` | create/extend | unit tests for the two helpers |
| `jobs/alaska_scrape.py` | **create** | Alaska cron entry point (clone of `delta_browser_scrape.py`) |
| `jobs/jetblue_scrape.py` | **create** | JetBlue cron entry point |
| `jobs/tests/test_alaska_scrape.py` · `test_jetblue_scrape.py` | create | entry-point `_build_plan`/`_parse_dates_csv` re-export tests |
| `jobs/.github/workflows/alaska-scrape.yml` · `jetblue-scrape.yml` | **create** | sharded crons (plain `ubuntu-latest`, no container) |
| `scraper/CLAUDE.md` | modify | record the 3-way vendoring of `alaska.py`/`jetblue.py` |
| `scraper/pipeline/scheduler.py` | modify (cutover, gated) | remove the two `refresh_*` jobs |

---

## Task 1: Vendor the Alaska + JetBlue scraper classes into `jobs/`

`jobs/scrapers/base.py` already has `HttpScraper`/`BaseScraper`/`ScraperBlockedError`, and `config/settings.py` (`TTL_HOURS`, `PriorityTier`) + `config/airport_tz.py` (`AIRPORT_TZ`) are present — so the two airline files' imports resolve with no other vendoring.

**Files:**
- Create: `jobs/scrapers/alaska.py`, `jobs/scrapers/jetblue.py` (copied verbatim from `scraper/scrapers/`)

- [ ] **Step 1: Copy the two files verbatim from the canonical scraper repo**

```bash
cd /Users/louisn/Documents/indiehax/point_pilot
cp scraper/scrapers/alaska.py  jobs/scrapers/alaska.py
cp scraper/scrapers/jetblue.py jobs/scrapers/jetblue.py
```

- [ ] **Step 2: Verify imports resolve (no missing deps)**

Run: `cd jobs && MOTHERDUCK_TOKEN=dummy python -c "from scrapers.alaska import AlaskaScraper; from scrapers.jetblue import JetBlueScraper; print(AlaskaScraper.airline_code, JetBlueScraper.airline_code, AlaskaScraper.dense_days, AlaskaScraper.sparse_step)"`
Expected: prints `AS B6 <int> <int>` with no ImportError. (If it errors on a missing symbol, the missing dep must also be vendored — but base/config/airport_tz are already present, so this should pass.)

- [ ] **Step 3: Lint**

Run: `cd jobs && ruff check scrapers/alaska.py scrapers/jetblue.py`
Expected: no errors.

- [ ] **Step 4: Record the 3-way vendoring in the canonical repo's guide**

In `scraper/CLAUDE.md`, in the vendoring section that lists what `api/` mirrors, add a line noting that `scrapers/alaska.py` + `scrapers/jetblue.py` are **also vendored into `jobs/`** (for the GitHub-Actions scheduled scrape) and must be propagated scraper → api → jobs on change. (Append a sentence to the existing "Copied verbatim" rule — keep it one line; do not restructure the doc.)

- [ ] **Step 5: Commit**

```bash
cd jobs && git add scrapers/alaska.py scrapers/jetblue.py
git -C ../scraper add CLAUDE.md
git commit -m "feat(jobs): vendor Alaska + JetBlue httpx scrapers for the Actions cron migration"
git -C ../scraper commit -m "docs: note alaska.py/jetblue.py are now vendored to jobs/ too"
```

---

## Task 2: Add Alaska + JetBlue to the per-shard leg-cap map

**Files:**
- Modify: `jobs/config/settings.py` (`CRON_MAX_LEGS_PER_SHARD`, ~line 158)

- [ ] **Step 1: Add the two entries**

In `jobs/config/settings.py`, inside the `CRON_MAX_LEGS_PER_SHARD` dict (after the `etihad` line), add:

```python
    # Alaska: ~55 MED pairs (110 directed legs). Single Fly IP already scrapes the full
    # catalogue safely, so a generous cap + a small shard fan-out covers it within a 6h job.
    "alaska": int(_get("ALASKA_MAX_LEGS_PER_SHARD", "40")),
    # JetBlue: ~13 pairs (26 directed legs) — one shard covers it.
    "jetblue": int(_get("JETBLUE_MAX_LEGS_PER_SHARD", "30")),
```

- [ ] **Step 2: Verify**

Run: `cd jobs && MOTHERDUCK_TOKEN=dummy python -c "from config.settings import CRON_MAX_LEGS_PER_SHARD as m; assert m['alaska']==40 and m['jetblue']==30; print('ok')"`
Expected: prints `ok`.

- [ ] **Step 3: Commit**

```bash
cd jobs && git add config/settings.py
git commit -m "feat(jobs): add alaska/jetblue per-shard leg caps"
```

---

## Task 3: Per-tier expiry stamping in `run_scrape` (shared cron-path fix)

Today `run_scrape` flat-stamps `PriorityTier.MED` (24h) for every route (`browser_scrape_common.py:216`), ignoring the route's adaptive tier. The queue `RouteJob` carries `.tier`; stamp by it so HIGH routes get 8h and LOW get 48h. This also corrects the existing cron airlines (Delta/SW/TK/EY).

**Files:**
- Modify: `jobs/browser_scrape_common.py` (add `_tier_for_job`; use it at line 216)
- Test: `jobs/tests/test_browser_scrape_common.py`

- [ ] **Step 1: Write the failing tests**

Create `jobs/tests/test_browser_scrape_common.py` (or append if it exists):

```python
import browser_scrape_common as common
from browser_scrape_common import _PairJob


class _StubRouteJob:
    def __init__(self, tier):
        self.origin, self.dest, self.tier = "SEA", "JFK", tier


def test_tier_for_job_uses_routejob_tier():
    assert common._tier_for_job(_StubRouteJob("HIGH"), "MED") == "HIGH"
    assert common._tier_for_job(_StubRouteJob("LOW"), "MED") == "LOW"


def test_tier_for_job_defaults_for_ondemand_pairjob():
    # On-demand _PairJobs have no .tier → fall back to the default.
    assert common._tier_for_job(_PairJob("SEA", "JFK"), "MED") == "MED"
```

- [ ] **Step 2: Run to verify it FAILS**

Run: `cd jobs && MOTHERDUCK_TOKEN=dummy python -m pytest tests/test_browser_scrape_common.py -v`
Expected: FAIL with `AttributeError: module 'browser_scrape_common' has no attribute '_tier_for_job'`.

- [ ] **Step 3: Add the helper and use it**

In `jobs/browser_scrape_common.py`, add the helper just above `run_scrape` (after `freshness`):

```python
def _tier_for_job(job, default: str) -> str:
    """The expiry tier for a scraped route: the queue RouteJob's adaptive tier in cron mode,
    or `default` for on-demand _PairJobs (which have no .tier). Fixes the prior flat-MED stamp
    that ignored HIGH (8h) / LOW (48h) windows."""
    return getattr(job, "tier", default)
```

Then in `run_scrape`, replace the flat-MED stamp (line ~216):

```python
                stamped = stamp_expiry(filter_valid(recs), PriorityTier.MED)
```

with:

```python
                stamped = stamp_expiry(filter_valid(recs), _tier_for_job(job, PriorityTier.MED))
```

- [ ] **Step 4: Run the new tests + the FULL jobs suite (shared-path change)**

Run: `cd jobs && MOTHERDUCK_TOKEN=dummy python -m pytest tests/test_browser_scrape_common.py -v`
Expected: PASS.
Run: `cd jobs && MOTHERDUCK_TOKEN=dummy python -m pytest tests/ -q`
Expected: all PASS. (This change touches the shared award-cron path — if any existing Delta/SW/TK/EY test asserted flat-MED expiry, update it to the per-tier expectation; per-tier is the intended behavior.)

- [ ] **Step 5: Commit**

```bash
cd jobs && git add browser_scrape_common.py tests/test_browser_scrape_common.py
git commit -m "fix(jobs): stamp per-route-tier expiry in run_scrape (was flat MED) — fixes HIGH/LOW windows for all cron airlines"
```

---

## Task 4: Dense/sparse date-window helper

The Fly scheduler scrapes a dense near-term window + a sparse tail to 30 days (per the scraper's `dense_days`/`sparse_step`). `build_queue_plan` only emits a flat `range(scrape_days)`. Add a pure helper the AS/B6 entry points use to build the matching window and pass to `run_scrape` — **without** changing the shared `build_queue_plan` (so Delta/SW/TK/EY are unaffected).

**Files:**
- Modify: `jobs/browser_scrape_common.py` (add `dense_sparse_dates`)
- Test: `jobs/tests/test_browser_scrape_common.py`

- [ ] **Step 1: Write the failing test**

Append to `jobs/tests/test_browser_scrape_common.py`:

```python
from datetime import date


def test_dense_sparse_dates_dense_then_sparse():
    # dense_days=3 → days 0,1,2 every day; then sparse_step=2 → 3,5,7,9 up to <max_day=10.
    out = common.dense_sparse_dates(date(2026, 7, 1), dense_days=3, sparse_step=2, max_day=10)
    assert out == [date(2026, 7, d) for d in (1, 2, 3, 4, 6, 8, 10)]


def test_dense_sparse_dates_no_sparse_when_dense_covers_window():
    out = common.dense_sparse_dates(date(2026, 7, 1), dense_days=5, sparse_step=3, max_day=5)
    assert out == [date(2026, 7, d) for d in (1, 2, 3, 4, 5)]
```

- [ ] **Step 2: Run to verify it FAILS**

Run: `cd jobs && MOTHERDUCK_TOKEN=dummy python -m pytest tests/test_browser_scrape_common.py::test_dense_sparse_dates_dense_then_sparse -v`
Expected: FAIL with `AttributeError: ... has no attribute 'dense_sparse_dates'`.

- [ ] **Step 3: Add the helper**

In `jobs/browser_scrape_common.py`, add after `parse_dates_csv`:

```python
def dense_sparse_dates(
    today: date, dense_days: int, sparse_step: int, max_day: int
) -> list[date]:
    """Dates matching the always-on scheduler's profile: every day for the first ``dense_days``
    (offsets 0..dense_days-1), then every ``sparse_step``-th day out to ``max_day`` inclusive.
    Keeps AS/B6's request-volume profile (and WAF exposure) the same as the proven Fly profile,
    rather than a flat ``range(scrape_days)``."""
    offsets = list(range(dense_days))
    d = dense_days
    while d <= max_day:
        offsets.append(d)
        d += sparse_step
    return [today + timedelta(days=n) for n in offsets]
```

- [ ] **Step 4: Run to verify PASS**

Run: `cd jobs && MOTHERDUCK_TOKEN=dummy python -m pytest tests/test_browser_scrape_common.py -v`
Expected: all PASS.

- [ ] **Step 5: Verify it matches the scheduler's window shape**

Read `scraper/pipeline/scheduler.py`'s date-window construction (the dense/sparse loop) and confirm `dense_sparse_dates` produces the same offsets for Alaska's `dense_days`/`sparse_step`. If the scheduler's boundary differs (e.g. exclusive `max_day`), adjust the helper + its tests to match. (Goal: identical date set to today's Fly scrape.)

- [ ] **Step 6: Commit**

```bash
cd jobs && git add browser_scrape_common.py tests/test_browser_scrape_common.py
git commit -m "feat(jobs): dense_sparse_dates helper to match the Fly scheduler's date window"
```

---

## Task 5: Alaska cron entry point

**Files:**
- Create: `jobs/alaska_scrape.py`
- Test: `jobs/tests/test_alaska_scrape.py`

- [ ] **Step 1: Write the entry point**

Create `jobs/alaska_scrape.py`:

```python
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
import time
from datetime import date

import browser_scrape_common as common
from config.settings import CRON_MAX_LEGS_PER_SHARD

ALASKA_HEARTBEAT_URL = os.getenv("ALASKA_HEARTBEAT_URL", "")

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


def _run_cron(shard_index: int, shards: int) -> None:
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
    common.run_scrape(
        scraper, [], dates,
        source="alaska", service="point-pilot-alaska", airline="AS",
        heartbeat_url=ALASKA_HEARTBEAT_URL, logger=logger, route_jobs=route_jobs,
    )


def main() -> None:
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
    _run_cron(SHARD_INDEX, SHARDS)


if __name__ == "__main__":
    main()
    # Parity with the browser entrypoints' hard-exit convention; harmless for httpx.
    time.sleep(1)
    os._exit(0)
```

- [ ] **Step 2: Write a smoke test for the entry point's import + config**

Create `jobs/tests/test_alaska_scrape.py`:

```python
def test_alaska_scrape_imports_and_configures(monkeypatch):
    monkeypatch.setenv("MOTHERDUCK_TOKEN", "dummy")
    import alaska_scrape
    assert alaska_scrape.MAX_LEGS_PER_SHARD == 40
    assert alaska_scrape.SHARDS >= 1
    # the scraper class is importable and is the AS httpx scraper
    from scrapers.alaska import AlaskaScraper
    assert AlaskaScraper.airline_code == "AS"
```

- [ ] **Step 3: Run the test + lint**

Run: `cd jobs && MOTHERDUCK_TOKEN=dummy python -m pytest tests/test_alaska_scrape.py -v`
Expected: PASS.
Run: `cd jobs && ruff check alaska_scrape.py`
Expected: no errors.

- [ ] **Step 4: Commit**

```bash
cd jobs && git add alaska_scrape.py tests/test_alaska_scrape.py
git commit -m "feat(jobs): Alaska cron entry point (sharded, dense/sparse window, httpx)"
```

---

## Task 6: JetBlue cron entry point

**Files:**
- Create: `jobs/jetblue_scrape.py`
- Test: `jobs/tests/test_jetblue_scrape.py`

- [ ] **Step 1: Write the entry point**

Create `jobs/jetblue_scrape.py` (identical shape to `alaska_scrape.py`, swapping the airline):

```python
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
```

- [ ] **Step 2: Smoke test**

Create `jobs/tests/test_jetblue_scrape.py`:

```python
def test_jetblue_scrape_imports_and_configures(monkeypatch):
    monkeypatch.setenv("MOTHERDUCK_TOKEN", "dummy")
    import jetblue_scrape
    assert jetblue_scrape.MAX_LEGS_PER_SHARD == 30
    from scrapers.jetblue import JetBlueScraper
    assert JetBlueScraper.airline_code == "B6"
```

- [ ] **Step 3: Run + lint**

Run: `cd jobs && MOTHERDUCK_TOKEN=dummy python -m pytest tests/test_jetblue_scrape.py -v && ruff check jetblue_scrape.py`
Expected: PASS, no lint errors.

- [ ] **Step 4: Commit**

```bash
cd jobs && git add jetblue_scrape.py tests/test_jetblue_scrape.py
git commit -m "feat(jobs): JetBlue cron entry point (sharded, dense/sparse window, httpx)"
```

---

## Task 7: GitHub Actions workflows (plain `ubuntu-latest`, no Chrome)

httpx needs no chromium container. Confirm `requirements.txt` covers the scraper's runtime deps (`httpx[http2]`, `brotli`, `tenacity`, `sqlalchemy`); install it directly on the runner.

**Files:**
- Create: `jobs/.github/workflows/alaska-scrape.yml`, `jobs/.github/workflows/jetblue-scrape.yml`

- [ ] **Step 1: Confirm runtime deps are in `requirements.txt`**

Run: `cd jobs && grep -iE "httpx|brotli|tenacity|sqlalchemy|h2" requirements.txt`
Expected: `httpx` (with the `[http2]` extra or a separate `h2`), `brotli`, `tenacity`, `sqlalchemy` present. If `h2`/`brotli` are missing (they're only needed by the httpx scrapers, not the browser ones), add `httpx[http2]` and `brotli` to `requirements.txt` and commit that change with this task.

- [ ] **Step 2: Write the Alaska workflow**

Create `jobs/.github/workflows/alaska-scrape.yml`:

```yaml
name: Alaska award scrape

on:
  workflow_dispatch:
  schedule:
    # 3×/day, offset to :17 and clear of the 08–11 UTC award-browser block, to keep the
    # 24h MED TTL fresh with margin (best-effort cron + the API on-demand backstop).
    - cron: "17 1,13,19 * * *"

permissions:
  contents: read

concurrency:
  group: alaska-scrape
  cancel-in-progress: false

jobs:
  scrape:
    runs-on: ubuntu-latest
    timeout-minutes: 60
    strategy:
      fail-fast: false
      matrix:
        shard: [0, 1]
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - run: pip install -r requirements.txt
      - name: scrape
        run: python alaska_scrape.py
        env:
          DATABASE_URL: ${{ secrets.DATABASE_URL }}
          MOTHERDUCK_TOKEN: ${{ secrets.MOTHERDUCK_TOKEN }}
          BETTERSTACK_SOURCE_TOKEN: ${{ secrets.BETTERSTACK_SOURCE_TOKEN }}
          ALASKA_HEARTBEAT_URL: ${{ secrets.ALASKA_HEARTBEAT_URL }}
          ALASKA_SHARDS: "2"
          ALASKA_SHARD_INDEX: ${{ matrix.shard }}
```

- [ ] **Step 3: Write the JetBlue workflow**

Create `jobs/.github/workflows/jetblue-scrape.yml`:

```yaml
name: JetBlue award scrape

on:
  workflow_dispatch:
  schedule:
    - cron: "37 2,14 * * *"   # 2×/day, offset clear of the award block

permissions:
  contents: read

concurrency:
  group: jetblue-scrape
  cancel-in-progress: false

jobs:
  scrape:
    runs-on: ubuntu-latest
    timeout-minutes: 60
    strategy:
      fail-fast: false
      matrix:
        shard: [0]
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - run: pip install -r requirements.txt
      - name: scrape
        run: python jetblue_scrape.py
        env:
          DATABASE_URL: ${{ secrets.DATABASE_URL }}
          MOTHERDUCK_TOKEN: ${{ secrets.MOTHERDUCK_TOKEN }}
          BETTERSTACK_SOURCE_TOKEN: ${{ secrets.BETTERSTACK_SOURCE_TOKEN }}
          JETBLUE_HEARTBEAT_URL: ${{ secrets.JETBLUE_HEARTBEAT_URL }}
          JETBLUE_SHARDS: "1"
          JETBLUE_SHARD_INDEX: ${{ matrix.shard }}
```

- [ ] **Step 4: Lint the YAML (parse check)**

Run: `cd jobs && python -c "import yaml; yaml.safe_load(open('.github/workflows/alaska-scrape.yml')); yaml.safe_load(open('.github/workflows/jetblue-scrape.yml')); print('yaml ok')"`
Expected: prints `yaml ok`.

- [ ] **Step 5: Commit**

```bash
cd jobs && git add .github/workflows/alaska-scrape.yml .github/workflows/jetblue-scrape.yml requirements.txt
git commit -m "feat(jobs): Alaska + JetBlue sharded GitHub Actions cron workflows (plain ubuntu, httpx)"
```

---

## Task 8: Deploy to Actions + parallel-run soak (gated; cutover NOT auto-executed)

This task is operational and **must not be auto-executed by an implementer subagent** — it merges to the public default branch, runs live scrapes, and (finally) destroys a prod Fly app. Surface it to the human at each gate.

- [ ] **Step 1: Open a PR for the `as-b6-to-actions` branch and merge to `main`**

`workflow_dispatch`/`schedule` only see workflows on the default branch. Open a PR, get it reviewed, merge. Then confirm the secrets the workflows reference exist in the repo (`DATABASE_URL`, `MOTHERDUCK_TOKEN`, `BETTERSTACK_SOURCE_TOKEN`; the `*_HEARTBEAT_URL`s are optional) — they already back the award crons.

- [ ] **Step 2: Dispatch one manual run of each and verify rows land**

Run: `gh workflow run alaska-scrape.yml` and `gh workflow run jetblue-scrape.yml`, watch with `gh run watch`. Then verify in Supabase: `SELECT source, count(*), max(scraped_at_utc) FROM pp.flights WHERE source IN ('alaska','jetblue') GROUP BY 1` shows fresh rows from the run.

- [ ] **Step 3: Parallel-run soak (~1 week) — Fly scheduler STILL RUNNING**

Leave the `point-pilot-scraper` Fly box running. Let the new crons run on schedule alongside it. Watch Better Stack `scrape_run` metrics + `max(scraped_at_utc)` by route, and the heatmap/best-deals coverage. Confirm Actions keeps AS/B6 inside the 24h MED TTL with margin and that cron skew/skips don't stale cold routes. Tune shard count / cron frequency if a tier lags.

- [ ] **Step 4: Cutover (only after the soak passes + explicit human sign-off)**

In `scraper/pipeline/scheduler.py`, remove the `refresh_alaska_routes` + `refresh_jetblue_routes` job registrations (and any now-dead per-airline scheduler config), commit, and redeploy the scraper service. Then **with explicit human confirmation**, decommission the box: `flyctl apps destroy point-pilot-scraper`. End state: `point-pilot-api` + `point-pilot-gflights` remain on Fly. The API's on-demand inline AS/B6 path is untouched throughout.

---

## Self-review

- **Spec coverage:** vendor scrapers (T1) ✓; leg caps (T2) ✓; per-tier stamping fix (T3) ✓; dense/sparse window (T4 — via a helper, not a `build_queue_plan` change, so Delta/SW/TK/EY are untouched) ✓; entry points (T5/T6) ✓; plain-ubuntu sharded workflows + cadence offset clear of the award block + concurrency group (T7) ✓; parallel-run soak → gated cutover + destroy box (T8) ✓; 3-way vendoring note (T1.4) ✓; on-demand inline path untouched (no API changes anywhere) ✓.
- **Simplification vs spec:** the spec said "vendor base.py/normalizer/queue_manager/config" — verified unnecessary (already vendored; only the two scraper classes are missing). Recorded.
- **Placeholder scan:** every code step has full code; commands have expected output. The one non-code judgement step (T4.5 verify-against-scheduler) is a verification, not a placeholder.
- **Type/name consistency:** `_tier_for_job(job, default)`, `dense_sparse_dates(today, dense_days, sparse_step, max_day)`, `build_queue_plan(airline, shard_index=, shards=, max_legs=, scrape_days=, today=)`, and `run_scrape(scraper, pairs, dates, source=, service=, airline=, heartbeat_url=, logger=, route_jobs=)` are used consistently across T3–T6 and match the real signatures in `browser_scrape_common.py`.
