# Cash And Award Coverage Bump Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Increase Google Flights cash density, reduce Alaska/Delta award queue wait, and add measured international coverage for Alaska, Delta, and JetBlue.

**Architecture:** This is a configuration and test hardening change in `points-pilot-jobs`. Workflow YAML controls Actions capacity and cadence; `config/routes.py` seeds route queue coverage; timezone maps must be mirrored so award rows and cash matching can process every seeded origin.

**Tech Stack:** GitHub Actions YAML, Python 3.11, pytest, ruff, PyYAML, Supabase-backed route queue at runtime.

## Global Constraints

- Work only inside `/Users/louisn/Documents/indiehax/point_pilot/.worktrees/jobs-coverage-bump`.
- Do not touch Turkish capacity in this change.
- Keep cash at `CASH_SCRAPE_DAYS: "30"` and `CASH_SHARDS: "6"`.
- Set cash `CASH_TOP_ROUTES: "800"` and schedule three daily runs.
- Set Alaska matrix to `[0, 1, 2, 3]` and `ALASKA_SHARDS: "4"`.
- Set Delta matrix to `[0, 1, 2, 3, 4, 5]` and `DELTA_SHARDS: "6"`.
- Leave JetBlue shard count at 4.
- Add exactly the route pairs listed in the design spec.
- Every seeded airport must exist in both `config/airport_tz.py` and `pp_db/airport_tz.py`.
- Use TDD: add/update tests first, verify failure where practical, then implement.
- Run `pytest tests/ -q` and `ruff check .` before reporting complete.

---

### Task 1: Test Tooling And Coverage Guards

**Files:**
- Modify: `requirements.txt`
- Modify: `ruff.toml`
- Modify: `tests/test_airport_tz.py`
- Create: `tests/test_cash_workflow.py`
- Modify: `tests/test_alaska_scrape.py`
- Modify: `tests/test_delta_browser_scrape.py`
- Modify: `tests/test_jetblue_scrape.py`
- Modify: `tests/test_routes_config.py`

**Interfaces:**
- Consumes: existing workflow YAML files and route registry.
- Produces: failing tests for the intended workflow, route-count, and timezone changes.

- [ ] **Step 1: Add test dependency and lint config expectations**

Add `PyYAML>=6.0` to `requirements.txt`.

Update `ruff.toml` with:

```toml
exclude = ["pp_db"]
```

- [ ] **Step 2: Add cash workflow tests**

Create `tests/test_cash_workflow.py`:

```python
import yaml

_WF = ".github/workflows/cash-browser-scrape.yml"


def _workflow():
    with open(_WF) as f:
        return yaml.safe_load(f)


def test_cash_workflow_runs_three_times_daily_with_safe_spacing():
    wf = _workflow()
    schedule = wf[True]["schedule"]
    crons = [s["cron"] for s in schedule]
    assert crons == ["15 6,14,22 * * *"]
    hours = [int(h) for h in crons[0].split()[1].split(",")]
    assert len(hours) == 3
    assert all(not (8 <= h <= 11) for h in hours)
    assert 20 not in hours


def test_cash_workflow_shards_and_route_limit_are_consistent():
    wf = _workflow()
    job = wf["jobs"]["scrape"]
    shards = job["strategy"]["matrix"]["shard"]
    env = job["steps"][-1]["env"]
    n = int(env["CASH_SHARDS"])
    assert shards == list(range(n))
    assert n == 6
    assert env["CASH_SHARD_INDEX"] == "${{ matrix.shard }}"
    assert env["CASH_SCRAPE_DAYS"] == "30"
    assert env["CASH_TOP_ROUTES"] == "800"
```

- [ ] **Step 3: Tighten shard workflow tests**

In `tests/test_alaska_scrape.py`, change the minimum shard assertion to:

```python
assert n >= 4, "Alaska runs at least 4 fresh-IP shards"
```

In `tests/test_delta_browser_scrape.py`, add:

