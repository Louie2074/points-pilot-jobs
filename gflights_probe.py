"""Azure-IP probe for the Google Flights cash scraper.

Question this answers: does Google Flights serve real results to our headful-Chrome cash
scraper from a GitHub-Actions (Azure) IP, or does it wall us (consent / "unusual traffic" /
CAPTCHA)? This gates whether the cash scraper can move off Fly onto sharded GH Actions like the
award scrapers did. It is a READ-ONLY probe: it calls the real ``GoogleFlightsScraper`` (vendored
byte-identical from points-pilot-scrapers) and writes nothing to the database.

Signal: the always-busy domestic routes (SFO-JFK, ORD-LAX, ATL-MCO) MUST return fares if Google
is serving us. If those come back empty / blocked, the Azure IP is walled. Healthy counts on them
= PASS. We also probe intl-business + a premium-economy (tfs-protobuf) unit since those are the
cash-coverage gaps the move is meant to fill.
"""

from __future__ import annotations

import logging
import sys
import time
from datetime import date, timedelta

from scrapers.base import ScraperBlockedError
from scrapers.google_flights import GoogleFlightsScraper

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
log = logging.getLogger("gflights_probe")

TODAY = date.today()


def _d(days: int) -> date:
    return TODAY + timedelta(days=days)


# (origin, dest, date, cabin, must_have_fares) — must_have_fares routes are the wall detector.
UNITS = [
    ("SFO", "JFK", _d(10), "economy", True),
    ("ORD", "LAX", _d(12), "economy", True),
    ("ATL", "MCO", _d(9), "economy", True),
    ("JFK", "SFO", _d(14), "business", False),
    ("JFK", "LHR", _d(21), "business", False),
    ("LAX", "HND", _d(30), "business", False),
    ("SEA", "LHR", _d(28), "premium_economy", False),
    ("SFO", "JFK", _d(10), "economy", True),  # repeat: catch rate-limit ramp
]


def main() -> int:
    scraper = GoogleFlightsScraper()
    results = []
    t0 = time.monotonic()
    try:
        for i, (o, dst, dt, cabin, must) in enumerate(UNITS, 1):
            tag = f"{o}->{dst} {dt.isoformat()} {cabin}"
            u0 = time.monotonic()
            try:
                fares = scraper.scrape_fares(o, dst, dt, cabin=cabin)
                n = len(fares)
                status = "OK" if n > 0 else ("EMPTY" if not must else "EMPTY!")
                results.append((tag, must, n, status))
                print(
                    f"PROBE unit {i}/{len(UNITS)}: {tag} -> {n} fares "
                    f"[{status}] ({time.monotonic() - u0:.0f}s)",
                    flush=True,
                )
            except ScraperBlockedError as exc:
                results.append((tag, must, -1, "BLOCKED"))
                print(f"PROBE unit {i}/{len(UNITS)}: {tag} -> BLOCKED ({exc})", flush=True)
            except Exception as exc:  # noqa: BLE001 — probe must finish + report
                results.append((tag, must, -2, "ERROR"))
                print(f"PROBE unit {i}/{len(UNITS)}: {tag} -> ERROR ({exc!r})", flush=True)
    finally:
        for closer in ("close", "stop", "shutdown"):
            fn = getattr(scraper, closer, None)
            if callable(fn):
                try:
                    fn()
                except Exception:  # noqa: BLE001
                    pass
                break

    # Verdict: the wall detector is the must-have-fares (busy domestic) units.
    detectors = [r for r in results if r[1]]
    detectors_ok = [r for r in detectors if r[2] > 0]
    blocked = [r for r in results if r[3] == "BLOCKED"]
    any_fares = sum(1 for r in results if r[2] > 0)
    total_fares = sum(r[2] for r in results if r[2] > 0)

    print("\n===== PROBE SUMMARY =====", flush=True)
    print(f"elapsed: {time.monotonic() - t0:.0f}s | units: {len(results)}", flush=True)
    print(
        f"busy-domestic detectors serving: {len(detectors_ok)}/{len(detectors)} | "
        f"units with fares: {any_fares}/{len(results)} | total fares: {total_fares} | "
        f"hard-blocked: {len(blocked)}",
        flush=True,
    )
    if len(detectors_ok) == len(detectors) and not blocked:
        print("PROBE VERDICT: PASS — Google Flights serves this Azure IP. Cash→GA is viable.", flush=True)
        return 0
    if not detectors_ok:
        print("PROBE VERDICT: BLOCKED — busy routes return nothing from Azure. Cash stays on Fly.", flush=True)
        return 2
    print("PROBE VERDICT: PARTIAL — mixed signal, inspect per-unit output above.", flush=True)
    return 3


if __name__ == "__main__":
    sys.exit(main())
