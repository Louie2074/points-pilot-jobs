"""``db.queries``-compatible facade for the SYNC script consumers (scraper, jobs).

The DuckDB ``db/queries.py`` functions acquired a thread-local connection internally and ran
each statement in DuckDB's default autocommit mode, so callers invoked them with NO connection
arg (``db.upsert_flights(records)``). The ``pp_db.queries`` ports instead take an explicit
``Connection`` first arg (Core idiom — testable, transaction-controllable).

This module bridges the two: it re-exports every ``pp_db.queries`` function with the ORIGINAL
conn-free signature, injecting a thread-local SQLAlchemy connection put in **AUTOCOMMIT**
isolation (each statement commits on its own — same semantics the DuckDB callers relied on).
So a sync consumer migrates by changing only its import:

    # before:  from db import queries as db
    # after:   from pp_db import autocommit as db
    db.upsert_flights(records)          # unchanged call sites

The async API service does NOT use this — it uses ``api/pp.py`` (run/run_write over to_thread)
so reads and writes get explicit, pool-friendly connection scoping. Thread-local fits the sync
consumers (incl. each ``asyncio.to_thread`` worker, which gets its own connection).
"""

from __future__ import annotations

import functools
import inspect
import threading

from pp_db import queries as _q
from pp_db.engine import get_engine

_local = threading.local()


def _autocommit_engine():
    """The shared sync engine, set to AUTOCOMMIT isolation (each statement commits on its own —
    the DuckDB default the sync callers relied on). ``execution_options`` on an Engine returns a
    lightweight shared-pool variant, so this reuses the same connection pool as ``get_engine()``."""
    return get_engine().execution_options(isolation_level="AUTOCOMMIT")


def _conn():
    """A thread-local, AUTOCOMMIT connection (lazily opened, reopened if closed)."""
    c = getattr(_local, "conn", None)
    if c is None or c.closed:
        c = _autocommit_engine().connect()
        _local.conn = c
    return c


def close_connection() -> None:
    """Drop in for the DuckDB ``db.connection.close_connection`` — close this thread's conn."""
    c = getattr(_local, "conn", None)
    if c is not None and not c.closed:
        c.close()
    _local.conn = None


def migrate() -> None:
    """No-op: the pp schema is managed by Alembic (pp_db/migrations), not at app startup.
    Kept so the sync consumers' `migrate()` call sites need no change."""
    return None


def _takes_conn(fn) -> bool:
    try:
        params = list(inspect.signature(fn).parameters.values())
    except (ValueError, TypeError):
        return False
    return bool(params) and params[0].name == "conn"


def _bind(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        return fn(_conn(), *args, **kwargs)
    # drop the leading conn from the public signature so help()/introspection match the original
    try:
        sig = inspect.signature(fn)
        wrapper.__signature__ = sig.replace(parameters=list(sig.parameters.values())[1:])
    except (ValueError, TypeError):
        pass
    return wrapper


# Re-export every conn-first query function from pp_db.queries, conn-injected.
__all__ = ["close_connection", "migrate"]
for _name in dir(_q):
    if _name.startswith("_"):
        continue
    _obj = getattr(_q, _name)
    if inspect.isfunction(_obj) and _takes_conn(_obj):
        globals()[_name] = _bind(_obj)
        __all__.append(_name)
