# Cash And Award Coverage Bump Design

**Date:** 2026-07-01

## Goal

Increase cash CPP density and reduce award route queue wait time for the carriers that currently
have headroom: cash/Google Flights, Alaska, Delta, and JetBlue.

## Current Signals

- GitHub Actions scrapers are stable: recent scheduled runs for cash, Alaska, JetBlue, Delta,
  Southwest, Turkish, and Etihad completed successfully.
- Fly `point-pilot-gflights` is stopped; GitHub Actions owns cash scraping.
- Cash coverage improved after the 6-shard migration, but Alaska/JetBlue/Etihad still have
  sub-80% route/date/cabin cash-unit density versus live award units.
- Alaska and Delta still have material due queue debt, and their recent runs completed cleanly.
- Turkish has a separate data issue: green workflow but zero current live rows. This change does
  not scale Turkish.

## Scope

1. Cash scraper:
   - Keep 6 shards.
   - Raise `CASH_TOP_ROUTES` from `600` to `800`.
   - Move scheduled cash from 2x/day to 3x/day at staggered UTC slots.
   - Keep `CASH_SCRAPE_DAYS=30`.

2. Award scraper capacity:
   - Alaska: increase Actions matrix and `ALASKA_SHARDS` from 3 to 4.
   - Delta: increase Actions matrix and `DELTA_SHARDS` from 5 to 6.
   - JetBlue already runs 4 shards; keep shard count unchanged.

3. Route expansion:
   - Add a measured set of international route pairs to `config/routes.py`.
   - Preserve exact-pair dedupe and bidirectional seeding behavior.
   - Add cash-pinned routes for the highest-value new international pairs so CPP fills early.

4. Tooling hygiene needed to make the repo's documented checks work:
   - Add `PyYAML` to `requirements.txt` because workflow tests import `yaml`.
   - Configure `ruff` to exclude vendored `pp_db/`, matching `CLAUDE.md`.
   - Add timezone guard tests for both `config.airport_tz` and `pp_db.airport_tz`.

## Route Additions

### Alaska

Add 16 partner international pairs, all MED tier:

- Aer Lingus: `JFK-DUB`, `BOS-DUB`, `ORD-DUB`, `IAD-DUB`
- Condor: `SEA-FRA`, `PDX-FRA`, `SFO-FRA`, `LAX-FRA`
- Icelandair: `SEA-KEF`, `PDX-KEF`, `JFK-KEF`, `BOS-KEF`
- ITA: `JFK-FCO`, `BOS-FCO`, `LAX-FCO`, `SFO-FCO`

Alaska source guardrail: Alaska Atmos lists Aer Lingus, Condor, Icelandair, and ITA as earn/redeem
partners, alongside oneworld partners.

### Delta

Add 11 Delta international pairs, all MED tier:

- `SEA-FCO`, `SEA-BCN`
- `JFK-OPO`, `JFK-MLA`, `JFK-OLB`
- `BOS-MAD`, `BOS-NCE`, `BOS-BCN`, `BOS-MXP`
- `JFK-CTA`
- `MSP-CPH`

Delta source guardrails: Delta News Hub has official route announcements for JFK-OPO/MLA/OLB,
SEA-FCO/BCN, Boston-Europe expansions, JFK-CTA, and MSP-CPH.

### JetBlue

Add 10 JetBlue transatlantic pairs, all MED tier:

- `JFK-DUB`, `BOS-DUB`
- `JFK-EDI`, `BOS-EDI`
- `JFK-LGW`, `BOS-LGW`
- `JFK-MAD`, `BOS-MAD`
- `BOS-BCN`
- `BOS-MXP`

JetBlue source guardrail: JetBlue's Europe page lists these JFK/BOS Europe markets and Mint fares.

## Timezone Requirements

Every airport appearing in seeded routes must exist in both timezone maps:

- `config/airport_tz.py`
- `pp_db/airport_tz.py`

This is required because award departure-time normalization and cash matching use different copies.
The current `pp_db` copy is missing some already-seeded airports; fix all missing entries, not only
the newly added airports.

## Tests And Validation

Local tests:

- Workflow YAML tests must assert cash schedule/top-routes and AS/DL shard matrix consistency.
- Route-count tests must update exact counts.
- Timezone tests must assert both maps cover every seeded airport.
- Run `pytest tests/ -q`.
- Run `ruff check .`.

Live validation after merge:

- Merge to `origin/main` in `points-pilot-jobs`.
- Dispatch `cash-browser-scrape.yml`; confirm all shards succeed and route/date/cabin units rise.
- Dispatch or wait for Alaska and Delta scheduled workflows; confirm green runs with the new shard
  matrices.
- Query Supabase after runs to verify cash freshness and AS/DL due queue reduction.

## Out Of Scope

- Turkish scaling, because its current issue is not capacity.
- Extending cash to 60-90 days; keep the near-term 30-day CPP window dense first.
- Southwest changes; live-route coverage is thin, but this request is focused on cash, Delta,
  Alaska, and JetBlue international density.
