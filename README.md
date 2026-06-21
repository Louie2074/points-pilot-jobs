# point-pilot-jobs

Scheduled maintenance jobs for point_pilot, run as GitHub Actions cron workflows.
Each job is a self-contained Python script that talks to the shared MotherDuck
database (`md:point_pilot`).

## Jobs

| Script | Workflow | Schedule | What it does |
|---|---|---|---|
| `transfer_bonuses.py` | `transfer-bonuses.yml` | 1st & 15th, 09:00 UTC | Scrapes current point-transfer bonuses from travel-on-points.com and snapshot-replaces the `transfer_bonuses` table. |
| `transfer_partners.py` | `transfer-partners.yml` | 1st & 15th, 10:00 UTC | Scrapes bank→airline transfer partners + ratios from thriftytraveler.com and full-table snapshot-replaces the `transfer_partners` table (sole owner). |
| `delta_browser_scrape.py` | `delta-browser-scrape.yml` | daily 08:00 UTC + on-demand dispatch | `nodriver` browser scrape of Delta SkyMiles award space (Azure runner IP clears Akamai) → `flights`. |
| `southwest_browser_scrape.py` | `southwest-browser-scrape.yml` | daily 09:00 UTC + on-demand dispatch | `nodriver` browser scrape of Southwest Rapid Rewards award space (Azure runner IP mints the F5/Shape sensor) → `flights`. |
| `turkish_browser_scrape.py` | `turkish-browser-scrape.yml` | daily 10:00 UTC + on-demand dispatch | `nodriver` browser scrape of Turkish Miles&Smiles award space, US↔IST (Azure runner IP clears the TLS-fingerprint block + PerimeterX) → `flights`. |
| `etihad_browser_scrape.py` | `etihad-browser-scrape.yml` | daily 11:00 UTC + on-demand dispatch | `nodriver` DOM scrape of Etihad Guest award space, US↔AUH (Azure runner IP clears Akamai + Imperva ABP) → `flights`. |
| `turkish_validate.py` | `turkish-validate.yml` | dispatch-only (no schedule) | Onboarding/regression check: runs the Turkish scraper against a few US↔IST routes under `xvfb` on the Azure IP and prints the records. No DB write (dummy token). |
| `etihad_validate.py` | `etihad-validate.yml` | dispatch-only (no schedule) | Onboarding/regression check: runs the Etihad scraper against a couple of US↔AUH routes under `xvfb` on the Azure IP and prints the records. No DB write (dummy token). |

`obs.py` is the shared Better Stack shipper used by the transfer jobs; the browser
scrapers use the vendored `pipeline/obs.py`. `conftest.py` holds shared pytest fixtures.

Stale-row retention (formerly the daily `cleanup_flights.py` GH-Action) now runs as a Supabase
**pg_cron** job (`pp-retention`) in the Postgres database itself, so it isn't a job in this repo.

### Award browser scrapers (Delta / Southwest / Turkish / Etihad)

Four airlines' award space is scraped here rather than in `points-pilot-scrapers` because their
sites block Fly/datacenter IPs (Akamai 444, F5/Shape, Imperva ABP, TLS fingerprinting) but clear
cleanly on GitHub's Azure runner IPs in a warmed headful Chrome (`nodriver`, under `xvfb`). Each
`*_browser_scrape.py` entrypoint is a **thin config** — its route list, `<AIRLINE>_*` env vars, and
its `scrapers/<airline>.py` Scraper class — calling the shared
[`browser_scrape_common.run_scrape()`](browser_scrape_common.py). The shared module owns the run
plan (cron stride / single-route on-demand / sharding), the scrape loop, the `scrape_run` Better
Stack metric, the freshness snapshot, and the heartbeat ping, so all four behave identically.

Each entrypoint accepts on-demand `workflow_dispatch` inputs (`origin`, `destination`, `dates`) for
a single-route run, and `<AIRLINE>_SCRAPE_DAYS` / `<AIRLINE>_SHARDS` env tuning. **Sharding** (a
GH-Actions `matrix` over `<AIRLINE>_SHARD_INDEX`) splits the directed-leg catalogue across parallel
runners on distinct IPs — used where a single shard can't cover the catalogue under its per-IP WAF
cap (`<AIRLINE>_MAX_LEGS_PER_SHARD`, default 20): **Delta** and **Southwest** run 3 shards, **Turkish**
2 (its 40 legs > the 20-leg cap). **Etihad** runs **single-shard** by design — its 20 directed legs
fit one shard's cap — so its workflow intentionally has no `matrix`. The `scrapers/browser.py` base +
`config/airport_tz.py` are vendored from `points-pilot-scrapers`.

