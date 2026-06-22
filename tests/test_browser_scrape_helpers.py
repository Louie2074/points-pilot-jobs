"""Pure (no-DB) unit tests for browser_scrape_common helpers — _tier_for_job + dense_sparse_dates.

These are hermetic (no live pp schema needed), so they live in their own module rather than
test_browser_scrape_common.py, which module-level-skips when DATABASE_URL is unset.
"""

import browser_scrape_common as common
from browser_scrape_common import _PairJob


class _StubRouteJob:
    def __init__(self, tier):
        self.origin, self.dest, self.tier = "SEA", "JFK", tier


def test_tier_for_job_uses_routejob_tier():
    assert common._tier_for_job(_StubRouteJob("HIGH"), "MED") == "HIGH"
    assert common._tier_for_job(_StubRouteJob("LOW"), "MED") == "LOW"


def test_tier_for_job_defaults_for_ondemand_pairjob():
    # On-demand _PairJobs have no .tier → fall back to the default.
    assert common._tier_for_job(_PairJob("SEA", "JFK"), "MED") == "MED"
