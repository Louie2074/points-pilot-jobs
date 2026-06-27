# point-pilot-jobs

Scheduled maintenance jobs for point_pilot, run as GitHub Actions cron workflows.
Each job is a self-contained Python script that talks to the shared **Supabase Postgres**
database (the `pp` schema) through the vendored **`pp_db`** data layer (`DATABASE_URL`).

## Jobs

| Script | Workflow | Schedule | What it does |
|---|---|---|---|
| `transfer_bonuses.py` | `transfer-bonuses.yml` | 1st & 15th, 09:00 UTC | Scrapes current point-transfer bonuses from travel-on-points.com and snapshot-replaces the `pp.transfer_bonuses` table (atomic — one transaction). |
| `transfer_partners.py` | `transfer-partners.yml` | 1st & 15th, 10:00 UTC | Scrapes bank→airline transfer partners + ratios from thriftytraveler.com and full-table snapshot-replaces the `pp.transfer_partners` table (sole owner; atomic — one transaction). |
| `delta_browser_scrape.py` | `delta-browser-scrape.yml` | 2×/day (08:00, 20:00 UTC) + on-demand dispatch | `nodriver` browser scrape of Delta SkyMiles award space (Azure runner IP clears Akamai) → `pp.flights`. Sharded 5× (`shard: [0, 1, 2, 3, 4]`). |
| `southwest_browser_scrape.py` | `southwest-browser-scrape.yml` | daily 09:00 UTC + on-demand dispatch | `nodriver` browser scrape of Southwest Rapid Rewards award space (Azure runner IP mints the F5/Shape sensor) → `pp.flights`. Sharded 6× (`shard: [0, 1, 2, 3, 4, 5]`). |
| `turkish_browser_scrape.py` | `turkish-browser-scrape.yml` | daily 10:00 UTC + on-demand dispatch | `nodriver` browser scrape of Turkish Miles&Smiles award space, US↔IST (Azure runner IP clears the TLS-fingerprint block + PerimeterX) → `pp.flights`. Sharded 3× (`shard: [0, 1, 2]`). |
| `etihad_browser_scrape.py` | `etihad-browser-scrape.yml` | daily 11:00 UTC + on-demand dispatch | `nodriver` DOM scrape of Etihad Guest award space, US↔AUH (Azure runner IP clears Akamai + Imperva ABP) → `pp.flights`. Sharded 2× (`shard: [0, 1]`). |
| `alaska_scrape.py` | `alaska-scrape.yml` | 3×/day (01:17, 13:17, 19:17 UTC) | Plain **httpx** scrape (no browser) of Alaska Mileage Plan award space (Azure runner IP clears the Fastly WAF) → `pp.flights`. Sharded 3× (`shard: [0, 1, 2]`). Migrated off the always-on Fly box; the API box still runs the on-demand inline Alaska scrape independently. |
| `jetblue_scrape.py` | `jetblue-scrape.yml` | 2×/day (02:37, 14:37 UTC) | Plain **httpx** scrape (no browser) of JetBlue TrueBlue award space (clean from Azure IPs) → `pp.flights`. Sharded 3× (`shard: [0, 1, 2]`). Migrated off the always-on Fly box; the API box still runs the on-demand inline JetBlue scrape independently. |
| `cash_browser_scrape.py` | `cash-browser-scrape.yml` | 2×/day (06:15, 18:15 UTC) | `nodriver` browser scrape of **Google Flights cash fares** for all tracked carriers (Azure runner IP serves Google cleanly at volume) → matched to award flights → `pp.cash_fares` (powers CPP). Sharded 4× (`shard: [0, 1, 2, 3]`); a whole route stays on one shard. Migrated off the always-on `point-pilot-gflights` Fly box; both upsert the same key, so GA can bake in parallel with Fly. |
| `turkish_validate.py` | `turkish-validate.yml` | dispatch-only (no schedule) | Onboarding/regression check: runs the Turkish scraper against a few US↔IST routes under `xvfb` on the Azure IP and prints the records. No DB write. |
| `etihad_validate.py` | `etihad-validate.yml` | dispatch-only (no schedule) | Onboarding/regression check: runs the Etihad scraper against a couple of US↔AUH routes under `xvfb` on the Azure IP and prints the records. No DB write. |

<!-- coverage-expansion 2026-06-23 concurrency: award scrapers are staggered by UTC slot
(Alaska 01:17/13:17/19:17 ×3, JetBlue 02:37/14:37 ×3, Cash 06:15/18:15 ×4,
Delta 08:00/20:00 ×5, Southwest 09:00 ×6, Turkish 10:00 ×3, Etihad 11:00 ×2), so no two
multi-shard jobs share a slot and peak concurrency is about 6, well under the 20-job ceiling. -->

`obs.py` is the shared Better Stack shipper used by the transfer jobs; the browser
scrapers use the vendored `pipeline/obs.py`. `conftest.py` holds shared pytest fixtures.

Stale-row retention (formerly the daily `cleanup_flights.py` GH-Action) now runs as a Supabase
**pg_cron** job (`pp-retention`) in the Postgres database itself, so it isn't a job in this repo.

