"""Engine / session factories for ``pp_db`` — sync (psycopg3) and async (asyncpg).

Both pin every session to UTC (mirroring the old DuckDB ``SET TimeZone='UTC'``) so the naive
``*_utc`` timestamps round-trip as UTC, and both are configured for the Supabase **Supavisor
transaction pooler** (port 6543), which forbids server-side prepared statements.

``DATABASE_URL`` (env) is a plain Postgres URL, e.g.
``postgresql://user:pw@aws-0-...pooler.supabase.com:6543/postgres``. The driver suffix
(``+psycopg`` / ``+asyncpg``) is added here so callers configure one URL.
"""

from __future__ import annotations

import os
from functools import lru_cache

from sqlalchemy import URL, make_url
from sqlalchemy import create_engine as _create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session, sessionmaker


def _base_url(url: str | None = None) -> URL:
    raw = url or os.environ.get("DATABASE_URL")
    if not raw:
        raise RuntimeError("DATABASE_URL is not set — cannot connect to Postgres.")
    return make_url(raw)


def _sync_url(url: str | None = None) -> URL:
    return _base_url(url).set(drivername="postgresql+psycopg")


def _async_url(url: str | None = None) -> URL:
    return _base_url(url).set(drivername="postgresql+asyncpg")


@lru_cache(maxsize=8)
def get_engine(url: str | None = None) -> Engine:
    """A sync Engine (psycopg3). UTC-pinned per connection; prepared statements off for the
    transaction pooler. Pooled with pre-ping so a recycled-by-Supavisor connection is detected."""
    return _create_engine(
        _sync_url(url),
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=5,
        connect_args={
            # psycopg3: disable server-side prepared statements (transaction pooler can't keep them).
            "prepare_threshold": None,
            # Pin UTC at connection STARTUP (libpq -c) rather than a post-connect ``SET TIME ZONE``
            # statement — the latter opens a transaction, which blocks switching a connection to
            # AUTOCOMMIT (the sync ``pp_db.autocommit`` facade needs that). Mirrors the asyncpg
            # ``server_settings={'timezone': 'UTC'}`` path.
            "options": "-c timezone=UTC",
        },
    )


@lru_cache(maxsize=8)
def get_async_engine(url: str | None = None) -> AsyncEngine:
    """An async Engine (asyncpg). UTC via server_settings; statement cache disabled for the
    transaction pooler."""
    return create_async_engine(
        _async_url(url),
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=5,
        connect_args={
            "statement_cache_size": 0,  # asyncpg: required under transaction pooling
            "server_settings": {"timezone": "UTC"},
        },
    )


@lru_cache(maxsize=8)
def get_sessionmaker(url: str | None = None) -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(url), expire_on_commit=False)


@lru_cache(maxsize=8)
def get_async_sessionmaker(url: str | None = None) -> async_sessionmaker:
    return async_sessionmaker(bind=get_async_engine(url), expire_on_commit=False)
