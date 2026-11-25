"""Database-backed queue-mode tests for the shared cron runner.

The legacy on-demand path (`run_scrape(..., route_jobs=None)`) is exercised by the per-airline
`_build_plan`/`_parse_dates_csv` tests. Hermetic shared-runner tests live in their own modules;
these focus on the queue-aware path.

`run_scrape` upserts flights and closes connections through the `pp_db.autocommit` facade, and
`build_queue_plan`→`QueueManager` reads `pp.routes_queue` from Postgres, so these drive the real
`pp` container. Seeding goes through the facade (the path the code under test uses) and a couple of
raw UPDATEs force routes due. Each database test skips if `DATABASE_URL` is unset.
"""

import logging
import os
from datetime import date, datetime, timedelta, timezone

import pytest

requires_database = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL unset — queue-mode test needs a live pp schema",
)

from sqlalchemy import text  # noqa: E402

import browser_scrape_common as common  # noqa: E402
from config.settings import SCRAPER_BLOCK_COOLDOWN_MIN, PriorityTier  # noqa: E402
from pp_db import autocommit as db  # noqa: E402
from pp_db.engine import get_engine  # noqa: E402


@pytest.fixture
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


@requires_database
def test_build_queue_plan_strides_disjoint_and_caps(clean_routes):
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


@requires_database
def test_run_scrape_queue_mode_marks_adaptively(clean_routes):
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


@requires_database
def test_run_scrape_queue_mode_blocked_route_stays_due(clean_routes):
    """A blocked route is not marked scraped, but it is backed off briefly."""
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

        logger=logging.getLogger("t"),
        route_jobs=route_jobs,
    )

    with get_engine().connect() as c:
        row = c.execute(
            text(
                "SELECT interval_h, last_scraped_at_utc, next_scrape_at_utc "
                "FROM pp.routes_queue WHERE airline='delta'"
            )
        ).fetchone()
    assert row[0] is None
    assert row[1] is None
    assert row[2].tzinfo is None

    backoff_until = row[2].replace(tzinfo=timezone.utc)
    expected_min = timedelta(minutes=SCRAPER_BLOCK_COOLDOWN_MIN - 1)
    expected_max = timedelta(minutes=SCRAPER_BLOCK_COOLDOWN_MIN + 1)
    delta = backoff_until - datetime.now(timezone.utc)
    assert expected_min <= delta <= expected_max


@requires_database
def test_run_scrape_queue_mode_metric_includes_block_details_and_queue_pressure(
    monkeypatch, clean_routes
):
    from scrapers.base import ScraperBlockedError

    metrics: list[dict] = []
    monkeypatch.setattr("pipeline.obs.ship_metric", lambda payload: metrics.append(payload))
    monkeypatch.setattr(common, "freshness", lambda *a, **k: {})

    _seed_due(3)
    today = date(2026, 6, 18)
    route_jobs, dates = common.build_queue_plan(
        "delta", shard_index=0, shards=2, max_legs=2, scrape_days=1, today=today
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

        logger=logging.getLogger("t"),
        route_jobs=route_jobs,
    )

    assert metrics
    metric = metrics[0]
    assert metric["queue_selected_routes"] == len(route_jobs)
    assert metric["queue_left_due_estimate"] == 1
    assert metric["queue_fill_ratio"] == 0.67
    assert metric["blocked"] is True
    assert metric["blocked_airline"] == "delta"
    assert metric["blocked_route"] == f"{route_jobs[0].origin}-{route_jobs[0].dest}"
    assert metric["blocked_backoff_min"] == SCRAPER_BLOCK_COOLDOWN_MIN


@requires_database
def test_run_scrape_queue_mode_metric_uses_actual_due_backlog(monkeypatch, clean_routes):
    metrics: list[dict] = []
    monkeypatch.setattr("pipeline.obs.ship_metric", lambda payload: metrics.append(payload))
    monkeypatch.setattr(common, "freshness", lambda *a, **k: {})

    _seed_due(6)
    today = date(2026, 6, 18)
    route_jobs, dates = common.build_queue_plan(
        "delta", shard_index=0, shards=2, max_legs=2, scrape_days=1, today=today
    )

    class _Scraper:
        source = "delta"

        def scrape(self, o, d, travel):
            return []

        def close(self):
            pass

    common.run_scrape(
        _Scraper(),
        [],
        dates,
        source="delta",
        service="point-pilot-delta",
        airline="delta",

        logger=logging.getLogger("t"),
        route_jobs=route_jobs,
    )

    assert metrics
    metric = metrics[0]
    assert metric["queue_selected_routes"] == 2
    assert metric["queue_left_due_estimate"] == 4
    assert metric["queue_fill_ratio"] == 0.33