### Award scrapers (Delta / Southwest / Turkish / Etihad / Alaska / JetBlue)

Six airlines' award space is scraped here rather than in `points-pilot-scrapers` because their
sites block Fly/datacenter IPs (Akamai 444, F5/Shape, Imperva ABP, TLS fingerprinting, Fastly WAF)
but clear cleanly on GitHub's Azure runner IPs. They come in two flavours:

- **Browser scrapers** — **Delta / Southwest / Turkish / Etihad** (`*_browser_scrape.py`): a warmed
  headful Chrome (`nodriver`, under `xvfb`) is needed to clear the bot wall.
- **httpx scrapers** — **Alaska / JetBlue** (`alaska_scrape.py` / `jetblue_scrape.py`): plain httpx,
  no browser — the Azure IP alone clears the WAF. Migrated off the always-on `point-pilot-scraper`
  Fly box to free sharded GitHub Actions crons (probe 2026-06-21); the API box still runs each
  airline's on-demand inline scrape independently.

Each entrypoint is a **thin config** — its route list, `<AIRLINE>_*` env vars, and its
`scrapers/<airline>.py` Scraper class — calling the shared
[`browser_scrape_common.run_scrape()`](browser_scrape_common.py) (the module name is historical; the
two httpx scrapers reuse it too). The shared module owns the run plan (scored-queue cron drain /
single-route on-demand / sharding), the scrape loop, the `scrape_run` Better Stack metric, the
freshness snapshot, and the heartbeat ping, so all six behave identically.

Each entrypoint accepts on-demand `workflow_dispatch` inputs (`origin`, `destination`, `dates`) for
a single-route run, and `<AIRLINE>_SCRAPE_DAYS` / `<AIRLINE>_SHARDS` env tuning. **Sharding** (a
GH-Actions `matrix` over `<AIRLINE>_SHARD_INDEX`) splits the directed-leg catalogue across parallel
runners on distinct IPs — used where a single shard can't cover the catalogue under its per-IP WAF
cap (`<AIRLINE>_MAX_LEGS_PER_SHARD`, default 20): **Southwest** runs 6 shards, **Delta** 5,
**Alaska**, **JetBlue**, and **Turkish** 3, and **Etihad** 2. The `scrapers/browser.py` base +
`config/airport_tz.py` are vendored from
`points-pilot-scrapers`. Scraped rows are written to `pp.flights` in Supabase Postgres via the
vendored `pp_db` layer (`browser_scrape_common`'s `upsert_flights` + its freshness-snapshot probe
both go through `pp_db`).

**Adding a new no-login airline** is a documented recipe — see `CLAUDE.md`. (Several bank-partner
airlines were reconned and parked because they require login or wall the datacenter IP behind a
CAPTCHA; the recon notes live in the agent memory, not this repo.)

### Stale-row retention (now Supabase pg_cron)

Deleting every flight/cash_fare row older than yesterday (UTC) used to be the daily
`cleanup_flights.py` GitHub-Action. This retention now runs **inside the database** as a Supabase
**pg_cron** job (`pp-retention`), so there's no longer a script or workflow for it in this repo.

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

1. Install deps: `pip install -r requirements.txt` (this now includes the `pp_db` Postgres stack —
   `sqlalchemy` + `psycopg[binary]` + `asyncpg`).
2. Export the Supabase Postgres connection string (the Supavisor transaction-pooler URL, port 6543 —
   `pp_db.engine` reads it):
   ```bash
   export DATABASE_URL=postgresql://user:pw@aws-1-us-west-2.pooler.supabase.com:6543/postgres
   ```

### GitHub Actions

Add these as repository secrets (Settings → Secrets and variables → Actions):

| Secret | Required | Purpose |
|---|---|---|
| `DATABASE_URL` | yes | Supabase Postgres connection string (Supavisor transaction pooler, port 6543) — `pp_db.engine` reads it |
| `BETTERSTACK_SOURCE_TOKEN` | no | Enables the completion metric + log shipping; reuse the scraper's source token |
| `BONUSES_HEARTBEAT_URL` | no | Better Stack heartbeat for the transfer-bonuses run |
| `TRANSFER_PARTNERS_HEARTBEAT_URL` | no | Better Stack heartbeat for the transfer-partners run |
| `DELTA_HEARTBEAT_URL` | no | Better Stack heartbeat for the daily Delta browser scrape |
| `SOUTHWEST_HEARTBEAT_URL` | no | Better Stack heartbeat for the daily Southwest browser scrape |
| `TURKISH_HEARTBEAT_URL` | no | Better Stack heartbeat for the daily Turkish browser scrape |
| `ETIHAD_HEARTBEAT_URL` | no | Better Stack heartbeat for the daily Etihad browser scrape |
| `ALASKA_HEARTBEAT_URL` | no | Better Stack heartbeat for the Alaska httpx scrape |
| `JETBLUE_HEARTBEAT_URL` | no | Better Stack heartbeat for the JetBlue httpx scrape |

The workflows also expose a manual **Run workflow** button (`workflow_dispatch`)
with a `dry_run` toggle.
