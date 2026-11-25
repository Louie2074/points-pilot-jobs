"""Best-effort operational events and logs over OTLP/HTTP to Grafana Cloud."""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from urllib.parse import unquote

import httpx
from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import ExportLogsServiceRequest

logger = logging.getLogger(__name__)

_threads: set[threading.Thread] = set()
_threads_lock = threading.Lock()

_BENIGN_PATTERNS = (
    "Task was destroyed but it is pending",  # nodriver Connection.aclose teardown
    "blake2",  # hashlib blake2b/blake2s import noise on some Python builds
)
_LEVEL_ABBR = {"WARNING": "WARN"}
_SEVERITY_NUMBERS = {10: 5, 20: 9, 30: 13, 40: 17, 50: 21}


def _log_target() -> tuple[str, dict[str, str]] | None:
    protocol = (
        os.getenv("OTEL_EXPORTER_OTLP_LOGS_PROTOCOL")
        or os.getenv("OTEL_EXPORTER_OTLP_PROTOCOL")
        or "http/protobuf"
    )
    if protocol != "http/protobuf":
        return None

    endpoint = os.getenv("OTEL_EXPORTER_OTLP_LOGS_ENDPOINT")
    base = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
    raw_headers = (
        os.getenv("OTEL_EXPORTER_OTLP_LOGS_HEADERS")
        or os.getenv("OTEL_EXPORTER_OTLP_HEADERS")
        or ""
    )
    if not (endpoint or base) or not raw_headers.strip():
        return None

    headers = {"Content-Type": "application/x-protobuf"}
    for pair in raw_headers.split(","):
        name, separator, value = pair.partition("=")
        if separator and name.strip() and value.strip():
            if name.strip().lower() != "content-type":
                headers[name.strip()] = unquote(value.strip())
    if len(headers) == 1:
        return None

    try:
        url = httpx.URL(endpoint or base)
    except httpx.InvalidURL:
        return None
    if url.scheme not in ("http", "https") or not url.host:
        return None
    if not endpoint:
        url = url.copy_with(path=f"{url.path.rstrip('/')}/v1/logs")
    return str(url), headers


def _post(url: str, headers: dict[str, str], body: bytes) -> None:
    def send() -> None:
        try:
            httpx.post(url, headers=headers, content=body, timeout=3).raise_for_status()
        except Exception:
            # The export should never affect request processing or expose credentials.
            logger.debug("OTLP log export failed")
        finally:
            with _threads_lock:
                _threads.discard(threading.current_thread())

    thread = threading.Thread(target=send, daemon=True)
    with _threads_lock:
        _threads.add(thread)
        try:
            thread.start()
        except RuntimeError:
            _threads.discard(thread)
            logger.debug("Could not start OTLP log export")


def _ship_records(records: list[tuple[str, str, int]], target: tuple[str, dict[str, str]]) -> None:
    request = ExportLogsServiceRequest()
    scope_by_service = {}
    configured_service = os.getenv("OTEL_SERVICE_NAME")
    timestamp = time.time_ns()
    for service, body, level in records:
        service = configured_service or service
        if service not in scope_by_service:
            resource = request.resource_logs.add()
            attribute = resource.resource.attributes.add(key="service.name")
            attribute.value.string_value = service
            scope = resource.scope_logs.add()
            scope.scope.name = "point-pilot"
            scope_by_service[service] = scope
        severity = logging.getLevelName(level)
        record = scope_by_service[service].log_records.add(
            time_unix_nano=timestamp,
            severity_text=_LEVEL_ABBR.get(severity, severity),
            severity_number=_SEVERITY_NUMBERS.get(level, 0),
        )
        record.body.string_value = body
    url, headers = target
    _post(url, headers, request.SerializeToString())


def ship_metric(fields: dict) -> None:
    """Ship one structured operational event as a JSON log body."""
    ship_metrics([fields])


