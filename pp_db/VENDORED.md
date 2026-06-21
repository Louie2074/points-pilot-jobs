# Vendored pp_db (runtime subset)

Canonical source: the **scraper** repo's `pp_db/` package (the MotherDuck→Postgres data layer).
This is the same managed-subset vendoring used for `db/`, `config/`, `pipeline/`, `scrapers/`
(see api/CLAUDE.md). Only the **runtime** modules are vendored here — `models`, `engine`,
`airport_tz`, and the `queries*` ports. The flip-time infra (`migrations/`, `backfill.py`,
`sql/`, `tests/`, `alembic.ini`) lives ONLY in the canonical package and is run there.

Sync rule: fix the data layer in the scraper's `pp_db/` FIRST, then re-copy the runtime files here.