**Adding a new no-login airline** is a documented recipe — see `CLAUDE.md`. (Several bank-partner
airlines were reconned and parked because they require login or wall the datacenter IP behind a
CAPTCHA; the recon notes live in the agent memory, not this repo.)

### Stale-row retention (now Supabase pg_cron)

Deleting every flight/cash_fare row older than yesterday (UTC) used to be the daily
`cleanup_flights.py` GitHub-Action. After the MotherDuck → Supabase Postgres cutover this
retention runs **inside the database** as a Supabase **pg_cron** job (`pp-retention`), so there's
no longer a script or workflow for it in this repo.

### `transfer_bonuses.py`

Scrapes the current point-transfer bonuses from travel-on-points.com and
snapshot-replaces the `pp.transfer_bonuses` Postgres table (atomically, in one transaction) for
every airline in `transfer_partners`. Like the award scrapers, it drives **headful Chrome via `nodriver`**
(the table is JS-rendered), so its workflow runs `browser-actions/setup-chrome` first.
Fail-closed: any HTTP non-2xx or parse error raises and exits
non-zero (workflow failure). Zero active bonuses is valid — it deletes all tracked
bonuses and inserts nothing.

```bash
python transfer_bonuses.py            # scrape + snapshot-replace
python transfer_bonuses.py --dry-run  # fetch + parse, skip the DELETE/INSERT
```

Ships a `transfer_bonuses_run` completion metric + WARNING+ logs to Better Stack when
`BETTERSTACK_SOURCE_TOKEN` is set (via `obs.py`); no token → no-op.

### `transfer_partners.py`

Scrapes bank→airline transfer partners and ratios from thriftytraveler.com and
full-table snapshot-replaces the `pp.transfer_partners` Postgres table (atomically, in one
transaction). Drives
**headful Chrome via `nodriver`** (JS-rendered tables; workflow runs
`browser-actions/setup-chrome`). This job is the **sole owner** of that table — the
scraper no longer seeds it. Coverage is
gated to the already-tracked IATA airline set; hotel rows, untracked airlines, and
the Rove + Marriott sections are skipped. Ratios are read as `bank : partner` and
stored as `bank ÷ partner` (bank points per mile).

Fail-closed: HTTP non-2xx or a page with no managed bank tables raises and exits
non-zero (workflow failure). A bank section that maps to zero rows is tolerated
(pure snapshot).

```bash
python transfer_partners.py            # scrape + snapshot-replace
python transfer_partners.py --dry-run  # fetch + parse, skip the DELETE/INSERT
```

Ships a `transfer_partners_run` completion metric (`ok`, `deleted`, `inserted`,
`banks_found`, `banks_missing`, `airline_rows_seen`, `rows_skipped_hotel`,
`rows_skipped_unmapped`, `rows_ratio_dropped`, `duration_s`, `dry_run`) plus
per-bank parse breakdowns in the logs when `BETTERSTACK_SOURCE_TOKEN` is set.
Optional `TRANSFER_PARTNERS_HEARTBEAT_URL` pings on a successful real run.

## Setup

1. Install deps: `pip install -r requirements.txt`
2. Export a MotherDuck token (the `duckdb` package picks it up automatically):
   ```bash
   export MOTHERDUCK_TOKEN=...   # https://app.motherduck.com/settings/tokens
   ```

### GitHub Actions

Add these as repository secrets (Settings → Secrets and variables → Actions):

| Secret | Required | Purpose |
|---|---|---|
| `MOTHERDUCK_TOKEN` | yes | MotherDuck access (`duckdb` reads it automatically) |
| `BETTERSTACK_SOURCE_TOKEN` | no | Enables the completion metric + log shipping; reuse the scraper's source token |
| `CLEANUP_HEARTBEAT_URL` | no | Better Stack heartbeat — a missed/failed daily cleanup then alerts |
| `BONUSES_HEARTBEAT_URL` | no | Better Stack heartbeat for the transfer-bonuses run |
| `TRANSFER_PARTNERS_HEARTBEAT_URL` | no | Better Stack heartbeat for the transfer-partners run |
| `DELTA_HEARTBEAT_URL` | no | Better Stack heartbeat for the daily Delta browser scrape |
| `SOUTHWEST_HEARTBEAT_URL` | no | Better Stack heartbeat for the daily Southwest browser scrape |
| `TURKISH_HEARTBEAT_URL` | no | Better Stack heartbeat for the daily Turkish browser scrape |
| `ETIHAD_HEARTBEAT_URL` | no | Better Stack heartbeat for the daily Etihad browser scrape |

The workflows also expose a manual **Run workflow** button (`workflow_dispatch`)
with a `dry_run` toggle.