def ship_metrics(events: list[dict]) -> None:
    """Ship a batch of structured events in one OTLP logs request."""
    target = _log_target()
    if not target or not events:
        return
    try:
        records = [
            (str(event.get("service") or "point-pilot-jobs"), json.dumps(event), logging.INFO)
            for event in events
        ]
        _ship_records(records, target)
    except Exception:
        logger.debug("Could not encode OTLP log batch")


def ship_log(
    message: str, *, service: str = "point-pilot-jobs", level: int = logging.INFO
) -> None:
    """Ship one plain-text log with its service and severity."""
    target = _log_target()
    if not target:
        return
    try:
        _ship_records([(service, message, level)], target)
    except Exception:
        logger.debug("Could not encode OTLP log record")


def flush(timeout: float = 5.0) -> None:
    """Give in-flight exports a bounded chance to finish before process exit."""
    deadline = time.monotonic() + timeout
    with _threads_lock:
        threads = tuple(_threads)
    for thread in threads:
        thread.join(max(0.0, deadline - time.monotonic()))


def flush_then_hard_exit(code: int = 0, *, delay_s: float = 0.0) -> None:
    """Flush telemetry before exiting browser entrypoints with pending teardown tasks."""
    if delay_s > 0:
        time.sleep(delay_s)
    flush()
    os._exit(code)


def _format_record(service: str, record: logging.LogRecord) -> str:
    level = _LEVEL_ABBR.get(record.levelname, record.levelname)
    line = f"{level} {service} [{record.name}] {record.getMessage()}"
    if record.exc_info:
        line += "\n" + logging.Formatter().formatException(record.exc_info)
    return line


class _BenignNoiseFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return not any(pattern in record.getMessage() for pattern in _BENIGN_PATTERNS)


class _OtlpLogHandler(logging.Handler):
    def __init__(self, service: str, level: int) -> None:
        super().__init__(level)
        self._service = service

    def emit(self, record: logging.LogRecord) -> None:
        if record.name == __name__:
            return
        try:
            ship_log(
                _format_record(self._service, record), service=self._service, level=record.levelno
            )
        except Exception:
            # A malformed library log record must not crash the application.
            pass


def install_log_shipping(service: str) -> None:
    """Forward WARNING+ logs to Grafana, with a configurable forwarding threshold."""
    if not _log_target():
        return
    level = getattr(logging, os.getenv("POINT_PILOT_LOG_LEVEL", "WARNING").upper(), logging.WARNING)
    root = logging.getLogger()
    if any(isinstance(handler, _OtlpLogHandler) for handler in root.handlers):
        return
    handler = _OtlpLogHandler(service, level)
    handler.addFilter(_BenignNoiseFilter())
    root.addHandler(handler)
    ship_log(f"INFO {service} [obs] started — OTLP log shipping enabled", service=service)


def ship_cash_run(
    *,
    routes: int,
    fares: int,
    routes_zero: int,
    dates_failed: int,
    blocked: bool,
    duration_s: float | None = None,
    freshness: dict | None = None,
    coverage: dict | None = None,
    selection: dict | None = None,
    cpp_coverage: dict | None = None,
) -> None:
    """Emit a `cash_run` event for the Google Flights cash scraper (no-op without a token).

    Mirrors the award scraper's `scrape_run` shape (event + service + counts + duration +
    data-freshness snapshot) so both scrapers chart the same way in Grafana. `coverage`
    carries the cash-coverage-lag fields (award_routes / cash_covered_routes / coverage_pct).
    `cpp_coverage` carries exact_cpp_coverage_pct / od_cpp_coverage_pct / cpp_total_rows."""
    payload = {
        "event": "cash_run",
        "service": "point-pilot-gflights",
        "routes": routes,
        "fares": fares,
        "routes_zero": routes_zero,
        "dates_failed": dates_failed,
        "blocked": blocked,
    }
    if duration_s is not None:
        payload["duration_s"] = duration_s
    if freshness:
        payload.update(freshness)
    if coverage:
        payload.update(coverage)
    if selection:
        payload.update(selection)
    if cpp_coverage:
        payload.update(cpp_coverage)
    ship_metric(payload)
