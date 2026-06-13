# Vendored Delta scraper (for delta-browser-scrape)

`scrapers/`, `config/`, `db/`, `pipeline/` here let this repo run the nodriver Delta browser
scrape **self-contained** (GH Actions Azure IP clears Akamai where Fly's IP gets HTTP 444), with
no cross-repo checkout / PAT.

Files: `scrapers/{base,browser,delta}.py`, `config/{settings,airport_tz}.py`,
`db/{connection,queries,schema}.py`, `pipeline/normalizer.py`, `pipeline/obs.py`.
Entry point: `delta_browser_scrape.py`.

`config/airport_tz.py` (copy of the scraper repo's) maps airport → IANA tz so `delta.py`
stores tz-aware local departure/arrival times (a naive value would land in the TIMESTAMPTZ
column as UTC, shifting every time by the airport's offset). Re-sync if it changes upstream.

`pipeline/obs.py` is a copy of the scraper repo's (canonical there) — it gives the Delta run
Better Stack log shipping + a `scrape_run` metric (service `point-pilot-delta`) at parity with the
Alaska/JetBlue + Google Flights scrapers. Re-sync if it changes upstream.

**`scrapers/browser.py`, `scrapers/delta.py`, and `scrapers/southwest.py` are CANONICAL HERE** — the Delta
and Southwest browser scrapers are maintained in this repo (the scraper repo no longer carries these).
The other files (`base.py`, `config/settings.py`, `db/*`, `pipeline/normalizer.py`) are copies of
**points-pilot-scrapers** shared modules — those stay canonical in the scraper repo (Alaska/JetBlue
use them), so re-sync them here if they change upstream. Editing the shared copies without
propagating is the footgun.
