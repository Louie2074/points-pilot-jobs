"""v10 brings the shared routes_queue scoring columns + airline_budget into jobs' schema."""

import duckdb
import pytest


@pytest.fixture
def conn():
    import db.connection as db_conn

    c = duckdb.connect(":memory:")
    c.execute("SET TimeZone='UTC'")
    db_conn._local.conn = c
    yield c
    db_conn._local.conn = None
    c.close()


def test_v10_scoring_columns_and_budget(conn):
    from db import schema

    schema.migrate()
    cols = {r[1] for r in conn.execute("PRAGMA table_info('routes_queue')").fetchall()}
    assert {
        "decayed_demand",
        "last_search_at_utc",
        "change_rate",
        "last_cheapest",
        "interval_h",
    } <= cols
    bcols = {r[1] for r in conn.execute("PRAGMA table_info('airline_budget')").fetchall()}
    assert {"airline", "tokens", "capacity", "refill_per_hour", "last_refill_utc"} <= bcols
    assert conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == 10
