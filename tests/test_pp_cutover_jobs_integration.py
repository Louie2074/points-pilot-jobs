"""End-to-end integration check for the MotherDuck → pp_db (Postgres) JOBS wiring.

Unlike the hermetic unit tests, this drives the ACTUAL wired jobs code against a real Postgres
``pp`` schema — the ``browser_scrape_common.freshness`` probe, the ``pp_db.autocommit`` write/read
facade ``run_scrape`` uses, the ported ``transfer_partners.reconcile`` snapshot-replace, and the
``migrate()`` no-op call sites the entrypoints rely on.

Run against the local pp-pg container:
    DATABASE_URL=postgresql://postgres:pp@localhost:5499/pp \
    MOTHERDUCK_TOKEN=dummy PYTHONPATH=<cutover-jobs worktree> \
    .venv/bin/python -m pytest tests/test_pp_cutover_jobs_integration.py -q

Skips itself if DATABASE_URL is unset (so it never runs in the hermetic CI lane).
"""

from __future__ import annotations

import logging
import os
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import text

if not os.environ.get("DATABASE_URL"):
    pytest.skip(
        "DATABASE_URL unset — pp_db jobs integration test needs a live pp schema",
        allow_module_level=True,
    )

import browser_scrape_common as common  # noqa: E402
import transfer_partners as tp  # noqa: E402
from pp_db import autocommit as db  # noqa: E402
from pp_db import queries as pq  # noqa: E402
from pp_db.engine import get_engine  # noqa: E402
from scrapers.base import FlightRecord  # noqa: E402

LOG = logging.getLogger("pp_cutover_jobs")
NOW = datetime.now(timezone.utc)
FAR = datetime(2099, 1, 1, tzinfo=timezone.utc)
D1 = date.today() + timedelta(days=14)


@pytest.fixture(autouse=True)
def clean():
    """Clean the tables this test touches before and after, so it's self-contained and leaves the
    container as it found it (these are the sole-owner / scrape tables)."""
    tables = ("flights", "cash_fares", "transfer_partners", "transfer_bonuses")
    with get_engine().begin() as c:
        for t in tables:
            c.execute(text(f"DELETE FROM pp.{t}"))
    yield
    db.close_connection()
    with get_engine().begin() as c:
        for t in tables:
            c.execute(text(f"DELETE FROM pp.{t}"))


# ---- migrate() no-op ----------------------------------------------------------------------------

def test_migrate_is_callable_noop():
    """The entrypoints call ``migrate()``; the facade now defines it as a no-op returning None."""
    assert db.migrate() is None


# ---- freshness probe (browser_scrape_common) ----------------------------------------------------

def test_freshness_returns_expected_shape():
    """Seed one source='turkish' flight, then the wired freshness() reports its row count + age."""
    with get_engine().begin() as c:
        c.execute(
            text(
                """
                INSERT INTO pp.flights(origin,destination,date,airline,program,source,points_cost,
                  cash_cost,stops,cabin_class,available_seats,raw_flight_number,scraped_at_utc,
                  expires_at_utc,is_saver,next_day_arrival,mixed_cabin)
                VALUES('IST','JFK',:dt,'TK','Miles&Smiles','turkish',45000,80.0,0,'business',4,
                  'TK1', :now, :far, true, false, false)
                """
            ),
            {"dt": D1, "now": NOW, "far": FAR},
        )
    info = common.freshness("turkish", LOG)
    assert info["turkish_rows"] == 1
    assert info["turkish_newest_age_h"] is not None
    assert info["turkish_newest_age_h"] >= 0


# ---- the facade write/read path run_scrape uses -------------------------------------------------

def test_facade_upsert_and_read_flights():
    """``run_scrape`` upserts via the facade ``upsert_flights`` and reads via ``get_flights`` —
    drive both directly: write a FlightRecord, read it back through the same facade."""
    rec = FlightRecord(
        origin="SEA",
        destination="JFK",
        date=D1,
        airline="AS",
        program="Mileage Plan",
        source="alaska",
        points_cost=25000,
        cash_cost=320.0,
        cabin_class="economy",
        stops=0,
        available_seats=4,
        scraped_at_utc=NOW,
        expires_at_utc=FAR,
        raw_flight_number="AS101",
    )
    assert db.upsert_flights([rec]) == 1
    rows = db.get_flights("SEA", "JFK", D1, D1)
    assert len(rows) == 1
    got = rows[0]
    assert got["airline"] == "AS"
    assert got["points_cost"] == 25000
    assert got["raw_flight_number"] == "AS101"


# ---- transfer_partners snapshot-replace (sole owner — the dangerous path) ------------------------

def test_transfer_partners_snapshot_replace():
    """Seed a stale partner row, then the ported reconcile() must fully replace it with the new set
    (snapshot semantics): the prior row is gone, exactly the new rows remain."""
    with get_engine().begin() as c:
        # bank_programs the facade read-back JOINs against (it INNER JOINs bank_programs); upsert so
        # the test is self-contained regardless of what the container already holds.
        c.execute(
            text(
                "INSERT INTO pp.bank_programs(id, name, short_code) VALUES "
                "(1, 'Chase Ultimate Rewards', 'CHASE'), (5, 'Bilt Rewards', 'BILT') "
                "ON CONFLICT (id) DO NOTHING"
            )
        )
        c.execute(
            text(
                "INSERT INTO pp.transfer_partners "
                "(bank_program_id, airline_code, program_name, transfer_ratio) "
                "VALUES (6, 'AS', 'Mileage Plan', 3.0)"  # stale Marriott row to be dropped
            )
        )

    new_partners = [
        {
            "bank_program_id": 1,
            "airline_code": "SQ",
            "program_name": "KrisFlyer",
            "transfer_ratio": 1.0,
            "min_transfer": 1000,
            "transfer_increment": 1000,
        },
        {
            "bank_program_id": 5,
            "airline_code": "AS",
            "program_name": "Mileage Plan",
            "transfer_ratio": 1.0,
            "min_transfer": 1000,
            "transfer_increment": 1000,
        },
    ]

    # Drive the real production call shape: reconcile inside one get_engine().begin() transaction.
    with get_engine().begin() as conn:
        deleted, inserted = tp.reconcile(conn, new_partners)
    assert deleted == 1  # the stale Marriott row
    assert inserted == 2

    # Read back through the facade query layer (what the API/UI use) — exactly the new rows.
    with get_engine().connect() as conn:
        partners = pq.get_transfer_partners(conn)
    assert {p["airline_code"] for p in partners} == {"SQ", "AS"}  # snapshot replaced

    # And the exact (bank_program_id, airline_code) pairs — proves the stale (6,'AS') row is gone,
    # not merely re-pointed (get_transfer_partners doesn't expose bank_program_id).
    with get_engine().connect() as conn:
        pairs = conn.execute(
            text("SELECT bank_program_id, airline_code FROM pp.transfer_partners")
        ).fetchall()
    assert {tuple(r) for r in pairs} == {(1, "SQ"), (5, "AS")}  # no stale (6, 'AS')