```python
import yaml

_WF = ".github/workflows/delta-browser-scrape.yml"


def test_delta_workflow_shard_matrix_is_consistent():
    with open(_WF) as f:
        wf = yaml.safe_load(f)
    job = wf["jobs"]["scrape"]
    shards = job["strategy"]["matrix"]["shard"]
    env = job["steps"][-1]["env"]
    n = int(env["DELTA_SHARDS"])
    assert shards == list(range(n)), f"matrix {shards} must be range(DELTA_SHARDS={n})"
    assert n >= 6, "Delta runs at least 6 fresh-IP shards"
    assert env["DELTA_SHARD_INDEX"] == "${{ matrix.shard }}"
```

In `tests/test_jetblue_scrape.py`, change the minimum shard assertion to:

```python
assert n >= 4, "JetBlue runs at least 4 fresh-IP shards"
```

- [ ] **Step 4: Update route-count test expectations**

In `tests/test_routes_config.py`, set:

```python
EXPECTED_PAIR_COUNTS = {
    "alaska": 137,
    "jetblue": 62,
    "delta": 113,
    "southwest": 58,
    "turkish": 25,
    "etihad": 13,
}
```

- [ ] **Step 5: Add complete timezone coverage tests**

Replace `tests/test_airport_tz.py` with:

```python
from config.airport_tz import AIRPORT_TZ as CONFIG_AIRPORT_TZ
from config.routes import all_seeded_routes
from pp_db.airport_tz import AIRPORT_TZ as CASH_AIRPORT_TZ


def _seeded_airports() -> set[str]:
    return {airport for origin, dest, _airline, _tier in all_seeded_routes() for airport in (origin, dest)}


def test_eze_timezone_is_available_for_jetblue_and_cash_matching():
    assert CONFIG_AIRPORT_TZ["EZE"] == "America/Argentina/Buenos_Aires"
    assert CASH_AIRPORT_TZ["EZE"] == "America/Argentina/Buenos_Aires"


def test_all_seeded_airports_have_award_timezones():
    missing = sorted(_seeded_airports() - set(CONFIG_AIRPORT_TZ))
    assert missing == []


def test_all_seeded_airports_have_cash_matching_timezones():
    missing = sorted(_seeded_airports() - set(CASH_AIRPORT_TZ))
    assert missing == []
```

- [ ] **Step 6: Run targeted tests and confirm they fail before implementation**

Run:

```bash
. .venv/bin/activate
pytest tests/test_cash_workflow.py tests/test_alaska_scrape.py tests/test_delta_browser_scrape.py tests/test_jetblue_scrape.py tests/test_routes_config.py tests/test_airport_tz.py -q
```

Expected: failures for cash schedule/top routes, Alaska/Delta shards, route counts, and missing timezone entries.

### Task 2: Implement Workflow Capacity, Routes, Timezones, And Docs

**Files:**
- Modify: `.github/workflows/cash-browser-scrape.yml`
- Modify: `.github/workflows/alaska-scrape.yml`
- Modify: `.github/workflows/delta-browser-scrape.yml`
- Modify: `config/routes.py`
- Modify: `config/airport_tz.py`
- Modify: `pp_db/airport_tz.py`
- Modify: `README.md`
- Modify: `CLAUDE.md`

**Interfaces:**
- Consumes: tests from Task 1.
- Produces: workflow and route configuration that satisfies the coverage bump.

- [ ] **Step 1: Update cash workflow**

In `.github/workflows/cash-browser-scrape.yml`:

```yaml
  schedule:
    - cron: "15 6,14,22 * * *"  # 3x/day, clear of the 08-11 UTC browser block + 20 UTC Delta
```

Keep:

```yaml
          CASH_SHARDS: "6"
          CASH_SCRAPE_DAYS: "30"
```

Change:

```yaml
          CASH_TOP_ROUTES: "800"
```

- [ ] **Step 2: Update Alaska workflow shards**

In `.github/workflows/alaska-scrape.yml`, set:

```yaml
        shard: [0, 1, 2, 3]
```

and:

```yaml
          ALASKA_SHARDS: "4"
```

- [ ] **Step 3: Update Delta workflow shards**

In `.github/workflows/delta-browser-scrape.yml`, set:

```yaml
        shard: [0, 1, 2, 3, 4, 5]
```

and:

