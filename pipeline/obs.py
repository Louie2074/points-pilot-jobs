"""Best-effort metric + log shipping to Better Stack via direct HTTPS POST.

Two channels, two sources:
  - METRICS: structured JSON events (ship_metric / ship_metrics) → the metrics source
    (BETTERSTACK_SOURCE_TOKEN). Unchanged wire format; feeds dashboards.
  - LOGS: raw single-line strings (ship_log) → a separate logs source
    (BETTERSTACK_LOGS_TOKEN). Each log is {dt, message} only — no structured columns —
    so the Better Stack logs view reads as plain lines.

Self-contained copy of the scraper/api `obs.py`, adapted for short-lived cron jobs.
The scraper is a long-running process, so it fires each POST on a daemon thread and
forgets it. A cron job exits in seconds and would kill those threads mid-request — so
POST threads are tracked here and `flush()` drains them before the process exits. Call
`flush()` once, last thing, in a finally.

No-op unless the relevant token is set. Never raises and never blocks the caller.

Env:
  BETTERSTACK_SOURCE_TOKEN   — metrics source ingest token (enables ship_metric[s])
  BETTERSTACK_INGEST_URL     — metrics ingest host (defaults to the classic Logs endpoint)
  BETTERSTACK_LOGS_TOKEN     — logs source ingest token (enables ship_log + log shipping)
  BETTERSTACK_LOGS_INGEST_URL— logs ingest host (defaults to the classic Logs endpoint;
                               set to the source-specific host for region sources)
  BETTERSTACK_LOG_LEVEL      — min level forwarded by install_log_shipping (default WARNING)
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.request
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

_DEFAULT_URL = "https://in.logs.betterstack.com"
_threads: list[threading.Thread] = []

_BENIGN_PATTERNS = (
    "Task was destroyed but it is pending",  # nodriver Connection.aclose teardown
    "blake2",  # hashlib blake2b/blake2s import noise on some Python builds
)

_LEVEL_ABBR = {"WARNING": "WARN"}


def _post(url: str, token: str, body: bytes) -> None:
    """POST one body to a Better Stack source on a TRACKED daemon thread so flush()
    can drain it before a short-lived cron exits."""

    def _run() -> None:
        try:
            req = urllib.request.Request(
                url,
                data=body,
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
                method="POST",
            )
            urllib.request.urlopen(req, timeout=5).close()
        except Exception as exc:  # noqa: BLE001
            logger.debug("better stack POST failed: %s", exc)

    t = threading.Thread(target=_run, daemon=True)
    _threads.append(t)
    t.start()


def ship_metric(fields: dict) -> None:
    """Fire one structured event at the Better Stack METRICS source. Best-effort."""
    ship_metrics([fields])


def ship_metrics(events: list[dict]) -> None:
    """Fire a batch of structured events at the Better Stack METRICS source in one POST.

    Better Stack's ingest accepts a JSON array, so N per-route events cost one request.
    A single event is sent as a bare object. No-op without BETTERSTACK_SOURCE_TOKEN. Each
    POST runs on a tracked daemon thread that `flush()` can drain."""
    token = os.getenv("BETTERSTACK_SOURCE_TOKEN")
    if not token or not events:
        return
    url = os.getenv("BETTERSTACK_INGEST_URL") or _DEFAULT_URL
    now = datetime.now(timezone.utc).isoformat()
    stamped = [{"dt": now, **e} for e in events]
    body = json.dumps(stamped[0] if len(stamped) == 1 else stamped).encode()
    _post(url, token, body)


def ship_log(message: str) -> None:
    """Ship one raw-string log line to the Better Stack LOGS source as {dt, message}.
    Best-effort; no-op without BETTERSTACK_LOGS_TOKEN."""
    token = os.getenv("BETTERSTACK_LOGS_TOKEN")
    if not token:
        return
    url = os.getenv("BETTERSTACK_LOGS_INGEST_URL", _DEFAULT_URL)
    body = json.dumps({"dt": datetime.now(timezone.utc).isoformat(), "message": message}).encode()
    _post(url, token, body)


def flush(timeout: float = 5.0) -> None:
    """Wait (up to `timeout` total) for in-flight POSTs to finish, so a short-lived
    process doesn't exit and kill a daemon POST thread mid-request."""
    deadline = time.monotonic() + timeout
    for t in list(_threads):
        t.join(max(0.0, deadline - time.monotonic()))


def _format_record(service: str, record: logging.LogRecord, identity: dict | None = None) -> str:
    """One raw log line: '<LEVEL> <service> [<logger>] <message>' (+ ' (k=v …)' identity,
    + folded traceback)."""
    level = _LEVEL_ABBR.get(record.levelname, record.levelname)
    line = f"{level} {service} [{record.name}] {record.getMessage()}"
    if identity:
        ident = " ".join(f"{k}={v}" for k, v in identity.items() if v is not None)
        if ident:
            line += f" ({ident})"
    if record.exc_info:
        line += "\n" + logging.Formatter().formatException(record.exc_info)
    return line


class _BenignNoiseFilter(logging.Filter):
    """Drop known library-internal teardown noise (nodriver pending-task, hashlib blake2)
    so it never reaches Better Stack. App-level warnings/errors pass through."""

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        return not any(p in msg for p in _BENIGN_PATTERNS)


class _BetterStackLogHandler(logging.Handler):
    """Forwards log records (with tracebacks) to the Better Stack LOGS source as raw strings."""

    def __init__(self, service: str, level: int) -> None:
        super().__init__(level)
        self._service = service

    def emit(self, record: logging.LogRecord) -> None:
        if record.name == __name__:  # loop guard — never ship our own POST-failure logs
            return
        try:
            ship_log(_format_record(self._service, record))
        except Exception:  # logging must never crash the app
            pass


def install_log_shipping(service: str) -> None:
    """Attach a root-logger handler that POSTs logs to the Better Stack LOGS source as raw
    strings, plus a benign-noise filter. No-op unless a Better Stack token is set. Threshold
    via BETTERSTACK_LOG_LEVEL (default WARNING; set INFO to forward everything)."""
    if not (os.getenv("BETTERSTACK_LOGS_TOKEN") or os.getenv("BETTERSTACK_SOURCE_TOKEN")):
        return
    level = getattr(logging, os.getenv("BETTERSTACK_LOG_LEVEL", "WARNING").upper(), logging.WARNING)
    root = logging.getLogger()
    if any(isinstance(h, _BetterStackLogHandler) for h in root.handlers):
        return
    handler = _BetterStackLogHandler(service, level)
    # The benign noise (nodriver teardown, blake2) is emitted by library loggers like `asyncio`
    # and PROPAGATES up to this handler. A filter on the root *logger* is only consulted for
    # records logged directly on root — never for propagated ones — so it must live on the
    # HANDLER to actually drop the noise before it ships.
    handler.addFilter(_BenignNoiseFilter())
    root.addHandler(handler)
    ship_log(f"INFO {service} [obs] started — direct log shipping enabled")


def ship_cash_run(
    *,
    routes: int,
    fares: int,
    routes_zero: int,
    dates_failed: int,
    blocked: bool,
    duration_s: float | None = None,
    freshness: dict | None = None,
) -> None:
    """Emit a `cash_run` event for the Google Flights cash scraper (no-op without a token).

    Mirrors the award scraper's `scrape_run` shape (event + service + counts + duration +
    data-freshness snapshot) so both scrapers chart the same way in Better Stack."""
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
    ship_metric(payload)
