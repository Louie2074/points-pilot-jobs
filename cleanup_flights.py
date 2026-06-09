#!/usr/bin/env python3
"""
cleanup_flights — delete stale rows from the MotherDuck `flights` table.

Runs daily on a GitHub Actions cron. Deletes every flight whose departure
`date` is older than yesterday (UTC) — i.e. it keeps yesterday plus everything
forward, and drops everything before yesterday. The connection is pinned to UTC
so `current_date` resolves to the UTC calendar date regardless of where the
runner happens to live.

This mirrors the scraper's old `expire_stale_flights()` cleanup (now removed from
the scraper, which is a pure write pipeline): `expires_at` is a scrape-freshness
TTL, NOT a signal that the flight is gone, so cleanup is anchored to the actual
flight `date`.

Observability: emits a `cleanup_flights_run` completion metric and ships WARNING+
logs to Better Stack when BETTERSTACK_SOURCE_TOKEN is set (see obs.py). All a
no-op without the token.

Requires MOTHERDUCK_TOKEN in the environment — the duckdb package picks it up
automatically when opening an `md:` connection.
"""

from __future__ import annotations

import argparse
import logging
import os
import time
import urllib.request

import duckdb

from obs import flush, install_log_shipping, ship_metric

# Optional Better Stack heartbeat — a missed daily run then raises an alert.
# No-op unless CLEANUP_HEARTBEAT_URL is set (so local/dry runs stay quiet).
CLEANUP_HEARTBEAT_URL = os.getenv("CLEANUP_HEARTBEAT_URL", "")


def _ping_heartbeat() -> None:
    if not CLEANUP_HEARTBEAT_URL:
        return
    try:
        urllib.request.urlopen(CLEANUP_HEARTBEAT_URL, timeout=10).close()
    except Exception as exc:  # noqa: BLE001 — monitoring must never break the run
        logger.warning("heartbeat ping failed: %s", exc)


logger = logging.getLogger("cleanup_flights")

# Strictly older than yesterday (UTC). `current_date - INTERVAL '1 day'` is
# yesterday, so `date < yesterday` keeps yesterday + today + future and deletes
# everything before. Identical to the scraper's old expire_stale_flights() predicate.
STALE_PREDICATE = "date < current_date - INTERVAL '1 day'"


def connect() -> duckdb.DuckDBPyConnection:
    """Open a UTC-pinned MotherDuck connection to the point_pilot database."""
    if not os.environ.get("MOTHERDUCK_TOKEN"):
        raise RuntimeError("MOTHERDUCK_TOKEN is not set — cannot connect to MotherDuck.")
    conn = duckdb.connect("md:point_pilot")
    conn.execute("SET TimeZone='UTC'")
    return conn


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report how many rows would be deleted, without deleting anything.",
    )
    args = parser.parse_args()

    # force=True so our config wins even if an imported library (or a hashlib
    # import-time warning) already attached a root handler — otherwise basicConfig
    # is a silent no-op and INFO logs get swallowed.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        force=True,
    )
    # Ship WARNING+ logs to Better Stack (no-op without BETTERSTACK_SOURCE_TOKEN).
    install_log_shipping("points-pilot-jobs")

    started = time.monotonic()
    deleted = 0
    would_delete = 0
    ok = False
    try:
        conn = connect()
        # Resolve the cutoff once so logs/metrics name the exact UTC boundary applied.
        cutoff = conn.execute("SELECT (current_date - INTERVAL '1 day')::DATE").fetchone()[0]

        if args.dry_run:
            would_delete = conn.execute(
                f"SELECT count(*) FROM flights WHERE {STALE_PREDICATE}"
            ).fetchone()[0]
            logger.info(
                "[dry-run] %d flight row(s) with date < %s (UTC) would be deleted.",
                would_delete,
                cutoff,
            )
        else:
            # DuckDB's DELETE returns a single-row result holding the rows removed.
            deleted = conn.execute(f"DELETE FROM flights WHERE {STALE_PREDICATE}").fetchone()[0]
            logger.info("Deleted %d flight row(s) with date < %s (UTC).", deleted, cutoff)

        ok = True
        return 0
    except Exception:
        # logger.exception ships to Better Stack as an error log (with traceback).
        logger.exception("cleanup_flights failed")
        return 1
    finally:
        metric = {
            "event": "cleanup_flights_run",
            "service": "points-pilot-jobs",
            "job": "cleanup_flights",
            "ok": ok,
            "deleted": deleted,
            "dry_run": args.dry_run,
            "duration_s": round(time.monotonic() - started, 3),
        }
        if args.dry_run:
            metric["would_delete"] = would_delete
        ship_metric(metric)
        flush()  # drain in-flight Better Stack POSTs before the process exits
        # Heartbeat only on a successful real run — a failed/never-run cron then
        # misses its ping and Better Stack alerts. Dry-runs (manual) don't ping.
        if ok and not args.dry_run:
            _ping_heartbeat()


if __name__ == "__main__":
    raise SystemExit(main())
