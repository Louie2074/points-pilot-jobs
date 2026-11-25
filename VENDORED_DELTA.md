# Vendored browser scrapers (Delta / Southwest / Turkish / Etihad)

`scrapers/`, `config/`, `pp_db/`, `pipeline/` here let this repo run the nodriver Delta,
Southwest, Turkish, and Etihad browser scrapes **self-contained** (GH Actions Azure IP clears
Akamai / Imperva / mints the F5/Shape sensor where Fly's IP gets blocked), with no cross-repo
checkout / PAT.

Files: `scrapers/{base,browser,delta,southwest,turkish,etihad}.py`, `config/{settings,airport_tz}.py`,
`pipeline/normalizer.py`, `pipeline/obs.py`.
Entry points: `delta_browser_scrape.py`, `southwest_browser_scrape.py`,
`turkish_browser_scrape.py`, `etihad_browser_scrape.py`.

**Data layer:** scrape rows are written to **Supabase Postgres** (`pp.flights`) through the
vendored **`pp_db`** package (see `pp_db/VENDORED.md`) — the sync entry points use its
`autocommit` facade.

`config/airport_tz.py` (copy of the scraper repo's) maps airport → IANA tz so `delta.py`
stores tz-aware local departure/arrival times (a naive value would land in the TIMESTAMPTZ
column as UTC, shifting every time by the airport's offset). Re-sync if it changes upstream.

`pipeline/obs.py` is a copy of the scraper repo's (canonical there) — it gives each browser scrape
Grafana OTLP log shipping and a `scrape_run` event (per-airline service `point-pilot-delta` /
`-southwest` / `-turkish` / `-etihad`, set in each `*_browser_scrape.py`) at parity with the
Alaska/JetBlue + Google Flights scrapers. Re-sync if it changes upstream.

**`scrapers/browser.py`, `scrapers/delta.py`, `scrapers/southwest.py`, `scrapers/turkish.py`, and
`scrapers/etihad.py` are CANONICAL HERE** — the Delta, Southwest, Turkish, and Etihad browser
scrapers are maintained in this repo (the scraper repo no longer carries these).
The other files (`base.py`, `config/settings.py`, `pp_db/*`, `pipeline/normalizer.py`) are copies of
**points-pilot-scrapers** shared modules — those stay canonical in the scraper repo (Alaska/JetBlue
use them), so re-sync them here if they change upstream. Editing the shared copies without
propagating is the footgun.
