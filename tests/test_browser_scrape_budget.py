"""Hermetic tests for the wall-clock time budget in ``run_scrape`` (no DB).

A long scheduled cron used to run until its whole shard slice drained, which on AS/B6
overran the GitHub-Actions 60-minute job cap and got hard-cancelled mid-run — losing the
``scrape_run`` event and leaving in-flight routes unmarked. ``run_scrape`` now
stops cleanly between routes once ``time_budget_s`` is reached, ships its metric, and exits;
the routes it didn't reach simply stay due for the next run.

These drive the on-demand path (``route_jobs=None``) so no ``pp`` schema is needed; the DB/obs
boundaries are stubbed so the budget *logic* is what's under test.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from types import SimpleNamespace

import browser_scrape_common as common
from config.settings import SCRAPER_BLOCK_COOLDOWN_MIN


class _FakeScraper:
    source = "jetblue"

    def __init__(self):
        self.calls = 0

    def scrape(self, origin, dest, travel):
        self.calls += 1
        return []  # zero rows: a successful (non-blocked) scrape

    def close(self):
        pass


def _stub_io(monkeypatch, freshness_snapshot=None):
    """Stub the DB/obs side effects so only the budget loop logic runs. Returns the captured
    ``ship_metric`` payloads."""
    metrics: list[dict] = []
    monkeypatch.setattr("pipeline.obs.ship_metric", lambda payload: metrics.append(payload))
    snapshot = dict(freshness_snapshot or {})
    monkeypatch.setattr(common, "freshness", lambda *a, **k: dict(snapshot))
    monkeypatch.setattr("pp_db.autocommit.close_connection", lambda: None)
    return metrics


def test_run_scrape_stops_before_scraping_when_budget_exhausted(monkeypatch):
    metrics = _stub_io(monkeypatch)
    scraper = _FakeScraper()

    outcome = common.run_scrape(
        scraper,
        [("SEA", "JFK"), ("LAX", "BOS")],
        [date(2026, 7, 1)],
        source="jetblue",
        service="point-pilot-jetblue",
        airline="B6",

        logger=logging.getLogger("t"),
        time_budget_s=0,  # already over budget -> stop before the first route
    )

    assert scraper.calls == 0  # no route scraped
    assert outcome.records == 0
    assert outcome.due_routes == 2
    assert outcome.status == "partial"
    assert outcome.exit_code == 0
    assert metrics, "scrape_run metric must still ship on a budget stop"
    assert metrics[0]["routes_scraped"] == 0
    assert metrics[0]["stopped_early"] is True
    assert metrics[0]["status"] == "partial"
    assert metrics[0]["data_health_confirmed"] is False


def test_run_scrape_completes_all_routes_within_generous_budget(monkeypatch):
    metrics = _stub_io(monkeypatch)
    scraper = _FakeScraper()

    outcome = common.run_scrape(
        scraper,
        [("SEA", "JFK"), ("LAX", "BOS")],
        [date(2026, 7, 1)],
        source="jetblue",
        service="point-pilot-jetblue",
        airline="B6",

        logger=logging.getLogger("t"),
        time_budget_s=3600,  # plenty of headroom -> no early stop
    )

    assert scraper.calls == 2  # both routes scraped
    assert metrics[0]["routes_scraped"] == 2
    assert metrics[0]["stopped_early"] is False
    assert metrics[0]["status"] == "healthy"
    assert outcome.status == "healthy"
    assert outcome.exit_code == 0
    assert metrics[0]["data_health_confirmed"] is True


def test_run_scrape_metric_counts_zero_record_routes(monkeypatch):
    metrics = _stub_io(monkeypatch)
    scraper = _FakeScraper()

    common.run_scrape(
        scraper,
        [("SEA", "JFK"), ("LAX", "BOS")],
        [date(2026, 7, 1)],
        source="jetblue",
        service="point-pilot-jetblue",
        airline="B6",

        logger=logging.getLogger("t"),
        time_budget_s=3600,
    )

    assert scraper.calls == 2
    assert metrics[0]["routes_scraped"] == 2
    assert metrics[0]["routes_zero"] == 2


def test_run_scrape_queue_mode_blocked_route_sets_backoff_and_metric_fields(monkeypatch):
    import pipeline.queue_manager as queue_manager
    from scrapers.base import ScraperBlockedError

    metrics = _stub_io(monkeypatch)
    blocked_calls: list[tuple[str, str, str, int]] = []

    class _BlockingScraper:
        source = "jetblue"

        def scrape(self, origin, dest, travel):
            raise ScraperBlockedError("WAF")

        def close(self):
            pass

    class _FakeQM:
        def __init__(self, scraper=None):
            pass

        def mark_blocked(self, job, now, cooldown_min):
            blocked_calls.append((job.origin, job.dest, job.airline, cooldown_min))

    monkeypatch.setattr(queue_manager, "QueueManager", _FakeQM)
    route_jobs = [
        SimpleNamespace(
            origin="SEA",
            dest="JFK",
            airline="jetblue",
            tier="MED",
            queue_due_count=4,
        )
    ]

    outcome = common.run_scrape(
        _BlockingScraper(),
        [],
        [date(2026, 7, 1)],
        source="jetblue",
        service="point-pilot-jetblue",
        airline="jetblue",

        logger=logging.getLogger("t"),
        route_jobs=route_jobs,
        time_budget_s=3600,
    )

    assert blocked_calls == [("SEA", "JFK", "jetblue", SCRAPER_BLOCK_COOLDOWN_MIN)]
    assert metrics
    assert metrics[0]["blocked"] is True
    assert metrics[0]["blocked_route"] == "SEA-JFK"
    assert metrics[0]["blocked_airline"] == "jetblue"
    assert metrics[0]["blocked_backoff_min"] == SCRAPER_BLOCK_COOLDOWN_MIN
    assert metrics[0]["queue_selected_routes"] == 1
    assert metrics[0]["queue_left_due_estimate"] == 3
    assert metrics[0]["queue_fill_ratio"] == 0.25
    assert metrics[0]["routes_zero"] == 0
    assert metrics[0]["status"] == "blocked"
    assert outcome.status == "blocked"
    assert outcome.exit_code == 1
    assert metrics[0]["data_health_confirmed"] is False


def test_run_scrape_failed_when_every_response_errors(monkeypatch):
    metrics = _stub_io(monkeypatch)

    class _FailingScraper:
        def scrape(self, origin, dest, travel):
            raise ValueError("malformed upstream response")

        def close(self):
            pass

    outcome = common.run_scrape(
        _FailingScraper(),
        [("SEA", "JFK")],
        [date(2026, 7, 1)],
        source="jetblue",
        service="point-pilot-jetblue",
        airline="B6",

        logger=logging.getLogger("t"),
        time_budget_s=3600,
    )

    assert outcome.status == "failed"
    assert outcome.exit_code == 1
    assert outcome.errors == 1
    assert metrics[0]["status"] == "failed"
    assert metrics[0]["data_health_confirmed"] is False


def test_run_scrape_partial_when_progress_precedes_error(monkeypatch):
    metrics = _stub_io(monkeypatch)

    class _PartiallyFailingScraper:
        def scrape(self, origin, dest, travel):
            if origin == "LAX":
                raise ValueError("malformed upstream response")
            return []

        def close(self):
            pass

    outcome = common.run_scrape(
        _PartiallyFailingScraper(),
        [("SEA", "JFK"), ("LAX", "BOS")],
        [date(2026, 7, 1)],
        source="jetblue",
        service="point-pilot-jetblue",
        airline="B6",

        logger=logging.getLogger("t"),
        time_budget_s=3600,
    )

    assert outcome.status == "partial"
    assert outcome.exit_code == 0
    assert outcome.errors == 1
    assert metrics[0]["status"] == "partial"
    assert metrics[0]["data_health_confirmed"] is False


def test_run_scrape_valid_empty_route_is_healthy_progress(monkeypatch):
    metrics = _stub_io(monkeypatch)

    outcome = common.run_scrape(
        _FakeScraper(),
        [("SEA", "JFK")],
        [date(2026, 7, 1)],
        source="jetblue",
        service="point-pilot-jetblue",
        airline="B6",

        logger=logging.getLogger("t"),
        time_budget_s=3600,
    )

    assert outcome.status == "healthy"
    assert outcome.routes_zero == 1
    assert metrics[0]["status"] == "healthy"
    assert metrics[0]["data_health_confirmed"] is True


def test_run_scrape_valid_populated_route_confirms_data_health(monkeypatch):
    metrics = _stub_io(monkeypatch)
    monkeypatch.setattr("pipeline.normalizer.filter_valid", lambda records: records)
    monkeypatch.setattr("pipeline.normalizer.stamp_expiry", lambda records, _tier: records)
    monkeypatch.setattr("pp_db.autocommit.upsert_flights", lambda records: len(records))

    class _PopulatedScraper(_FakeScraper):
        def scrape(self, origin, dest, travel):
            self.calls += 1
            return [object()]

    outcome = common.run_scrape(
        _PopulatedScraper(),
        [("SEA", "JFK")],
        [date(2026, 7, 1)],
        source="jetblue",
        service="point-pilot-jetblue",
        airline="B6",

        logger=logging.getLogger("t"),
        time_budget_s=3600,
    )

    assert outcome.status == "healthy"
    assert outcome.records == 1
    assert metrics[0]["records"] == 1
    assert metrics[0]["data_health_confirmed"] is True


def test_run_scrape_empty_on_demand_does_not_confirm_data_health(monkeypatch):
    metrics = _stub_io(monkeypatch)

    outcome = common.run_scrape(
        _FakeScraper(),
        [],
        [date(2026, 7, 1)],
        source="jetblue",
        service="point-pilot-jetblue",
        airline="B6",

        logger=logging.getLogger("t"),
        time_budget_s=0,
    )

    assert outcome.status == "healthy"
    assert outcome.routes_scraped == 0
    assert outcome.records == 0
    assert metrics[0]["status"] == "healthy"
    assert metrics[0]["data_health_confirmed"] is False


def test_run_scrape_empty_cron_queue_with_no_source_data_does_not_confirm_data_health(monkeypatch):
    import pipeline.queue_manager as queue_manager

    metrics = _stub_io(monkeypatch)

    class _FakeQM:
        def __init__(self, scraper=None):
            pass

    monkeypatch.setattr(queue_manager, "QueueManager", _FakeQM)

    outcome = common.run_scrape(
        _FakeScraper(),
        [],
        [date(2026, 7, 1)],
        source="jetblue",
        service="point-pilot-jetblue",
        airline="B6",

        logger=logging.getLogger("t"),
        route_jobs=[],
        time_budget_s=0,
    )

    assert outcome.status == "healthy"
    assert outcome.due_routes == 0
    assert metrics[0]["status"] == "healthy"
    assert metrics[0]["data_health_confirmed"] is False


def test_run_scrape_empty_cron_queue_with_unexpired_source_data_confirms_data_health(monkeypatch):
    import pipeline.queue_manager as queue_manager

    metrics = _stub_io(monkeypatch)
    freshness_calls = 0

    def fresh_snapshot(*_args, **_kwargs):
        nonlocal freshness_calls
        freshness_calls += 1
        return {
            "jetblue_rows": 4,
            "jetblue_newest_age_h": 2.0,
            "jetblue_unexpired_rows": 1,
        }

    monkeypatch.setattr(common, "freshness", fresh_snapshot)

    class _FakeQM:
        def __init__(self, scraper=None):
            pass

    monkeypatch.setattr(queue_manager, "QueueManager", _FakeQM)

    outcome = common.run_scrape(
        _FakeScraper(),
        [],
        [date(2026, 7, 1)],
        source="jetblue",
        service="point-pilot-jetblue",
        airline="B6",

        logger=logging.getLogger("t"),
        route_jobs=[],
        time_budget_s=0,
    )

    assert outcome.status == "healthy"
    assert metrics[0]["jetblue_rows"] == 4
    assert metrics[0]["jetblue_newest_age_h"] == 2.0
    assert metrics[0]["jetblue_unexpired_rows"] == 1
    assert freshness_calls == 1
    assert metrics[0]["data_health_confirmed"] is True


def test_run_scrape_empty_cron_queue_with_recent_but_expired_data_does_not_confirm_data_health(
    monkeypatch,
):
    import pipeline.queue_manager as queue_manager

    metrics = _stub_io(
        monkeypatch,
        {
            "jetblue_rows": 4,
            "jetblue_newest_age_h": 0.0,
            "jetblue_unexpired_rows": 0,
        },
    )

    class _FakeQM:
        def __init__(self, scraper=None):
            pass

    monkeypatch.setattr(queue_manager, "QueueManager", _FakeQM)

    outcome = common.run_scrape(
        _FakeScraper(),
        [],
        [date(2026, 7, 1)],
        source="jetblue",
        service="point-pilot-jetblue",
        airline="B6",

        logger=logging.getLogger("t"),
        route_jobs=[],
        time_budget_s=0,
    )

    assert outcome.status == "healthy"
    assert metrics[0]["jetblue_unexpired_rows"] == 0
    assert metrics[0]["data_health_confirmed"] is False


def test_freshness_snapshot_counts_only_rows_expiring_after_database_now(monkeypatch):
    executed = []
    newest = datetime(2026, 7, 23, 12, tzinfo=timezone.utc)

    class _Result:
        def fetchone(self):
            return 7, newest, 2

    class _Connection:
        def execute(self, statement, params):
            executed.append((str(statement), params))
            return _Result()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    class _Engine:
        def connect(self):
            return _Connection()

    monkeypatch.setattr("pp_db.engine.get_engine", lambda: _Engine())

    snapshot = common.freshness("jetblue", logging.getLogger("t"))

    assert snapshot["jetblue_rows"] == 7
    assert snapshot["jetblue_unexpired_rows"] == 2
    assert "expires_at_utc > now()" in " ".join(executed[0][0].split())
    assert executed[0][1] == {"source": "jetblue"}
