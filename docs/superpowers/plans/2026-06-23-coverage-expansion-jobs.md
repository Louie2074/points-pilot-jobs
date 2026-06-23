# Coverage Expansion — Jobs (GitHub Actions) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Mirror the route-seed + `airport_tz` expansion into the `jobs` repo (so the GH-Actions crons seed/scrape the same routes the scraper repo seeds), and scale per-airline sharding/cadence (Delta 3→5 shards + 1→2 runs, JetBlue 2→3, Southwest 5→6, Turkish 2→3, Etihad 1→2) — adding runner IPs, NOT raising per-shard WAF caps.

**Architecture:** `jobs` vendors `config/routes.py` + `config/airport_tz.py` (+ `pp_db/airport_tz.py`) from the scraper repo and seeds the **same** `pp.routes_queue`, so route/tz edits MUST match the scraper copies exactly. Sharding is matrix + `<AIRLINE>_SHARDS` env in each `.github/workflows/*.yml`; per-shard caps in `config/settings.py:140-155` stay unchanged (each shard is a fresh Azure IP under the airline's per-session ceiling). The 90-day window is delivered scraper-side (Fly, AS/B6); browser airlines (DL/WN/TK/EY) stay near-term here per the design — no window edits in this repo.

**Tech Stack:** Python 3.11 (pytest, ruff), GitHub Actions YAML. Repo is public (unlimited Actions minutes; only the 20-concurrent-job ceiling matters).

**Baseline checks (run first):** `pytest tests/ -q` · `ruff check .`

**Consistency requirement:** the NEW route tuples + tz entries below are byte-identical to the scraper plan's Tasks 2–3. The orchestrator diffs the route lists + tz dicts across repos after both land.

---

### Task 1: Mirror route-seed expansion into `jobs/config/routes.py`

**Files:** Modify `config/routes.py`. Test: `tests/` (any route-count test).

- [ ] **Step 1: Append the IDENTICAL new tuples** to each list, matching the scraper plan exactly:
  - `ALASKA_MED_ROUTES`: the 23 intl-partner + new-US-origin pairs (SEA/SFO/LAX→HND/NRT/HKG/SYD/TPE/LHR; DEN/PHX/AUS/MSP/SAN/SJC/GEG origins) under a `# coverage-expansion 2026-06-23` comment.
  - `JETBLUE_MED_ROUTES`: the 11 transcon/Caribbean/TATL pairs (JFK→LAX/SAN/AUS/SJU/LHR; BOS→SFO/SEA/SJU/LHR; FLL→SJU; EWR→FLL).
  - `DELTA_MED_ROUTES`: the 16 SkyTeam-intl + hub-spoke pairs (DTW/ATL→ICN; JFK/ATL→CDG; DTW/ATL→AMS; ATL→GRU; + ATL/SLC/MSP/DTW/JFK spokes).
  - `SOUTHWEST_MED_ROUTES`: the 16 domestic focus-city pairs (DEN→AUS/TPA/MSY/BNA; MDW→DEN/TPA/AUS; BWI→MDW/ATL/SAN; HOU→DAL/DEN; PHX→DEN; OAK→PHX; SAN→PHX; SMF→LAS).
  - `TURKISH_MED_ROUTES`: the 5 US gateways → IST (SAN/AUS/RDU/BWI/MSP).
  - `ETIHAD_MED_ROUTES`: the 3 US gateways → AUH (DFW/SEA/DCA).
  - `CASH_PINNED_ROUTES`: the 11 pinned intl pairs (SEA/SFO/LAX→HND; LAX→NRT; SFO→HKG; LAX→SYD; LAX→TPE; SEA→LHR; DTW→ICN; JFK→CDG; DTW→AMS).

  (Use the exact tuples from `scraper/docs/superpowers/plans/2026-06-23-coverage-expansion-scraper.md` Tasks 2 & 4 — provided in the executor prompt. Append only; do not edit existing entries.)

- [ ] **Step 2:** If `tests/` asserts per-airline route counts, update the expected numbers; run `pytest tests/ -q`.
- [ ] **Step 3: Commit** — `git add config/routes.py tests/ && git commit -m "feat(jobs): mirror route-seed expansion (intl + new origins) from scraper"`

---

### Task 2: Mirror `airport_tz` additions

**Files:** Modify `config/airport_tz.py` AND `pp_db/airport_tz.py`. Test: `tests/test_airport_tz.py` if present.

- [ ] **Step 1:** Add the IDENTICAL new IATA→IANA entries from the scraper plan Task 3 (`NRT, HKG, SYD, TPE, ICN, CDG, AMS, GRU, SJU, MSY`) to `config/airport_tz.py`, alphabetically.
- [ ] **Step 2:** Apply the same additions to `pp_db/airport_tz.py`.
- [ ] **Step 3:** Run `pytest tests/ -q` (if a `test_airport_tz` guard exists it must pass; otherwise this is config-only).
- [ ] **Step 4: Commit** — `git add config/airport_tz.py pp_db/airport_tz.py && git commit -m "feat(jobs): mirror new origins in airport_tz (config + pp_db)"`

---

### Task 3: JetBlue 2→3 shards

**Files:** `.github/workflows/jetblue-scrape.yml`.

- [ ] **Step 1:** Change the matrix `shard: [0, 1]` → `shard: [0, 1, 2]` (line ~22) AND `JETBLUE_SHARDS: "2"` → `"3"` (line ~38). Both together (mismatch corrupts the stride-shard).
- [ ] **Step 2:** Validate the YAML parses (`python -c "import yaml,sys; yaml.safe_load(open('.github/workflows/jetblue-scrape.yml'))"`).
- [ ] **Step 3: Commit** — `git commit -am "feat(jobs): JetBlue 2→3 shards"`

---

### Task 4: Delta 3→5 shards + add a 2nd daily run

**Files:** `.github/workflows/delta-browser-scrape.yml`. Per-shard cap (`DELTA_MAX_LEGS_PER_SHARD=18`) stays — we add IPs/runs, not bigger shards.

- [ ] **Step 1:** Matrix `shard: [0, 1, 2]` → `shard: [0, 1, 2, 3, 4]` AND `DELTA_SHARDS: "3"` → `"5"` (lines ~47, ~59).
- [ ] **Step 2:** Add a 2nd cron in the `schedule:` block (after `- cron: "0 8 * * *"`): `- cron: "0 20 * * *"` (20:00 UTC — clear of the 08–11 award block and Alaska's 19:17). Add a comment noting the 2nd daily run halves Delta's effective TTL.
- [ ] **Step 3:** Validate YAML parses.
- [ ] **Step 4: Commit** — `git commit -am "feat(jobs): Delta 3→5 shards + 2nd daily run (08:00, 20:00 UTC)"`

---

### Task 5: Southwest 5→6 shards

**Files:** `.github/workflows/southwest-browser-scrape.yml`.

- [ ] **Step 1:** Matrix `shard: [0, 1, 2, 3, 4]` → `[0, 1, 2, 3, 4, 5]` AND `SOUTHWEST_SHARDS: "5"` → `"6"` (lines ~53, ~65).
- [ ] **Step 2:** Validate YAML parses.
- [ ] **Step 3: Commit** — `git commit -am "feat(jobs): Southwest 5→6 shards"`

---

### Task 6: Turkish 2→3 shards

**Files:** `.github/workflows/turkish-browser-scrape.yml`.

- [ ] **Step 1:** Matrix `shard: [0, 1]` → `[0, 1, 2]` AND `TURKISH_SHARDS: "2"` → `"3"` (lines ~52, ~64). (3 shards × 20 cap = 60 ≥ the new ~50 directed TK legs, full daily coverage.)
- [ ] **Step 2:** Validate YAML parses.
- [ ] **Step 3: Commit** — `git commit -am "feat(jobs): Turkish 2→3 shards"`

---

### Task 7: Etihad 1→2 shards (introduce matrix)

**Files:** `.github/workflows/etihad-browser-scrape.yml`. Etihad currently has NO matrix (single shard) — add one, mirroring Turkish's block.

- [ ] **Step 1:** Add under the job (after `timeout-minutes: 60`, mirroring `turkish-browser-scrape.yml`):

```yaml
    strategy:
      fail-fast: false
      matrix:
        shard: [0, 1]
```

- [ ] **Step 2:** Add the env wiring in the run step's `env:` block (mirroring Turkish):

```yaml
          ETIHAD_SHARDS: "2"
          ETIHAD_SHARD_INDEX: ${{ matrix.shard }}
```

(2 shards × 20 cap = 40 ≥ the new ~32 directed EY legs. Removes the single-IP-fate risk where a WAF'd IP loses 100% of the day.)

- [ ] **Step 3:** Validate YAML parses.
- [ ] **Step 4: Commit** — `git commit -am "feat(jobs): shard Etihad 1→2 (was single-IP)"`

---

### Task 8: Verify cron stagger + concurrency, lint, cleanup

- [ ] **Step 1: Concurrency check.** Confirm peak concurrent jobs stays well under 20: each airline's shards run at distinct UTC hours (Alaska 01:17/13:17/19:17 ×3; JetBlue 02:37/14:37 ×3; Delta 08:00+20:00 ×5; Southwest 09:00 ×6; Turkish 10:00 ×3; Etihad 11:00 ×2). No two multi-shard airlines share a slot → peak ≈ 6 (Southwest). Document this in a comment in `README.md`'s schedule table.
- [ ] **Step 2:** `ruff check .` → clean. `pytest tests/ -q` → green (report count).
- [ ] **Step 3:** Update `README.md` job/schedule table with the new shard counts + Delta's 2nd run.
- [ ] **Step 4: Commit** — `git add README.md && git commit -m "docs(jobs): update schedule table for coverage-expansion sharding"` (+ separate `chore: cleanup` if the formatter touched anything).

**Out of scope:** scraper-repo edits (companion plan), per-shard cap increases (caps stay — add IPs), the 90-day window (scraper-side), `api/` edits.

## Self-review (author)
- Spec §3 (route + tz expansion) → Tasks 1–2 (mirror). §4 (per-airline shard/cadence sizing) → Tasks 3–7 exactly matching the §4.4 table. Concurrency ≤20 → Task 8.
- No placeholders: every shard edit names the matrix array + `<AIRLINE>_SHARDS` env and the both-together rule; tuples reference the scraper plan (provided in executor prompt).
- Consistency: shard targets match spec §4.4; caps explicitly unchanged; new tuples/tz identical to scraper plan.
