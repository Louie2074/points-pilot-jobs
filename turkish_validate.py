"""Validation harness for the Turkish Miles&Smiles scraper (no DB write).

Runs TurkishScraper against US->IST on the GitHub Actions (Azure) IP and prints the records, to
confirm end-to-end award-data extraction (warm session + in-page availability fetch clears the
TLS-fingerprint + PerimeterX wall). Exits non-zero on no records. faulthandler dumps stacks if it
hangs, so a stuck browser op is visible in the log. Imports need MOTHERDUCK_TOKEN (import-time
settings gate) — set a dummy; this never touches the DB.
"""

import faulthandler
import logging
import sys
from datetime import date, timedelta

faulthandler.enable()
# If the run is still alive after 90s/180s (i.e. a browser op is hung), dump every thread's stack
# to stderr so the log shows exactly where it's stuck.
faulthandler.dump_traceback_later(90, repeat=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("turkish_validate")


def p(msg: str) -> None:
    print(f">>> {msg}", flush=True)


p("importing TurkishScraper")
from scrapers.turkish import TurkishScraper  # noqa: E402

# Start tiny: one route, one date, to isolate the transport before scaling up.
ROUTES = [("SEA", "IST")]


def main() -> None:
    dt = date.today() + timedelta(days=21)
    p("instantiating TurkishScraper")
    sc = TurkishScraper()
    total = 0
    try:
        for origin, dest in ROUTES:
            p(f"scrape {origin}-{dest} {dt} START")
            try:
                recs = sc.scrape(origin, dest, dt)
            except Exception as exc:  # noqa: BLE001
                log.error("scrape %s-%s %s FAILED: %s", origin, dest, dt, exc, exc_info=True)
                continue
            p(f"scrape {origin}-{dest} {dt} DONE -> {len(recs)} records")
            total += len(recs)
            for r in recs[:5]:
                log.info(
                    "    %-9s %s->%s  %6d pts  seats=%s stops=%s dep=%s  %s",
                    r.cabin_class, r.origin, r.destination, r.points_cost,
                    r.available_seats, r.stops, r.departure_time_local, r.raw_flight_number,
                )
    finally:
        p("closing scraper")
        sc.close()

    log.info("TOTAL records: %d", total)
    if total == 0:
        log.error("VALIDATION FAILED — 0 records")
        sys.exit(1)
    log.info("VALIDATION OK")


if __name__ == "__main__":
    main()
