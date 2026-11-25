"""Hermetic tests for the Grafana Cloud OTLP log transport."""

import json
import logging

import httpx
import pytest
from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import ExportLogsServiceRequest

from pipeline import obs


@pytest.fixture
def exports(monkeypatch):
    requests = []
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_HEADERS", raising=False)
    monkeypatch.delenv("OTEL_SERVICE_NAME", raising=False)

    def capture(url, *, headers, content, timeout):
        requests.append((url, headers, ExportLogsServiceRequest.FromString(content)))
        return httpx.Response(204, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", capture)
    return requests


def _configure(monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "https://grafana.invalid/otlp/")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "Authorization=Basic demo==,X-Team=a%2Cb")


def test_ship_metrics_exports_a_protobuf_batch(monkeypatch, exports):
    _configure(monkeypatch)
    rows = [
        {"event": "scrape_run", "service": "point-pilot-delta", "records": 5},
        {"event": "cash_run", "service": "point-pilot-gflights", "fares": 6},
        {"event": "scrape_route", "service": "point-pilot-delta", "records": 3},
    ]
    obs.ship_metrics(rows)
    obs.flush()

    [(url, headers, request)] = exports
    assert url == "https://grafana.invalid/otlp/v1/logs"
    assert headers == {
        "Content-Type": "application/x-protobuf",
        "Authorization": "Basic demo==",
        "X-Team": "a,b",
    }
    by_service = {
        log.resource.attributes[0].value.string_value: log.scope_logs[0].log_records
        for log in request.resource_logs
    }
    assert list(by_service) == ["point-pilot-delta", "point-pilot-gflights"]
    assert request.resource_logs[0].resource.attributes[0].key == "service.name"
    for service, logs in by_service.items():
        assert all(log.time_unix_nano > 0 for log in logs)
        assert [json.loads(log.body.string_value) for log in logs] == [
            row for row in rows if row["service"] == service
        ]


def test_missing_credentials_disable_export(monkeypatch, exports):
    _configure(monkeypatch)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_HEADERS")
    obs.ship_metric({"event": "scrape_run"})
    obs.ship_log("hello")
    assert exports == []


def test_log_handler_preserves_warning_and_filters_teardown_noise(monkeypatch, exports):
    _configure(monkeypatch)
    monkeypatch.setenv("OTEL_SERVICE_NAME", "point-pilot-overridden")
    root = logging.getLogger()
    before = root.handlers[:]
    try:
        obs.install_log_shipping("point-pilot-delta")
        logging.getLogger("asyncio").error("Task was destroyed but it is pending!")
        logging.getLogger("scrapers.delta").warning("scraper warning")
        obs.flush()
    finally:
        root.handlers[:] = before

    records = []
    for _, _, request in exports:
        [resource] = request.resource_logs
        assert resource.resource.attributes[0].value.string_value == "point-pilot-overridden"
        records.extend(resource.scope_logs[0].log_records)
    assert not any("Task was destroyed" in record.body.string_value for record in records)
    [warning] = [record for record in records if "scraper warning" in record.body.string_value]
    assert warning.severity_number == 13
    assert warning.body.string_value == "WARN point-pilot-delta [scrapers.delta] scraper warning"


def test_export_timeout_does_not_escape_or_leave_pending_threads(monkeypatch, exports):
    _configure(monkeypatch)

    def time_out(*_args, **_kwargs):
        raise httpx.ReadTimeout("synthetic timeout")

    monkeypatch.setattr(httpx, "post", time_out)
    obs.ship_log("hello")
    obs.flush()
    assert not obs._threads
