# Coverage Expansion — Award Routes, 90-Day Window, Sharding & Cash Scaling

**Date:** 2026-06-23
**Repos:** `scraper` (primary: window, seeds, airport_tz, cash) + `jobs` (GitHub Actions workflows, per-shard caps). No `api`/`ui` change.
**Status:** Approved design (brainstorming). Next step: implementation plan (writing-plans → tandem).

## 1. Goal

Expand award-data coverage so search and **Explore** surface far more routes and dates:

1. **More routes for every airline** — grow both origins *and* destinations, proportionally, prioritizing international destinations where the program books partner award space (this is what fills Explore's thin regions).
2. **Extend the award scrape window 30d → ~90d (3 months)** so searches/Explore 1–3 months out hit cached data.
3. Do it **intelligently and with sharding** so the larger route × deeper window does not blow the per-airline WAF ceilings, the GitHub Actions concurrency budget, or the cash scraper's capacity.

Data-agnostic principle (carried from the Explore design): the features already read whatever the cache holds; this work *feeds* them. Explore sorts on **points**, so missing CPP on far-out rows is acceptable.

## 2. The load-bearing insight

The binding constraint per scraper shard is **wall-clock time** against the GitHub Actions budget (45 min soft / 60–150 min hard), not the leg cap. Throughput is:

```
legs_per_shard_per_run ≈ TIME_BUDGET ÷ (avg_dates_per_route × delay × overhead)
```

A deeper window (more dates/route) *directly* shrinks how many routes a shard keeps fresh. Naïvely giving every route a 90-day window would roughly halve coverage. **Depth-tiering rescues it:** only the hot minority gets the full 90 days, so the *average* dates/route barely moves while the horizon triples. Everything below depends on this.

## 3. Current state (grounded, with sources)

### 3.1 Shared window + scheduler (`scraper`)
- `_scrape_window(today, scraper)` builds **two** segments: dense (days 0–13, daily) + sparse (14–30, step 3) → ~20 dates (Alaska). `scheduler.py:102-117`.
- `SCRAPE_DAYS_AHEAD=30`, `SCRAPE_DENSE_DAYS=14`, `SCRAPE_SPARSE_STEP=3` — `settings.py:32,42,43`.
- Per-scraper window overrides are class attrs (`scrape_days_ahead`, `dense_days`, `sparse_step`) — `scrapers/base.py:292-294`; JetBlue overrides to dense 10 / sparse 4 → ~13 dates (`scrapers/jetblue.py:119-124`).
- Priority tiers LOW→MED→HIGH promoted by `search_count` (`PROMOTE_TO_MED=3`, `PROMOTE_TO_HIGH=10`); adaptive cadence within `CADENCE_BOUNDS_H` (HIGH 8–24h, MED 24–72h, LOW 48–144h); composite score = demand×overdue×change-rate. `settings.py:86-93,157-184`; `scoring.py`; `queue_manager.py:119-203`.
- `routes_queue` PK `(origin, dest, airline)`, indexed on `next_scrape_at_utc` — `pp_db/models.py:50-67`.

### 3.2 Per-airline scrape config (the sizing table)

| Airline | Mechanism | Per-shard cap | Proven ceiling | Shards | Runs/day | Jobs/day | Pairs (undirected→directed) | Window today |
|---|---|---|---|---|---|---|---|---|
| Alaska (AS) | httpx | 40 legs | ~50/session | 3 | 3 | 9 | 74→148 | dense14/sparse3, 30d (~20 dates) |
| JetBlue (B6) | httpx | 30 legs | — | 2 | 2 | 4 | 27→54 | dense10/sparse4, 30d (~13 dates) |
| Delta (DL) | browser (Akamai) | 18 legs | ~27/session | 3 | 1 | 3 | 50→100 | 5d cron (dense only) |
| Southwest (WN) | browser (F5/Shape) | 20 legs | ~67% IP-ban | 5 | 1 | 5 | 42→84 | 14d cron |
| Turkish (TK) | browser | 20 legs | 20 | 2 | 1 | 2 | 20→40 (US→IST) | 3d cron |
| Etihad (EY) | browser | 20 legs | 20 | 1 | 1 | 1 | 10→20 (US→AUH) | 3d cron |

- Total **16 award jobs/day**; peak concurrent ~5–6. GitHub Actions free-plan ceiling = **20 concurrent jobs** (repo is public — runners are free; only concurrency is bounded). `jobs/.github/workflows/*`, `jobs/config/settings.py:139-154`.
- httpx delay 6s (Alaska) / 12s (JetBlue); browser delay 12s. Time budget per cron shard = 2700s (45 min); `jobs/browser_scrape_common.py:29`.
- Browser scraper mechanisms are real WAF clears (headful Chrome under xvfb on Azure IPs); `websockets` pin <15 is non-negotiable (nodriver CDP).

### 3.3 Seeds + airport_tz + cash coupling (`scraper`)
- Seeds in `config/routes.py` as one-direction tuples; `all_seeded_routes()` auto-adds reverse; metros expand via `route_set()`/`expand_route_pairs()`. 223 undirected pairs total (446 directed). Seeded into `pp.routes_queue` by `QueueManager.seed_from_config()` at startup (`main.py:74-76`, `queue_manager.py:360-372`).
- **`config/airport_tz.py`** maps 65 IATA→IANA. A missing origin silently (a) skips the cash matcher (no CPP — `pp_db/queries.py:57-64`) and (b) drops JetBlue departure times (`scrapers/jetblue.py:53-72`). **Hermetic test `test_every_route_airport_has_timezone` fails the build if any seeded airport is unmapped** (`tests/test_airport_tz.py:23-25`) — this structurally prevents the trap.
- Cash scraper covers **30 days** (`CASH_SCRAPE_DAYS=30`, `settings.py:122-128`); far-tail award rows (31–90d) will have `cpp=null`.

### 3.4 Cash scraper (`point-pilot-gflights`, Fly)
- 1 Fly machine (shared-cpu-2x, 2GB, sjc), browser nav, **no WAF wall** (Google doesn't IP-ban search). Binding constraint = ~22s/unit wall-clock. `fly.google.toml`, `scrapers/google_flights.py:103-163`.
- `CASH_TOP_ROUTES=800`/run × ~4 effective runs/day = **~2,400–2,880 units/day** delivered (after the 2026-06-21 cadence fix). `fly.google.toml:16-17`.
- 72h TTL, 30d window, 4 cabins (PE+first demoted every 4th run). Eligible nonstop universe ~3,686 units → ~1,229 refreshes/day needed → **~2.3× headroom today**.
- **Selection:** `get_top_cash_routes()` (`pp_db/queries_cash.py:130-206`) picks `(route,date,cabin)` units that have **nonstop award rows in `pp.flights`**, lack fresh cash (72h), and aren't zero-memoized. **Demand RANKS, it does not GATE.** A zero-demand new route is eligible but sorts *below* demand-ranked routes and is cut by the 800-unit cap. `CASH_PINNED_ROUTES` (`routes.py:332-338`) bypasses ranking. Instrumented via `units_eligible/selected/dropped` to Better Stack.

## 4. Design

### 4.1 §1 — Three-tier window (30d → 90d)
Add a **coarse** segment to `_scrape_window` (`scheduler.py:102-117`): after the sparse segment, append days 31–90 at step 7 (~9 dates). New scraper class attrs `coarse_days_ahead=90`, `coarse_step=7` (`scrapers/base.py:294`), overridable per airline. Full depth ≈ **29 dates**. Far-out award space moves slowly; a weekly tail loses little.

### 4.2 §2 — Depth-by-tier (the budget safety valve)
Add a `tier` parameter to `_scrape_window(today, scraper, tier=None)` and a `WINDOW_DEPTH` config (`settings.py`, after `CADENCE_*`). Effective horizon by tier:

| Tier | Horizon | ~dates | Earned by |
|---|---|---|---|
| HIGH | 90d (full 3-tier) | ~29 | demand ≥10 or volatile (existing promotion) |
| MED | ~45d | ~22 | default |
| LOW | 14d (dense only) | ~14 | cold/new long-tail |

Pass `tier=job.tier` from `refresh_airline_routes` (`scheduler.py:210`). `tier=None` falls back to full depth (back-compat for on-demand / coverage audits). With a realistic ~15/60/25 HIGH/MED/LOW mix, **avg ≈ 20 dates — unchanged from today** despite the 3× horizon. Pro on-demand search still covers any specific far date for a cold route on request. Tests: extend `test_scheduler.py` worst-case-fits-interval to iterate all three tier depths.

### 4.3 §3 — Route-seed expansion (`config/routes.py` + `config/airport_tz.py`)
Proportional growth, origins **and** destinations, international-first where the program books partner space. **Every new origin gets an `airport_tz.py` entry** (build-gated).

| Airline | Pairs now | Target | Focus |
|---|---|---|---|
| Alaska (httpx) | 74 | ~120 | intl partner space (SEA/SFO/LAX/PDX → NRT/HND/ICN/TPE/HKG/SYD/LHR) + new US origins (DEN/PHX/AUS/MSP) |
| JetBlue (httpx) | 27 | ~45 | transcon + Caribbean/LatAm + TATL partner |
| Delta (browser) | 50 | ~75 | hubs (ATL/DTW/MSP/SLC) + SkyTeam intl dests |
| Southwest (browser) | 42 | ~65 | domestic focus-city mesh (no intl; window stays near-term) |
| Turkish (browser) | 20 | ~30 | more US origins → IST |
| Etihad (browser) | 10 | ~16 | more US origins → AUH |

### 4.4 §4 — Per-airline shard/cadence sizing (`jobs`)
httpx airlines (cheap, richest intl partner space) get the **broad** deep window; browser airlines (expensive, WAF-capped) reserve the deep window for HIGH-tier and grow via shards. Sized from the §2 formula to hold each shard ≤45 min:

| Airline | Shards | Runs/day | Jobs/day | Note |
|---|---|---|---|---|
| Alaska | 3 → 3 | 3 → 3 | 9 | existing headroom covers 120 routes |
| JetBlue | 2 → 3 | 2 | 6 | + drop delay 12s→8s |
| Delta | 3 → **5** | 1 → **2** | 10 | Akamai cap is per-IP; add IPs, not bigger shards |
| Southwest | 5 → 6 | 1 | 6 | F5/Shape — add IPs, never push one |
| Turkish | 2 → 3 | 1 | 3 | |
| Etihad | 1 → **2** | 1 | 2 | finally sharded (removes single-IP-fate risk) |

~36 jobs/day (from 16). **Peak concurrency stays well under 20** by spreading crons across more UTC hours (today they cram 08–11; fan out to ~02–20) so no two multi-shard airlines overlap — peak ≈ 6. Shard count edits are paired: matrix array **and** `<AIRLINE>_SHARDS` env (mismatch corrupts the stride-shard). `jobs/.github/workflows/*`, `jobs/config/settings.py`.

### 4.5 §5 — Award rollout & validation
Phased per airline, **httpx first** (cheapest, highest intl payoff): for each new route — dispatch on-demand once → confirm the parser handles its response (international/connecting itineraries may surface shapes the parser hasn't seen — a real risk for `normalize()`) → watch Better Stack `blocked`/`scrape_run` → *then* commit the seed. Never bulk-add untested routes.

### 4.6 §6 — Cash coverage scaling (`scraper` + `fly.google.toml`)
The new routes auto-enroll for cash (once in `pp.flights`) but, being zero-demand, get crowded out by the 800-unit cap. A +60% route expansion (with international biz/first, plus planned O&D-connecting units) thins the ~2.3× margin toward ~1.0–1.25×. So:

1. **Pin the strategic new international routes** → add to `CASH_PINNED_ROUTES` (`config/routes.py:332-338`). Guaranteed day-one CPP regardless of demand — *the real fix for the Explore-facing routes.* Zero cost.
2. **Raise `CASH_TOP_ROUTES` 800 → ~1,200** (`fly.google.toml:16`). +50% selection; ~+50 min/day run time, fits the 4h cadence. No WAF wall.
3. **Tighten cadence if needed** (`CASH_REFRESH_INTERVAL_MIN` 240→180, `fly.google.toml:17`) — proven knob; gate on observed run duration (don't start runs back-to-back vs the 10-min `CASH_MIN_REST_MIN`).
4. **2nd gflights Fly machine — held in reserve**, gated on `units_dropped > 0` persisting AND a **2-machine egress-IP canary** (both sjc machines may NAT to one egress IP, so sharding may not help + risks Google bot-detection). ~$3–5/mo. The queue already supports parallel consumers (no per-consumer affinity).

Premium-cabin note: international business/first is the slow, every-4th-run demoted cabin. Pinned intl routes refresh premium CPP slower than economy — acceptable; revisit `CASH_PE_EVERY_N` only if intl premium CPP lags badly.

## 5. Known limits (accepted, not addressed here)
- **CPP far-tail:** cash window stays 30d. Award rows on days 31–90 keep `cpp=null` (Explore sorts on points). Extending `CASH_SCRAPE_DAYS` to 90 is a much heavier, separate Fly-throughput effort — out of scope. Documented in `settings.py`.
- **Cross-repo `airport_tz.py` vendoring:** the dict is vendored into `pp_db/` (and used by `api`/`jobs`). Update all copies; the hermetic test guards the scraper copy.

## 6. Risks
| Risk | Severity | Mitigation |
|---|---|---|
| New intl/connecting itineraries surface response shapes `normalize()` hasn't parsed | High | On-demand canary per route before seeding; per-airline phased rollout |
| Deep window × routes overruns a shard's GH Actions timeout | High | Depth-tiering keeps avg dates ~20; per-shard time budget (45 min) stops cleanly; size shards from the §2 formula |
| New origin missing from `airport_tz.py` → silent CPP/time drop | High | Hermetic build-gating test already exists; add every new origin |
| Shard matrix/env mismatch corrupts stride-shard | High | Edit matrix array + `<AIRLINE>_SHARDS` env together; covered in plan steps |
| Cash universe grows past capacity → 15–30d tail starves | Medium | Pin strategic routes; raise `CASH_TOP_ROUTES`; monitor `units_dropped`; shard in reserve |
| Peak GH Actions concurrency approaches 20 | Medium | Stagger crons across 02–20 UTC; peak ≈ 6 |
| gflights 2nd machine shares one sjc egress IP | Medium | 2-machine canary before committing; monitor for CAPTCHA/429 |
| Delta 5 shards × 2 runs raises Akamai exposure | Medium | Per-IP cap unchanged (18 < ~27 ceiling); validate block rate before/after |

## 7. Scope boundaries
- **In:** `scraper` (window tiers, depth-by-tier, route seeds, airport_tz, cash knobs, tests) + `jobs` (workflow shard/cron edits, per-shard caps).
- **Out:** `api`/`ui` (no change); cash 90d horizon; new airline onboarding; demand-driven auto-promotion of routes (a clean later add-on).

## 8. Decisions locked (2026-06-23)
- Award magnitude: **as proposed** (§4.3 table).
- Delta: **full** (3→5 shards, 1→2 runs/day, tiered deep window).
- Cash: **pin + capacity bump** now; 2nd-machine shard gated on canary + telemetry (cost is not the blocker — GH Actions is free for the public repo; the cash machine is the only $ item and the gate is the egress-IP question, not cost).
