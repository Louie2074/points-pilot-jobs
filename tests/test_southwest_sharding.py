"""Phase 3: Southwest cron runs across 3 fresh-IP shards, partitioning the due set disjointly."""

from datetime import date

import duckdb
import pytest
import yaml

import browser_scrape_common as common
from config.settings import PriorityTier
from db import queries as db

_WF = ".github/workflows/southwest-browser-scrape.yml"


def test_southwest_workflow_has_3_shard_matrix():
    with open(_WF) as f:
        wf = yaml.safe_load(f)
    job = wf["jobs"]["scrape"]
    assert job["strategy"]["matrix"]["shard"] == [0, 1, 2], "expected a 3-shard matrix"
    # the scrape step's env must wire SHARDS=3 + SHARD_INDEX from the matrix
    env = job["steps"][-1]["env"]
    assert env["SOUTHWEST_SHARDS"] == "3"
    assert env["SOUTHWEST_SHARD_INDEX"] == "${{ matrix.shard }}"


@pytest.fixture(autouse=True)
def conn():
    import db.connection as db_conn

    c = duckdb.connect(":memory:")
    c.execute("SET TimeZone='UTC'")
    db_conn._local.conn = c
    from db import schema

    schema.migrate()
    yield c
    db_conn._local.conn = None
    c.close()


def test_southwest_3shard_plan_partitions_due_set():
    for i in range(9):
        db.upsert_route(f"O{i:02d}", f"D{i:02d}", PriorityTier.MED, airline="southwest")
    db.get_connection().execute(
        "UPDATE routes_queue SET next_scrape_at_utc = now() - INTERVAL '1 hour'"
    )
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
