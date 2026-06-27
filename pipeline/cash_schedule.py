"""Pure scheduling helpers for the Google Flights cash runner (google_flights_main.py).

Kept in a separate, dependency-light module (no browser/`nodriver` imports) so they can be
unit-tested hermetically. Scraper-only — used only by the cash runner, which is NOT vendored
to api/jobs, so these need no cross-repo propagation.
"""

from __future__ import annotations

from collections.abc import Iterable


def sleep_seconds(interval_s: float, elapsed_s: float, min_rest_s: float) -> int:
    """Seconds to sleep so runs start on a fixed period.

    Sleep the remainder of `interval_s` after a run that took `elapsed_s`, floored at
    `min_rest_s` so an over-long run can't make the next run start back-to-back with no rest.
    """
    return int(max(min_rest_s, interval_s - elapsed_s))


# Slow / sparse / low-priority cabins scraped only every Nth run (demoted to free slots for the
# economy+business tail). Premium economy and first are both ~as large as economy but lower yield.
_DEMOTED_CABINS = ("premium_economy", "first")


def cabins_for_run(
    base_cabins: Iterable[str], run_index: int, pe_every_n: int
) -> tuple[str, ...]:
    """The cabins to scrape on run `run_index` (0-based), demoting the slow cabins.

    Premium economy and first are the slowest, lowest-yield cabins, so they are scraped only
    every `pe_every_n`-th run (freeing slots for the productive economy/business tail) rather
    than dropped. `pe_every_n <= 1` keeps them every run. Cabins outside `_DEMOTED_CABINS` are
    always kept; a base list without those cabins is returned unchanged.
    """
    keep_demoted = pe_every_n <= 1 or run_index % pe_every_n == 0
    if keep_demoted:
        return tuple(base_cabins)
    return tuple(c for c in base_cabins if c not in _DEMOTED_CABINS)
