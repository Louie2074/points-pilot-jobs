import logging
from datetime import date

import httpx
import pytest
import yaml

import browser_scrape_common as common

_WF = ".github/workflows/jetblue-scrape.yml"


def test_jetblue_scrape_imports_and_configures():
    import jetblue_scrape
    # POI-20 lever #3: bumped 30→36 for the expanded Mint business route set.
    assert jetblue_scrape.MAX_LEGS_PER_SHARD == 36
    from scrapers.jetblue import JetBlueScraper
    assert JetBlueScraper.airline_code == "B6"


@pytest.mark.parametrize("status_code", [403, 406])
def test_jetblue_canary_reports_first_waf_response_as_blocked(monkeypatch, status_code):
    """The one-request monthly canary must not need a multi-request block streak."""
    from scrapers.jetblue import JetBlueScraper

    metrics: list[dict] = []
    monkeypatch.setattr("pipeline.obs.ship_metric", lambda payload: metrics.append(payload))
    monkeypatch.setattr(common, "freshness", lambda *args, **kwargs: {})
    monkeypatch.setattr("pp_db.autocommit.close_connection", lambda: None)

    scraper = JetBlueScraper()
    response = httpx.Response(
        status_code,
        request=httpx.Request("POST", "https://www.jetblue.com/api/search"),
    )
    monkeypatch.setattr(scraper._client, "request", lambda *args, **kwargs: response)
    monkeypatch.setattr(scraper, "_cooldown", lambda: None)

    outcome = common.run_scrape(
        scraper,
        [("JFK", "LAX")],
        [date(2026, 7, 26)],
        source="jetblue",
        service="point-pilot-jetblue",
        airline="B6",

        logger=logging.getLogger("test-jetblue"),
    )

    assert outcome.status == "blocked"
    assert outcome.exit_code == 1
    assert metrics[0]["status"] == "blocked"
    assert metrics[0]["blocked"] is True


def test_jetblue_workflow_runs_monthly_probe_while_blocked():
    """JetBlue is currently 100% blocked on GitHub Actions HTTP 406, so keep a monthly low-rate
    health probe instead of three 5-shard coverage pushes."""
    with open(_WF) as f:
        wf = yaml.safe_load(f)
    # PyYAML parses the bare `on:` key as the boolean True.
    schedule = wf[True]["schedule"]
    crons = [s["cron"] for s in schedule]
    assert crons == ["37 20 1 * *"]
    minute, hour, _dom, _month, dow = crons[0].split()
    assert minute == "37"
    assert hour == "20"
    assert dow == "*"
    hour = int(hour)
    assert not (8 <= hour <= 11), "cron must avoid the 08–11 UTC award block"


def test_jetblue_workflow_shard_matrix_is_consistent():
    """JetBlue uses a one-shard, one-route, one-date monthly probe while HTTP 406 is blocked."""
    with open(_WF) as f:
        wf = yaml.safe_load(f)
    job = wf["jobs"]["scrape"]
    shards = job["strategy"]["matrix"]["shard"]
    env = job["steps"][-1]["env"]
    n = int(env["JETBLUE_SHARDS"])
    assert shards == list(range(n)), f"matrix {shards} must be range(JETBLUE_SHARDS={n})"
    assert n == 1, "JetBlue runs one-shard monthly probe while HTTP 406 blocked"
    assert (
        env["JETBLUE_SCRAPE_DAYS"] == "1"
    ), "JetBlue uses one-date probes while HTTP 406 is blocked"
    assert env["JETBLUE_MAX_LEGS_PER_SHARD"] == "1"
    assert env["JETBLUE_SHARD_INDEX"] == "${{ matrix.shard }}"
