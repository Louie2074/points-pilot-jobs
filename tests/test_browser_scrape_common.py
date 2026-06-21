"""Queue-mode (`build_queue_plan` + adaptive marking in `run_scrape`) for the shared cron runner.

The legacy on-demand path (`run_scrape(..., route_jobs=None)`) is exercised by the per-airline
`_build_plan`/`_parse_dates_csv` tests; these focus on the new queue-aware path.

After the MotherDuck→Supabase cutover, `run_scrape` upserts flights and closes connections through
the `pp_db.autocommit` facade and `build_queue_plan`→`QueueManager` reads `pp.routes_queue` from
Postgres, so these drive the real `pp` container. Seeding goes through the facade (the path the code
under test uses) and a couple of raw UPDATEs force routes due. Skips if `DATABASE_URL` is unset.
"""

import logging
import os
from datetime import date

import pytest

if not os.environ.get("DATABASE_URL"):
    pytest.skip(
        "DATABASE_URL unset — run_scrape queue-mode test needs a live pp schema",
        allow_module_level=True,
    )

from sqlalchemy import text  # noqa: E402

import browser_scrape_common as common  # noqa: E402
from config.settings import PriorityTier  # noqa: E402
from pp_db import autocommit as db  # noqa: E402
from pp_db.engine import get_engine  # noqa: E402


@pytest.fixture(autouse=True)
def clean_routes():
    """Empty routes_queue around each test so the seeded due-set is deterministic. ``run_scrape``'s
    own ``close_connection()`` (the facade's) is safe — it just drops the thread-local conn."""
    with get_engine().begin() as c:
        c.execute(text("TRUNCATE pp.routes_queue RESTART IDENTITY CASCADE"))
    yield
    with get_engine().begin() as c:
        c.execute(text("TRUNCATE pp.routes_queue RESTART IDENTITY CASCADE"))


def _seed_due(n, airline="delta"):
    for i in range(n):
        db.upsert_route(f"O{i:02d}", f"D{i:02d}", PriorityTier.MED, airline=airline)
    with get_engine().begin() as c:
        c.execute(text("UPDATE pp.routes_queue SET next_scrape_at_utc = now() - INTERVAL '1 hour'"))


def test_build_queue_plan_strides_disjoint_and_caps():
    _seed_due(12)
    today = date(2026, 6, 18)
    jobs0, dates = common.build_queue_plan(
        "delta", shard_index=0, shards=3, max_legs=2, scrape_days=3, today=today
    )
    jobs1, _ = common.build_queue_plan(
        "delta", shard_index=1, shards=3, max_legs=2, scrape_days=3, today=today
    )
    assert len(jobs0) == 2 and len(jobs1) == 2  # per-shard cap
    s0 = {(j.origin, j.dest) for j in jobs0}
    s1 = {(j.origin, j.dest) for j in jobs1}
    assert s0.isdisjoint(s1)  # disjoint strides
    assert len(dates) == 3


def test_run_scrape_queue_mode_marks_adaptively():
    _seed_due(1)
    today = date(2026, 6, 18)
    route_jobs, dates = common.build_queue_plan(
        "delta", shard_index=0, shards=1, max_legs=5, scrape_days=1, today=today
    )

    class _Scraper:
        source = "delta"

        def scrape(self, o, d, travel):
            return []  # zero rows: still a successful (non-blocked) scrape -> route marked

        def close(self):
            pass

    common.run_scrape(
        _Scraper(),
        [],
        dates,
        source="delta",
        service="point-pilot-delta",
        airline="delta",
        heartbeat_url="",
        logger=logging.getLogger("t"),
        route_jobs=route_jobs,
    )
    with get_engine().connect() as c:
        row = c.execute(
            text(
                "SELECT interval_h FROM pp.routes_queue "
                "WHERE airline='delta' AND interval_h IS NOT NULL"
            )
        ).fetchone()
    assert row is not None  # the scraped route was marked adaptively


def test_run_scrape_queue_mode_blocked_route_stays_due():
    """The critical safety invariant: a WAF-blocked route is NEVER marked (stays due)."""
    from scrapers.base import ScraperBlockedError

    _seed_due(1)
    today = date(2026, 6, 18)
    route_jobs, dates = common.build_queue_plan(
        "delta", shard_index=0, shards=1, max_legs=5, scrape_days=1, today=today
    )

    class _Blocking:
        source = "delta"

        def scrape(self, o, d, travel):
            raise ScraperBlockedError("WAF")

        def close(self):
            pass

    common.run_scrape(
        _Blocking(),
        [],
        dates,
        source="delta",
        service="point-pilot-delta",
        airline="delta",
        heartbeat_url="",
        logger=logging.getLogger("t"),
        route_jobs=route_jobs,
    )
    # No interval_h was written, and last_scraped is still NULL -> route remains due.
    with get_engine().connect() as c:
        marked = c.execute(
            text(
                "SELECT count(*) FROM pp.routes_queue "
                "WHERE airline='delta' AND interval_h IS NOT NULL"
            )
        ).scalar()
    assert marked == 0
