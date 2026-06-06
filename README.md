# point-pilot-jobs

Scheduled maintenance jobs for point_pilot, run as GitHub Actions cron workflows.
Each job is a self-contained Python script that talks to the shared MotherDuck
database (`md:point_pilot`).

## Jobs

| Script | Workflow | Schedule | What it does |
|---|---|---|---|
| `cleanup_flights.py` | `cleanup-flights.yml` | daily 03:15 UTC | Deletes rows from `flights` whose departure `date` is older than yesterday (UTC). |
| `transfer_bonuses.py` | `transfer-bonuses.yml` | 1st & 15th, 09:00 UTC | Scrapes current point-transfer bonuses from frequentmiler.com and snapshot-replaces the `transfer_bonuses` table. |

`obs.py` is the shared Better Stack shipper used by both jobs; `conftest.py` holds
shared pytest fixtures.

### `cleanup_flights.py`

Deletes every flight older than yesterday (UTC) — keeps yesterday plus all future
dates. Cleanup is anchored to the flight `date`, not `expires_at` (which is only a
scrape-freshness TTL); this logic was moved out of the scraper (now a pure write
pipeline) into this repo.

```bash
python cleanup_flights.py            # delete stale rows
python cleanup_flights.py --dry-run  # report how many would be deleted, delete nothing
```

**Observability (optional).** When `BETTERSTACK_SOURCE_TOKEN` is set, each run ships
a `cleanup_flights_run` completion metric to Better Stack (`ok`, `deleted`,
`duration_s`, `dry_run`) plus WARNING+ logs (failures with tracebacks), via direct
HTTPS POST — see `obs.py`. Reuse the scraper's source token so events land in the
same source; they're tagged `service=points-pilot-jobs`. No token → no-op.

### `transfer_bonuses.py`

Scrapes the current point-transfer bonuses from frequentmiler.com and
snapshot-replaces the `transfer_bonuses` table in MotherDuck for every airline in
`transfer_partners`. Fail-closed: any HTTP non-2xx or parse error raises and exits
non-zero (workflow failure). Zero active bonuses is valid — it deletes all tracked
bonuses and inserts nothing.

```bash
python transfer_bonuses.py            # scrape + snapshot-replace
python transfer_bonuses.py --dry-run  # fetch + parse, skip the DELETE/INSERT
```

Same observability contract as `cleanup_flights.py` (ships a completion metric +
WARNING+ logs when `BETTERSTACK_SOURCE_TOKEN` is set).

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

The workflows also expose a manual **Run workflow** button (`workflow_dispatch`)
with a `dry_run` toggle.
