from __future__ import annotations

import logging
from datetime import date

import httpx

import browser_scrape_common as common
from scrapers.base import HttpScraper, RequestStats


class ProbeScraper(HttpScraper):
    airline_code = "PP"
    program_name = "Probe"
    source = "probe"

    def fetch_raw(self, origin: str, dest: str, travel_date: date) -> dict:
        response = self._request("GET", "https://api.example.test/availability")
        assert response is not None
        return response.json()

    def normalize(self, raw: dict, origin: str, dest: str, travel_date: date) -> list:
        return []


def response(status_code: int) -> httpx.Response:
    return httpx.Response(
        status_code,
        json={"ok": True},
        request=httpx.Request("GET", "https://api.example.test/availability"),
    )


def test_http_scraper_counts_each_tenacity_attempt(monkeypatch):
    scraper = ProbeScraper()
    responses = [response(503), response(200)]
    monkeypatch.setattr(scraper._client, "request", lambda *a, **k: responses.pop(0))
    monkeypatch.setattr(scraper, "_pace", lambda: None)
    monkeypatch.setattr(scraper._request.retry, "wait", lambda retry_state: 0)

    assert scraper.fetch_raw("SEA", "BOS", date(2026, 8, 20)) == {"ok": True}

    stats = scraper.request_stats()
    assert stats.attempts == 2
    assert stats.retries == 1
    assert stats.server_errors == 1


def test_http_scraper_counts_transport_attempt_before_retry(monkeypatch):
    scraper = ProbeScraper()
    outcomes = [
        httpx.ReadTimeout(
            "timed out",
            request=httpx.Request("GET", "https://api.example.test/availability"),
        ),
        response(200),
    ]

    def request(*args, **kwargs):
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(scraper._client, "request", request)
    monkeypatch.setattr(scraper, "_pace", lambda: None)
    monkeypatch.setattr(scraper._request.retry, "wait", lambda retry_state: 0)

    assert scraper.fetch_raw("SEA", "BOS", date(2026, 8, 20)) == {"ok": True}

    stats = scraper.request_stats()
    assert stats.attempts == 2
    assert stats.retries == 1
    assert stats.server_errors == 0


def test_http_scraper_excludes_homepage_warmup_from_availability_attempts(monkeypatch):
    scraper = ProbeScraper()
    scraper.prime_url = "https://www.example.test/"
    monkeypatch.setattr(scraper._client, "get", lambda *a, **k: response(200))
    monkeypatch.setattr(scraper._client, "request", lambda *a, **k: response(200))
    monkeypatch.setattr(scraper, "_pace", lambda: None)

    assert scraper.fetch_raw("SEA", "BOS", date(2026, 8, 20)) == {"ok": True}
    assert scraper.request_stats().attempts == 1


def test_request_stats_classifies_availability_responses():
    scraper = ProbeScraper()

    scraper._record_request(403)
    scraper._record_request(406)
    scraper._record_request(429, retry=True)
    scraper._record_request(503, retry=True)
    scraper._record_request(None, retry=True)

    stats = scraper.request_stats()
    assert stats.attempts == 5
    assert stats.retries == 3
    assert stats.blocked == 2
    assert stats.rate_limited == 1
    assert stats.server_errors == 1


def test_run_scrape_reports_upstream_requests_without_database_cleanup(monkeypatch):
    metrics: list[dict] = []
    monkeypatch.setattr("pipeline.obs.ship_metric", lambda payload: metrics.append(payload))
    monkeypatch.setattr(common, "freshness", lambda *a, **k: {})
    monkeypatch.setattr("pp_db.autocommit.close_connection", lambda: None)

    class _Scraper:
        source = "delta"

        def __init__(self):
            self.closed = False

        def scrape(self, origin, dest, travel):
            return []

        def close(self):
            self.closed = True

        def request_stats(self):
            assert self.closed
            return RequestStats(attempts=2)

    outcome = common.run_scrape(
        _Scraper(),
        [("SEA", "BOS")],
        [date(2026, 8, 20)],
        source="delta",
        service="point-pilot-delta",
        airline="DL",

        logger=logging.getLogger("test-upstream-requests"),
    )

    assert outcome.upstream_requests == 2
    assert metrics[0]["upstream_requests"] == 2
