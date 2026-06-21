"""Phase 3: Southwest cron runs across 3 fresh-IP shards, partitioning the due set disjointly.

The shard-matrix assertion is pure YAML (no DB — always runs). The plan-partition test exercises
``build_queue_plan``→``QueueManager``, which after the MotherDuck→Supabase cutover reads
``pp.routes_queue`` from Postgres, so it seeds the real ``pp`` container via the
``pp_db.autocommit`` facade and skips when ``DATABASE_URL`` is unset.
"""

import os
from datetime import date

import pytest
import yaml

import browser_scrape_common as common
from config.settings import PriorityTier

_WF = ".github/workflows/southwest-browser-scrape.yml"

_NEEDS_PG = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL unset — queue-plan test needs a live pp schema",
)


def test_southwest_workflow_has_3_shard_matrix():
    with open(_WF) as f:
        wf = yaml.safe_load(f)
    job = wf["jobs"]["scrape"]
    assert job["strategy"]["matrix"]["shard"] == [0, 1, 2], "expected a 3-shard matrix"
    # the scrape step's env must wire SHARDS=3 + SHARD_INDEX from the matrix
    env = job["steps"][-1]["env"]
    assert env["SOUTHWEST_SHARDS"] == "3"
    assert env["SOUTHWEST_SHARD_INDEX"] == "${{ matrix.shard }}"


@_NEEDS_PG
def test_southwest_3shard_plan_partitions_due_set():
    from sqlalchemy import text

    from pp_db import autocommit as db
    from pp_db.engine import get_engine

    with get_engine().begin() as c:
        c.execute(text("TRUNCATE pp.routes_queue RESTART IDENTITY CASCADE"))
    for i in range(9):
        db.upsert_route(f"O{i:02d}", f"D{i:02d}", PriorityTier.MED, airline="southwest")
    with get_engine().begin() as c:
        c.execute(text("UPDATE pp.routes_queue SET next_scrape_at_utc = now() - INTERVAL '1 hour'"))
    today = date(2026, 6, 18)
    sets = []
    for idx in range(3):
        plan, _dates = common.build_queue_plan(
            "southwest", shard_index=idx, shards=3, max_legs=3, scrape_days=3, today=today
        )
        sets.append({(j.origin, j.dest) for j in plan})
    # disjoint across shards
    assert sets[0].isdisjoint(sets[1])
    assert sets[0].isdisjoint(sets[2])
    assert sets[1].isdisjoint(sets[2])
    # together they cover all 9 due routes (3 shards × cap 3)
    assert len(sets[0] | sets[1] | sets[2]) == 9
