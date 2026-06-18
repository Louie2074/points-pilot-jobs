from datetime import date

from etihad_browser_scrape import _build_plan, _parse_dates_csv


def test_parse_dates_csv_valid_and_invalid():
    assert _parse_dates_csv("2026-06-20, ,nonsense,2026-06-22") == [
        date(2026, 6, 20),
        date(2026, 6, 22),
    ]


def test_build_plan_single_route():
    pairs, dates = _build_plan("jfk", "auh", "2026-06-20,2026-06-21", 3, date(2026, 6, 13))
    assert pairs == [("JFK", "AUH")]  # requested direction only
    assert dates == [date(2026, 6, 20), date(2026, 6, 21)]


def test_build_plan_single_route_no_dates_falls_back_to_window():
    pairs, dates = _build_plan("JFK", "AUH", "", 3, date(2026, 6, 13))
    assert pairs == [("JFK", "AUH")]
    assert dates == [date(2026, 6, 13), date(2026, 6, 14), date(2026, 6, 15)]


def test_etihad_cron_uses_queue(monkeypatch):
    import etihad_browser_scrape as ep

    calls = {}

    def fake_build_queue_plan(airline, **kw):
        calls.update(airline=airline, **kw)
        return (["JOB"], ["DATE"])

    monkeypatch.setattr(ep.common, "build_queue_plan", fake_build_queue_plan)
    monkeypatch.setattr(ep.common, "run_scrape", lambda *a, **k: 0)
    monkeypatch.setattr("scrapers.etihad.EtihadScraper", lambda *a, **k: object())

    ep._run_cron(shard_index=0, shards=1)
    assert calls["airline"] == "etihad"
    assert calls["max_legs"] == ep.MAX_LEGS_PER_SHARD
    assert calls["shard_index"] == 0
    assert calls["shards"] == 1
