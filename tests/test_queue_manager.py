"""jobs QueueManager: scored get_due_batch + adaptive mark_scraped (vendored).

QueueManager now reads/writes the ``pp.routes_queue`` Postgres table via the ``pp_db.autocommit``
facade (the MotherDuck→Supabase cutover), so these drive it against the real ``pp`` container
rather than an in-memory DuckDB. Seeding goes through the facade (the same path QueueManager uses);
the few raw UPDATEs (forcing routes due / bumping demand) run on a pp_db engine connection. Skips
if ``DATABASE_URL`` is unset so the hermetic lane stays green.
"""

import json
import os
from datetime import datetime, timezone

import pytest

if not os.environ.get("DATABASE_URL"):
    pytest.skip(
        "DATABASE_URL unset — QueueManager test needs a live pp schema", allow_module_level=True
    )

from sqlalchemy import text  # noqa: E402

from config.settings import PriorityTier  # noqa: E402
from pipeline.queue_manager import QueueManager  # noqa: E402
from pp_db import autocommit as db  # noqa: E402
from pp_db.engine import get_engine  # noqa: E402


@pytest.fixture(autouse=True)
def clean_routes():
    """Start each test from an empty routes_queue so seeded rows are deterministic."""
    with get_engine().begin() as c:
        c.execute(text("TRUNCATE pp.routes_queue RESTART IDENTITY CASCADE"))
    yield
    with get_engine().begin() as c:
        c.execute(text("TRUNCATE pp.routes_queue RESTART IDENTITY CASCADE"))


@pytest.fixture
def queue():
    return QueueManager(scraper=None)


def _exec(sql: str) -> None:
    with get_engine().begin() as c:
        c.execute(text(sql))


def test_get_due_batch_orders_by_score(queue):
    for o, d in [("AAA", "BBB"), ("CCC", "DDD")]:
        db.upsert_route(o, d, PriorityTier.MED, airline="delta")
    _exec("UPDATE pp.routes_queue SET next_scrape_at_utc = now() - INTERVAL '1 hour'")
    _exec("UPDATE pp.routes_queue SET decayed_demand = 50 WHERE origin='CCC'")
    batch = queue.get_due_batch(limit=10, airline="delta")
    assert batch[0].origin == "CCC"


def test_mark_scraped_persists_adaptive_state(queue):
    db.upsert_route("ATL", "LAX", PriorityTier.MED, airline="delta")
    _exec("UPDATE pp.routes_queue SET next_scrape_at_utc = now() - INTERVAL '1 hour'")
    job = queue.get_due_batch(limit=5, airline="delta")[0]
    now = datetime(2026, 6, 18, 12, tzinfo=timezone.utc)
    changed = queue.mark_scraped(job, {"economy": 9000}, now)
    assert changed is True  # no prior cheapest
    with get_engine().connect() as c:
        row = c.execute(
            text(
                "SELECT interval_h, last_cheapest FROM pp.routes_queue "
                "WHERE origin='ATL' AND dest='LAX'"
            )
        ).fetchone()
    assert row[0] is not None
    assert json.loads(row[1]) == {"economy": 9000}
