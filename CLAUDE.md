# points-pilot-jobs — agent guide

Scheduled GitHub Actions jobs for point_pilot. Two kinds of job, all writing to the shared
MotherDuck DB (`md:point_pilot`):

1. **Maintenance** — `transfer_bonuses.py` + `transfer_partners.py` (scrape bank/airline transfer
   data, snapshot-replace tables). (Stale-flight retention is now a Supabase pg_cron job,
   `pp-retention`, not a GH-Action — the former `cleanup_flights.py` was removed in the cutover.)
2. **Award browser scrapers** — `delta` / `southwest` / `turkish` / `etihad` `_browser_scrape.py`:
   `nodriver` (headful Chrome via CDP, under `xvfb`) scrapes of airlines whose sites block Fly's
   datacenter IP but clear on GitHub's Azure runner IPs.

Start with `README.md` for the job catalogue + schedules. This file is the working guide.

## Commands

| Task | Command |
|---|---|
| Tests | `MOTHERDUCK_TOKEN=dummy pytest tests/ -q` (import-time settings gate needs the var; no real DB hit) |
| Lint | `ruff check .` |
| Run a maintenance job | `python transfer_partners.py --dry-run` (or `transfer_bonuses.py`) |
| Validate an award scraper (no DB) | `MOTHERDUCK_TOKEN=dummy python turkish_validate.py` / `etihad_validate.py` (or dispatch `*-validate.yml`) |
| Run an award scrape on-demand | dispatch `<airline>-browser-scrape.yml` with `origin`/`destination`/`dates` inputs |

The local shell may print `blake2`/`hashlib` errors from the pyenv 3.11.1 Python — they're benign
noise; tests still run. CI uses Python 3.11 with `MOTHERDUCK_TOKEN=dummy`.

## Layout

- `*_browser_scrape.py` — thin per-airline entrypoints (route list + `<AIRLINE>_*` env + Scraper
  class) calling **`browser_scrape_common.run_scrape()`**, which owns the run plan, scrape loop,
  `scrape_run` metric, freshness snapshot, and heartbeat. Don't duplicate that logic — extend the
  shared module.
- `*_validate.py` — thin per-airline entrypoints (scraper factory + routes + watchdog) over
  **`validate_common.run_validation()`** — the no-DB, dispatch-only `workflow_dispatch` check that
  scrapes a couple of routes and prints the records. Same rule: extend the shared harness, don't
  copy it per airline.
- `scrapers/` — `base.py` (`FlightRecord` + `BaseScraper` + `ScraperBlockedError`), `browser.py`
  (`BrowserScraper`: spawns Chrome, warms a page, runs an in-page `fetch`/DOM-read), and one module
  per airline. `browser.py` + `config/` + `db/` + `pipeline/` are **vendored from
  `points-pilot-scrapers`** — fix there first, then propagate (see `VENDORED_DELTA.md`).
- `config/airport_tz.py` — IATA→IANA timezone map. **A new ORIGIN airport must be added here** or
  the scraper drops its local departure times (foreign destinations may stay unmapped — the time is
  just dropped, not fatal).
- `tests/` — offline `normalize()` tests per scraper (fixtures from real captured responses) +
  `_build_plan`/`_parse_dates_csv` tests for the entrypoints. Hermetic; no live DB.

## Onboarding a new no-login award airline (the proven recipe)

Turkish and Etihad were added this way. Mirror `scrapers/etihad.py` + `etihad_browser_scrape.py` +
`tests/test_etihad.py` + `.github/workflows/etihad-browser-scrape.yml`.

1. **Capture the award flow on the Azure IP, not residentially.** Many sites (Imperva ABP, Akamai)
   block a residential CDP top-nav but clear on the GH-Actions runner. Drive the real award search
   in a `workflow_dispatch` recon script (headful Chrome under `xvfb`); intercept both the
   page-context `fetch`/XHR **and** the CDP Network layer (`Network.enable` +
   `setBypassServiceWorker(true)` — some sites fetch availability from a service worker invisible to
   a page patch). If no API surfaces, DOM-scrape the rendered result cards (Etihad does this).
   Dump the result HTML as an artifact and design selectors offline.
2. **Build `scrapers/<airline>.py`** (`BrowserScraper` subclass): warm the right page → **ONE**
   `tab.evaluate` per scrape (in-page fetch or DOM read + any challenge retry) → `normalize()` into
   `FlightRecord`s, **one record per (itinerary × priced cabin)** with the correct per-cabin price.
3. **Thin entrypoint** `<airline>_browser_scrape.py` (copy etihad's): routes + env + `run_scrape()`.
   Add `tests/test_<airline>.py` (offline normalize) + `.github/workflows/<airline>-browser-scrape
   .yml` (staggered cron) + a `<airline>_validate.py` no-DB check (a thin entrypoint over
   `validate_common.run_validation` — scraper factory + routes + watchdog, copy etihad's). Add new
   origin airports to `config/airport_tz.py`. Update `README.md`.
4. **Validate on the Azure IP** (no-DB validate → real on-demand DB run → check MotherDuck rows:
   `SELECT cabin_class,count(*),min(points_cost),max(points_cost) FROM flights WHERE source='<x>'
   GROUP BY 1`; business should exceed economy on a long-haul).

## Hard-won gotchas

- **websockets pin** (`requirements.txt`): `websockets>=14,<15`. 16.0 silently breaks nodriver's
  background listener mid-scrape (`cannot call get() concurrently`) and hangs to job timeout.
- **ONE `tab.evaluate` per scrape.** Multiple concurrent CDP ops in one scrape trip nodriver's
  listener — do both cabins + any retry inside a single in-page script.
- **`os._exit(0)` at the end** of each entrypoint: nodriver leaves keepalive tasks so the process
  never exits on its own (the GH-Actions step would hang to its timeout).
- **Per-cabin pricing trap:** an option's "from"/cheapest price is the cheapest *cabin*, not each
  cabin — read the per-cabin price node, or business gets stamped with the economy price. Verify
  per-cabin prices visually before trusting a new scraper.
- **A killed step drops its logs.** For recon, use `xvfb-run -a python -u …` + `os._exit`, and fetch
  via `gh api repos/<owner>/points-pilot-jobs/actions/jobs/<job_id>/logs` when `gh run view --log`
  flakes.
- **Not every airline is onboardable here.** Some require a frequent-flyer login (Avianca, Virgin)
  or wall the datacenter IP behind a CAPTCHA/Access-Denied (American, Air France, Qantas). Those are
  parked; reaching them needs a residential/proxy egress or stored logins. Don't burn many cycles —
  time-box form-driving and park with notes.

## Conventions

- `gh` is authed as `Louie2074`; this repo is **public** (unlimited Actions minutes). Workflows must
  be on the default branch before `workflow_dispatch` sees them.
- Commit per-repo with explicit `git add`. End commit messages with the project's `Co-Authored-By`
  trailer. Don't push to `main` unprompted beyond the scope you were asked to do.