```yaml
          DELTA_SHARDS: "6"
```

- [ ] **Step 4: Add route pairs and cash pins**

Append the exact route groups from the design spec to `ALASKA_MED_ROUTES`, `DELTA_MED_ROUTES`, and
`JETBLUE_MED_ROUTES`.

Append these pairs to `CASH_PINNED_ROUTES`:

```python
    # coverage-bump 2026-07-01 - pin highest-value new intl routes for early CPP.
    ("JFK", "DUB"), ("BOS", "DUB"),
    ("SEA", "FRA"), ("SEA", "KEF"), ("JFK", "FCO"),
    ("SEA", "FCO"), ("SEA", "BCN"), ("JFK", "OPO"),
    ("BOS", "MAD"), ("BOS", "BCN"), ("BOS", "MXP"),
```

- [ ] **Step 5: Mirror timezone maps**

Add new and already-missing seeded airports to both timezone maps where absent:

```python
    "ATH": "Europe/Athens",
    "BCN": "Europe/Madrid",
    "BNE": "Australia/Brisbane",
    "BUR": "America/Los_Angeles",
    "CPH": "Europe/Copenhagen",
    "CTA": "Europe/Rome",
    "DOH": "Asia/Qatar",
    "DUB": "Europe/Dublin",
    "EDI": "Europe/London",
    "FCO": "Europe/Rome",
    "FRA": "Europe/Berlin",
    "HEL": "Europe/Helsinki",
    "KEF": "Atlantic/Reykjavik",
    "LGB": "America/Los_Angeles",
    "LGW": "Europe/London",
    "MAD": "Europe/Madrid",
    "MEX": "America/Mexico_City",
    "MLA": "Europe/Malta",
    "MXP": "Europe/Rome",
    "NCE": "Europe/Paris",
    "OLB": "Europe/Rome",
    "ONT": "America/Los_Angeles",
    "OPO": "Europe/Lisbon",
    "SCL": "America/Santiago",
```

Only add keys missing from each map; keep existing comments concise.

- [ ] **Step 6: Update docs**

Update `README.md` and `CLAUDE.md` so they state:

- cash runs 3x/day with 6 shards and `CASH_TOP_ROUTES=800`;
- Alaska runs 4 shards;
- Delta runs 6 shards;
- JetBlue remains 4 shards;
- Fly gflights remains legacy/stopped.

- [ ] **Step 7: Run targeted tests**

Run:

```bash
. .venv/bin/activate
pytest tests/test_cash_workflow.py tests/test_alaska_scrape.py tests/test_delta_browser_scrape.py tests/test_jetblue_scrape.py tests/test_routes_config.py tests/test_airport_tz.py -q
```

Expected: pass.

### Task 3: Full Verification And Cleanup Commit

**Files:**
- Modify only if tests or lint reveal issues.

**Interfaces:**
- Consumes: Tasks 1-2.
- Produces: green local checks and a final implementation commit.

- [ ] **Step 1: Run full tests**

Run:

```bash
. .venv/bin/activate
pytest tests/ -q
```

Expected: pass, with live-DB tests skipped when `DATABASE_URL` is unset.

- [ ] **Step 2: Run lint**

Run:

```bash
ruff check .
```

Expected: pass.

- [ ] **Step 3: Check git diff**

Run:

```bash
git diff --stat
git diff --check
```

Expected: no whitespace errors; changes limited to workflows, route config, timezone maps, tests,
requirements, and docs.

- [ ] **Step 4: Commit implementation**

Run:

```bash
git add .github/workflows/cash-browser-scrape.yml .github/workflows/alaska-scrape.yml .github/workflows/delta-browser-scrape.yml config/routes.py config/airport_tz.py pp_db/airport_tz.py tests/test_cash_workflow.py tests/test_alaska_scrape.py tests/test_delta_browser_scrape.py tests/test_jetblue_scrape.py tests/test_routes_config.py tests/test_airport_tz.py requirements.txt ruff.toml README.md CLAUDE.md
git commit -m "feat: bump cash and international scraper coverage" -m "Co-Authored-By: Codex <codex@openai.com>"
```

Expected: commit succeeds.
