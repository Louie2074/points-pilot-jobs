"""Hermetic tests for the sharded one-shot cash entrypoint ``cash_browser_scrape``.

No browser, no DB. We stub the GoogleFlightsScraper, the pp_db cash helpers, the matcher, and
``ship_cash_run``, then drive ``main()`` and assert the loop scrapes every route, upserts, ships a
metric with the right counts, respects the time budget, and reads the shard env.
"""

from __future__ import annotations

import importlib
from datetime import date

import pytest


@pytest.fixture()
def cbs(monkeypatch):
    """Import the entrypoint fresh and neutralise log shipping + heartbeat for every test."""
    mod = importlib.import_module("cash_browser_scrape")
    importlib.reload(mod)
    monkeypatch.setattr(mod, "install_log_shipping", lambda *a, **k: None)
    return mod


class _FakeScraper:
    """Records scrape_fares calls; returns a configurable number of fares per call."""

    def __init__(self, fares_per_call=1, raise_on=None, raise_blocked_on=None):
        self.calls = []
        self.closed = False
        self._fares_per_call = fares_per_call
        self._raise_on = raise_on  # index → raise generic Exception
        self._raise_blocked_on = raise_blocked_on  # index → raise ScraperBlockedError

    def scrape_fares(self, origin, dest, travel_date, cabin="economy"):
        i = len(self.calls)
        self.calls.append((origin, dest, travel_date, cabin))
        if self._raise_blocked_on is not None and i == self._raise_blocked_on:
            from scrapers.base import ScraperBlockedError

            raise ScraperBlockedError("blocked")
        if self._raise_on is not None and i == self._raise_on:
            raise RuntimeError("scrape boom")
        return ["fare"] * self._fares_per_call

    def close(self):
        self.closed = True


def _wire(mod, monkeypatch, scraper, routes):
    """Patch the module's externals to drive main() offline. Returns a dict of captured state."""
    state: dict = {"upserted_fares": [], "coverage": [], "metric": None}

    monkeypatch.setattr(mod, "GoogleFlightsScraper", lambda: scraper)
    monkeypatch.setattr(mod, "get_top_cash_routes", lambda *a, **k: routes, raising=True)
    # capture the shard kwargs passed to get_top_cash_routes
    real_routes = routes

    def _routes(*a, **k):
        state["routes_kwargs"] = k
        state["routes_args"] = a
        return real_routes

    monkeypatch.setattr(mod, "get_top_cash_routes", _routes)
    monkeypatch.setattr(mod, "get_flights_for_match", lambda *a, **k: [])
    monkeypatch.setattr(mod, "match_cash_fares", lambda fares, award, **k: list(fares))

    def _upsert_fares(recs):
        state["upserted_fares"].append(list(recs))
        return len(recs)

    monkeypatch.setattr(mod, "upsert_cash_fares", _upsert_fares)
    monkeypatch.setattr(
        mod,
        "upsert_cash_coverage",
        lambda o, d, t, **k: state["coverage"].append((o, d, t, k)),
    )

    def _ship(**k):
        state["metric"] = k

    monkeypatch.setattr(mod, "ship_cash_run", _ship)
    return state


_ROUTES = [
    ("SFO", "JFK", date(2026, 7, 1), "economy"),
    ("SFO", "JFK", date(2026, 7, 2), "business"),
    ("SEA", "BOS", date(2026, 7, 1), "economy"),
]


def test_loop_scrapes_every_route_and_upserts(cbs, monkeypatch):
    scraper = _FakeScraper(fares_per_call=2)
    state = _wire(cbs, monkeypatch, scraper, _ROUTES)

    cbs.main()

    # Every route was scraped, in order.
    assert scraper.calls == _ROUTES
    # Every route yielded fares → one upsert per route.
    assert len(state["upserted_fares"]) == 3
    # Coverage recorded for every route (with fare_count).
    assert len(state["coverage"]) == 3
    # Scraper closed.
    assert scraper.closed is True


def test_ships_metric_with_counts(cbs, monkeypatch):
    scraper = _FakeScraper(fares_per_call=2)
    state = _wire(cbs, monkeypatch, scraper, _ROUTES)

    cbs.main()

    m = state["metric"]
    assert m is not None
    assert m["routes"] == 3
    assert m["fares"] == 6  # 3 routes × 2 fares
    assert m["routes_zero"] == 0
    assert m["dates_failed"] == 0
    assert m["blocked"] is False
    assert "duration_s" in m


def test_zero_fare_route_counts_zero_not_upserted(cbs, monkeypatch):
    scraper = _FakeScraper(fares_per_call=0)
    state = _wire(cbs, monkeypatch, scraper, _ROUTES)

    cbs.main()

    assert state["upserted_fares"] == []  # nothing upserted
    assert len(state["coverage"]) == 3  # but coverage still recorded for each
    assert state["metric"]["routes_zero"] == 3
    assert state["metric"]["fares"] == 0


def test_generic_scrape_error_is_counted_and_loop_continues(cbs, monkeypatch):
    scraper = _FakeScraper(fares_per_call=1, raise_on=1)  # 2nd route errors
    state = _wire(cbs, monkeypatch, scraper, _ROUTES)

    cbs.main()

    # All 3 routes attempted (error did not abort the loop).
    assert len(scraper.calls) == 3
    assert state["metric"]["dates_failed"] == 1
    # 2 successful routes upserted.
    assert len(state["upserted_fares"]) == 2


def test_blocked_error_aborts_loop_and_marks_blocked(cbs, monkeypatch):
    scraper = _FakeScraper(fares_per_call=1, raise_blocked_on=1)  # blocked on 2nd
    state = _wire(cbs, monkeypatch, scraper, _ROUTES)

    cbs.main()

    # Stopped at the blocked route — 3rd route never scraped.
    assert len(scraper.calls) == 2
    assert state["metric"]["blocked"] is True
    # 1st route (before the block) persisted.
    assert len(state["upserted_fares"]) == 1
    # Scraper still closed in finally.
    assert scraper.closed is True


def test_reads_shard_env_and_threads_into_route_query(cbs, monkeypatch):
    monkeypatch.setenv("CASH_SHARDS", "4")
    monkeypatch.setenv("CASH_SHARD_INDEX", "2")
    scraper = _FakeScraper(fares_per_call=1)
    state = _wire(cbs, monkeypatch, scraper, _ROUTES)

    cbs.main()

    kw = state["routes_kwargs"]
    assert kw["shard_count"] == 4
    assert kw["shard_index"] == 2


def test_time_budget_stops_loop_cleanly(cbs, monkeypatch):
    # Budget of 0s → the loop must stop before scraping anything (or after the guard fires).
    monkeypatch.setenv("CASH_RUN_BUDGET_S", "0")
    scraper = _FakeScraper(fares_per_call=1)
    state = _wire(cbs, monkeypatch, scraper, _ROUTES)

    cbs.main()

    # With a 0s budget no route is scraped, but the metric still ships (clean stop, not a crash).
    assert scraper.calls == []
    assert state["metric"] is not None
    assert scraper.closed is True


def test_heartbeat_pinged_when_url_set(cbs, monkeypatch):
    pinged = {"url": None}
    monkeypatch.setattr(cbs, "GFLIGHTS_HEARTBEAT_URL", "https://hb.example/x")
    monkeypatch.setattr(cbs, "_ping_heartbeat", lambda: pinged.__setitem__("hit", True))
    scraper = _FakeScraper(fares_per_call=1)
    _wire(cbs, monkeypatch, scraper, _ROUTES)

    cbs.main()

    assert pinged.get("hit") is True
