"""pp_db — shared Postgres data layer (SQLAlchemy 2.0) for the point_pilot services.

Replaces the vendored DuckDB ``db/`` across api / scraper / jobs. Import models from
``pp_db.models`` and engines/sessions from ``pp_db.engine``. Query functions (porting
``db/queries.py``) land in ``pp_db.queries`` next.
"""

from pp_db import models
from pp_db.engine import (
    get_async_engine,
    get_async_sessionmaker,
    get_engine,
    get_sessionmaker,
)
from pp_db.models import Base

__all__ = [
    "Base",
    "models",
    "get_engine",
    "get_async_engine",
    "get_sessionmaker",
    "get_async_sessionmaker",
]
