from datetime import date

from delta_browser_scrape import _build_plan, _parse_dates_csv


def test_parse_dates_csv_valid():
    assert _parse_dates_csv("2026-06-20,2026-06-21") == [date(2026, 6, 20), date(2026, 6, 21)]


def test_parse_dates_csv_drops_invalid_and_blanks():
    assert _parse_dates_csv("2026-06-20, ,nonsense,2026-06-22") == [
        date(2026, 6, 20),
        date(2026, 6, 22),
    ]


def test_parse_dates_csv_empty():
    assert _parse_dates_csv("") == []


def test_build_plan_single_route_with_dates():
    pairs, dates = _build_plan("atl", "lax", "2026-06-20,2026-06-21", 5, date(2026, 6, 8))
    # requested direction only (no reverse), exactly the supplied dates
    assert pairs == [("ATL", "LAX")]
    assert dates == [date(2026, 6, 20), date(2026, 6, 21)]


def test_build_plan_single_route_no_dates_falls_back_to_window():
    pairs, dates = _build_plan("ATL", "LAX", "", 3, date(2026, 6, 8))
    assert pairs == [("ATL", "LAX")]
    assert dates == [date(2026, 6, 8), date(2026, 6, 9), date(2026, 6, 10)]


def test_build_plan_single_route_only_shard0_works():
    # On-demand single-route dispatch spawns both matrix shards, but only shard 0 scrapes;
    # other shards no-op so the route isn't scraped twice.
    p0, d0 = _build_plan("ATL", "LAX", "", 5, date(2026, 6, 8), shard_index=0, shards=2)
    p1, d1 = _build_plan("ATL", "LAX", "", 5, date(2026, 6, 8), shard_index=1, shards=2)
    assert p0 == [("ATL", "LAX")] and d0
    assert p1 == [] and d1 == []


def test_delta_cron_uses_queue(monkeypatch):
    import delta_browser_scrape as ep

    calls = {}

    def fake_build_queue_plan(airline, **kw):
        calls.update(airline=airline, **kw)
        return (["JOB"], ["DATE"])

    monkeypatch.setattr(ep.common, "build_queue_plan", fake_build_queue_plan)
    monkeypatch.setattr(ep.common, "run_scrape", lambda *a, **k: 0)
    monkeypatch.setattr("scrapers.delta.DeltaScraper", lambda *a, **k: object())

    ep._run_cron(shard_index=0, shards=3)
    assert calls["airline"] == "delta"
    assert calls["max_legs"] == ep.MAX_LEGS_PER_SHARD
    assert calls["shard_index"] == 0
    assert calls["shards"] == 3
