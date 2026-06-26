"""Azure-IP probe for the Google Flights cash scraper.

Question this answers: does Google Flights serve real results to our headful-Chrome cash
scraper from a GitHub-Actions (Azure) IP, or does it wall us (consent / "unusual traffic" /
CAPTCHA)? This gates whether the cash scraper can move off Fly onto sharded GH Actions like the
award scrapers did. READ-ONLY: calls the real ``GoogleFlightsScraper`` (vendored byte-identical
from points-pilot-scrapers) and writes nothing to the database.

Signal: the always-busy domestic routes (SFO-JFK, ATL-MCO) MUST return fares if Google is
serving us. Healthy counts on them = PASS; uniform zeros = BLOCKED. A WATCHDOG force-exits after
a hard deadline so the step always completes and its log is readable (gh won't stream an
in-progress step), and so one hung unit can't burn the whole job. Three terminal shapes:
  * units complete WITH fares      -> PASS (Google serves the Azure IP)
  * units complete with 0 fares    -> BLOCKED (Google walls the Azure IP)
  * 0 units complete (all hang)    -> driver still broken (e.g. websockets pin didn't apply)
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
from datetime import date, timedelta

# Print the runtime versions FIRST thing, flushed, so they survive even a total hang.
try:  # pragma: no cover - diagnostic only
    import nodriver as _nd
    import websockets as _ws

    print(f"PROBE env: websockets {_ws.__version__} | nodriver {_nd.__version__}", flush=True)
except Exception as _exc:  # noqa: BLE001
    print(f"PROBE env: version import failed: {_exc!r}", flush=True)

from scrapers.base import ScraperBlockedError
from scrapers.google_flights import GoogleFlightsScraper

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)

TODAY = date.today()
DEADLINE_S = 360  # hard ceiling; watchdog force-exits past this so the step always finishes
results: list[tuple[str, bool, int, str]] = []


def _d(days: int) -> date:
    return TODAY + timedelta(days=days)


# (origin, dest, date, cabin, must_have_fares) — must_have_fares = the busy-domestic wall detector.
UNITS = [
    ("SFO", "JFK", _d(10), "economy", True),
    ("ATL", "MCO", _d(9), "economy", True),
    ("JFK", "LHR", _d(21), "business", False),
    ("SEA", "LHR", _d(28), "premium_economy", False),
]


def _summary_and_verdict() -> int:
    detectors = [r for r in results if r[1]]
    detectors_ok = [r for r in detectors if r[2] > 0]
    blocked = [r for r in results if r[3] == "BLOCKED"]
    any_fares = sum(1 for r in results if r[2] > 0)
    total_fares = sum(r[2] for r in results if r[2] > 0)
    print("\n===== PROBE SUMMARY =====", flush=True)
    print(
        f"units completed: {len(results)}/{len(UNITS)} | "
        f"busy-domestic detectors serving: {len(detectors_ok)}/{len(detectors)} | "
        f"units with fares: {any_fares} | total fares: {total_fares} | hard-blocked: {len(blocked)}",
        flush=True,
    )
    if not results:
        print("PROBE VERDICT: DRIVER-BROKEN — no unit completed (browser/driver never returned).", flush=True)
        return 4
    if detectors and len(detectors_ok) == len(detectors) and not blocked:
        print("PROBE VERDICT: PASS — Google Flights serves this Azure IP. Cash->GA is viable.", flush=True)
        return 0
    if not detectors_ok:
        print("PROBE VERDICT: BLOCKED — busy routes return nothing from Azure. Cash stays on Fly.", flush=True)
        return 2
    print("PROBE VERDICT: PARTIAL — mixed signal, inspect per-unit output above.", flush=True)
    return 3


def _watchdog() -> None:
    time.sleep(DEADLINE_S)
    print(f"\nPROBE WATCHDOG: {DEADLINE_S}s deadline hit — forcing exit with partial results.", flush=True)
    _summary_and_verdict()
    os._exit(7)


def main() -> int:
    threading.Thread(target=_watchdog, daemon=True).start()
    scraper = GoogleFlightsScraper()
    try:
        for i, (o, dst, dt, cabin, must) in enumerate(UNITS, 1):
            tag = f"{o}->{dst} {dt.isoformat()} {cabin}"
            print(f"PROBE unit {i}/{len(UNITS)} START: {tag}", flush=True)
            u0 = time.monotonic()
            try:
                fares = scraper.scrape_fares(o, dst, dt, cabin=cabin)
                n = len(fares)
                status = "OK" if n > 0 else ("EMPTY" if not must else "EMPTY!")
                results.append((tag, must, n, status))
                print(f"PROBE unit {i}/{len(UNITS)} DONE: {tag} -> {n} fares [{status}] ({time.monotonic()-u0:.0f}s)", flush=True)
            except ScraperBlockedError as exc:
                results.append((tag, must, -1, "BLOCKED"))
                print(f"PROBE unit {i}/{len(UNITS)} DONE: {tag} -> BLOCKED ({exc})", flush=True)
            except Exception as exc:  # noqa: BLE001 — probe must finish + report
                results.append((tag, must, -2, "ERROR"))
                print(f"PROBE unit {i}/{len(UNITS)} DONE: {tag} -> ERROR ({exc!r})", flush=True)
    finally:
        for closer in ("close", "stop", "shutdown"):
            fn = getattr(scraper, closer, None)
            if callable(fn):
                try:
                    fn()
                except Exception:  # noqa: BLE001
                    pass
                break
    rc = _summary_and_verdict()
    sys.stdout.flush()
    os._exit(rc)  # hard exit: don't let a lingering nodriver loop hang the step


if __name__ == "__main__":
    main()
