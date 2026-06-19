"""Hermetic tests for Better Stack obs: ship_log raw strings + structured metrics."""

import json
import logging
import sys

import obs


class _Capture:
    """Replaces obs._post; records (url, token, decoded body string)."""

    def __init__(self):
        self.calls = []

    def __call__(self, url, token, body):
        self.calls.append((url, token, body.decode()))


def _patch_post(monkeypatch):
    cap = _Capture()
    monkeypatch.setattr(obs, "_post", cap)
    return cap


def test_ship_log_noop_without_logs_token(monkeypatch):
    cap = _patch_post(monkeypatch)
    monkeypatch.delenv("BETTERSTACK_LOGS_TOKEN", raising=False)
    obs.ship_log("hello")
    assert cap.calls == []


def test_ship_log_posts_dt_and_message_only(monkeypatch):
    cap = _patch_post(monkeypatch)
    monkeypatch.setenv("BETTERSTACK_LOGS_TOKEN", "logtok")
    monkeypatch.delenv("BETTERSTACK_LOGS_INGEST_URL", raising=False)
    obs.ship_log("WARN point-pilot-jobs [x] boom")
    assert len(cap.calls) == 1
    url, token, body = cap.calls[0]
    assert token == "logtok"
    assert url == "https://in.logs.betterstack.com"
    payload = json.loads(body)
    assert set(payload.keys()) == {"dt", "message"}
    assert payload["message"] == "WARN point-pilot-jobs [x] boom"


def test_ship_metric_still_structured_to_metrics_token(monkeypatch):
    cap = _patch_post(monkeypatch)
    monkeypatch.setenv("BETTERSTACK_SOURCE_TOKEN", "mettok")
    obs.ship_metric({"event": "scrape_run", "records": 5})
    assert len(cap.calls) == 1
    url, token, body = cap.calls[0]
    assert token == "mettok"
    payload = json.loads(body)
    assert payload["event"] == "scrape_run"
    assert payload["records"] == 5
    assert "dt" in payload


def test_ship_log_uses_logs_token_not_metrics(monkeypatch):
    cap = _patch_post(monkeypatch)
    monkeypatch.setenv("BETTERSTACK_SOURCE_TOKEN", "mettok")
    monkeypatch.setenv("BETTERSTACK_LOGS_TOKEN", "logtok")
    obs.ship_log("INFO point-pilot-jobs [x] hi")
    assert cap.calls[0][1] == "logtok"


def test_handler_emits_single_line_string(monkeypatch):
    cap = _patch_post(monkeypatch)
    monkeypatch.setenv("BETTERSTACK_LOGS_TOKEN", "logtok")
    handler = obs._BetterStackLogHandler("point-pilot-jobs", logging.WARNING)
    rec = logging.LogRecord(
        name="delta_browser_scrape",
        level=logging.WARNING,
        pathname=__file__,
        lineno=1,
        msg="[delta] BOS→EWR gap",
        args=(),
        exc_info=None,
    )
    handler.emit(rec)
    assert len(cap.calls) == 1
    payload = json.loads(cap.calls[0][2])
    assert payload["message"] == "WARN point-pilot-jobs [delta_browser_scrape] [delta] BOS→EWR gap"


def test_handler_folds_traceback_into_message(monkeypatch):
    cap = _patch_post(monkeypatch)
    monkeypatch.setenv("BETTERSTACK_LOGS_TOKEN", "logtok")
    handler = obs._BetterStackLogHandler("point-pilot-jobs", logging.ERROR)
    try:
        raise ValueError("nope")
    except ValueError:
        rec = logging.LogRecord(
            name="x",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="failed",
            args=(),
            exc_info=sys.exc_info(),
        )
    handler.emit(rec)
    msg = json.loads(cap.calls[0][2])["message"]
    assert msg.startswith("ERROR point-pilot-jobs [x] failed\n")
    assert "ValueError: nope" in msg


def test_benign_noise_filter_drops_known_lines():
    f = obs._BenignNoiseFilter()
    rec = logging.LogRecord(
        name="asyncio",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg="Task was destroyed but it is pending! ... Connection.aclose()",
        args=(),
        exc_info=None,
    )
    assert f.filter(rec) is False
    blake = logging.LogRecord(
        name="root",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg="code for hash blake2s was not found",
        args=(),
        exc_info=None,
    )
    assert f.filter(blake) is False
    keep = logging.LogRecord(
        name="x",
        level=logging.WARNING,
        pathname=__file__,
        lineno=1,
        msg="real warning",
        args=(),
        exc_info=None,
    )
    assert f.filter(keep) is True


def test_install_log_shipping_filters_propagated_asyncio_noise(monkeypatch):
    """The nodriver teardown noise is emitted by the `asyncio` logger and PROPAGATES to the
    root handler. A filter on the root *logger* is never consulted for propagated records, so
    the filter must live on the handler."""
    cap = _patch_post(monkeypatch)
    monkeypatch.setenv("BETTERSTACK_LOGS_TOKEN", "logtok")
    root = logging.getLogger()
    handlers_before = root.handlers[:]
    filters_before = root.filters[:]
    try:
        obs.install_log_shipping("point-pilot-turkish")
        logging.getLogger("asyncio").error(
            "Task was destroyed but it is pending! task: <Task ... Connection.aclose()>"
        )
        logging.getLogger("scrapers.turkish").warning("real scrape warning")
        shipped = [body for _, _, body in cap.calls]
        assert not any("Task was destroyed" in m for m in shipped), shipped
        assert any("real scrape warning" in m for m in shipped), shipped
    finally:
        root.handlers[:] = handlers_before
        root.filters[:] = filters_before
