"""Hermetic tests for the cash-route shard filter in ``pp_db.queries_cash.get_top_cash_routes``.

The shard filter keeps a whole route (all its dates/cabins) on ONE shard via
``abs(hashtextextended(origin || '-' || dest, 0)) % :shard_count == :shard_index``. These tests
don't need a live DB — they inspect the compiled SQL text + the bind params that
``get_top_cash_routes`` would send, plus a pure-Python sanity check of the partition math.
"""

from __future__ import annotations

from datetime import date

from pp_db import queries_cash


class _FakeResult:
    def fetchall(self):
        return []


class _CaptureConn:
    """Stand-in Connection: records the (sql, params) of the last execute, returns no rows."""

    def __init__(self) -> None:
        self.sql = None
        self.params = None

    def execute(self, sql, params=None):
        self.sql = sql
        self.params = params
        return _FakeResult()


def _capture(**kwargs) -> _CaptureConn:
    conn = _CaptureConn()
    queries_cash.get_top_cash_routes(
        conn, limit=10, days_ahead=30, ttl_hours=72, cabins=("economy",), **kwargs
    )
    return conn


def _sql_text(conn: _CaptureConn) -> str:
    # text(...) clause — str() renders the bound SQL string (with the named params).
    return str(conn.sql)


def test_default_args_have_no_active_shard_restriction():
    """Defaults (shard_count=1) must behave exactly as today: the clause is present but
    short-circuits via `:shard_count = 1`, and the params carry the inert 0/1 pair."""
    conn = _capture()
    sql = _sql_text(conn)
    # The clause is in the SQL but guarded so shard_count=1 selects everything.
    assert "hashtextextended" in sql
    assert conn.params["shard_count"] == 1
    assert conn.params["shard_index"] == 0


def test_sharded_args_thread_into_params_and_sql():
    conn = _capture(shard_index=2, shard_count=4)
    sql = _sql_text(conn)
    assert "hashtextextended" in sql
    # The mod expression and the bind names are present.
    assert "% :shard_count = :shard_index" in sql
    assert conn.params["shard_count"] == 4
    assert conn.params["shard_index"] == 2


def test_shard_clause_sits_before_order_by_and_after_negative_memory():
    conn = _capture(shard_index=1, shard_count=3)
    sql = _sql_text(conn)
    shard_pos = sql.index("hashtextextended")
    order_pos = sql.index("ORDER BY")
    # Negative-memory blocks (cash_coverage NOT EXISTS) must precede the shard filter.
    neg_mem_pos = sql.rindex("cc.next_probe_utc")
    assert neg_mem_pos < shard_pos < order_pos


# --- pure-Python partition-math sanity (mirrors the SQL's deterministic % mod) -----------------
#
# We can't reproduce Postgres' hashtextextended() in Python, but the *partitioning contract* the
# SQL relies on is: for a fixed shard_count, every distinct hash value maps to exactly one shard in
# [0, shard_count), the shards are disjoint, and their union is the whole space. Model the hash as
# an opaque int and assert the contract for a representative spread of hashes.


def _assign(hash_value: int, shard_count: int) -> int:
    return abs(hash_value) % shard_count


def test_partition_is_total_and_disjoint():
    shard_count = 4
    # A spread of pretend hash values (incl. negatives — abs() is applied in the SQL).
    hashes = list(range(-50, 51)) + [10**9, -(10**9), 7919, -104729]
    buckets: dict[int, list[int]] = {i: [] for i in range(shard_count)}
    for h in hashes:
        buckets[_assign(h, shard_count)].append(h)
    # Total: every hash lands somewhere.
    assert sum(len(v) for v in buckets.values()) == len(hashes)
    # Disjoint: no hash in two buckets.
    seen: set[int] = set()
    for v in buckets.values():
        assert not (set(v) & seen)
        seen.update(v)
    assert seen == set(hashes)


def test_same_route_lands_on_same_shard_regardless_of_date_or_cabin():
    # The SQL hashes only (origin || '-' || dest), so a route's hash is fixed across its
    # dates/cabins → its shard is stable. Model: same hash → same shard for any (date, cabin).
    route_hash = 1234567
    shard_count = 4
    s1 = _assign(route_hash, shard_count)
    # different date / cabin tuples for the SAME route hash → identical shard
    for _date, _cabin in [
        (date(2026, 7, 1), "economy"),
        (date(2026, 8, 15), "business"),
        (date(2026, 9, 30), "premium_economy"),
    ]:
        assert _assign(route_hash, shard_count) == s1


def test_route_distribution_spreads_across_shards():
    # A sanity check that the modulo doesn't collapse everything to one shard for distinct hashes.
    shard_count = 4
    used = {_assign(h, shard_count) for h in range(1000)}
    assert used == {0, 1, 2, 3}
