"""Best-effort metric + log shipping to Better Stack via direct HTTPS POST.

Self-contained copy of the scraper/api `obs.py` (pipeline/obs.py), adapted for
short-lived cron jobs. One structured JSON event per call, POSTed straight to the
source's ingest endpoint. No-op unless BETTERSTACK_SOURCE_TOKEN is set. Never
raises and never blocks the caller.

The scraper is a long-running process, so it fires each POST on a daemon thread
and forgets it. A cron job exits in seconds and would kill those threads
mid-request — so POST threads are tracked here and `flush()` drains them before
the process exits. Call `flush()` once, last thing, in a finally.

Env:
  BETTERSTACK_SOURCE_TOKEN  — the Better Stack source ingest token (required to enable)
  BETTERSTACK_INGEST_URL    — ingest host (defaults to the classic Logs endpoint)
  BETTERSTACK_LOG_LEVEL     — min level forwarded by install_log_shipping (default WARNING)
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


def ship_metric(fields: dict) -> None:
    """Fire one structured event at Better Stack. Best-effort, off the hot path."""
    ship_metrics([fields])


def ship_metrics(events: list[dict]) -> None:
    """Fire a batch of structured events at Better Stack in a single POST.

    Best-effort: no token or empty batch → no-op; failures swallowed. A single
    event is sent as a bare object, a batch as a JSON array (the ingest accepts
    both). Each POST runs on a tracked daemon thread that `flush()` can drain.
    """
    token = os.getenv("BETTERSTACK_SOURCE_TOKEN")
    if not token or not events:
        return
    url = os.getenv("BETTERSTACK_INGEST_URL") or _DEFAULT_URL
    now = datetime.now(timezone.utc).isoformat()
    stamped = [{"dt": now, **e} for e in events]
    body = json.dumps(stamped[0] if len(stamped) == 1 else stamped).encode()

    def _post() -> None:
        try:
            req = urllib.request.Request(
                url,
                data=body,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {token}",
                },
                method="POST",
            )
            urllib.request.urlopen(req, timeout=5).close()
        except Exception as exc:  # noqa: BLE001 — monitoring must never break the job
            logger.debug("ship_metrics failed: %s", exc)

    t = threading.Thread(target=_post, daemon=True)
    _threads.append(t)
    t.start()


def flush(timeout: float = 5.0) -> None:
    """Wait (up to `timeout` total) for in-flight POSTs to finish, so a short-lived
    process doesn't exit and kill a daemon POST thread mid-request."""
    deadline = time.monotonic() + timeout
    for t in list(_threads):
        t.join(max(0.0, deadline - time.monotonic()))


class _BetterStackLogHandler(logging.Handler):
    """Forwards log records (with tracebacks) to Better Stack via ship_metric."""

    def __init__(self, service: str, level: int) -> None:
        super().__init__(level)
        self._service = service

    def emit(self, record: logging.LogRecord) -> None:
        if record.name == __name__:  # loop guard — never ship our own POST-failure logs
            return
        try:
            fields = {
                "event": "log",
                "service": self._service,
                "level": record.levelname.lower(),
                "logger": record.name,
                "message": record.getMessage(),
            }
            if record.exc_info:
                fields["traceback"] = logging.Formatter().formatException(record.exc_info)
            ship_metric(fields)
        except Exception:  # logging must never crash the job
            pass


def install_log_shipping(service: str) -> None:
    """Attach a root-logger handler that POSTs logs to Better Stack so errors and
    tracebacks become searchable. No-op unless BETTERSTACK_SOURCE_TOKEN is set.
    Threshold via BETTERSTACK_LOG_LEVEL (default WARNING; set INFO to forward all)."""
    if not os.getenv("BETTERSTACK_SOURCE_TOKEN"):
        return
    level = getattr(logging, os.getenv("BETTERSTACK_LOG_LEVEL", "WARNING").upper(), logging.WARNING)
    root = logging.getLogger()
    if any(isinstance(h, _BetterStackLogHandler) for h in root.handlers):
        return
    root.addHandler(_BetterStackLogHandler(service, level))
    ship_metric(
        {
            "event": "log",
            "service": service,
            "level": "info",
            "message": f"{service} started — direct log shipping enabled",
        }
    )
