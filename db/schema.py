"""
DDL definitions and idempotent migration runner.

migrate() is safe to call on every startup. The baseline DDL reflects the CURRENT (latest)
schema — all CREATE ... IF NOT EXISTS, so re-runs are no-ops. Migrations transform older
databases forward; ones that aren't intrinsically idempotent (the routes_queue repivot, the
timestamp-column renames) are written as conditional callables that act only when the old
shape is still present, so they're safe on a fresh DB (where the baseline is already current)
and on a re-run.

Column-name convention: system timestamps carry a `_utc` suffix (they're true UTC instants);
flight times carry `_local` (local airport time, what the traveler reads off the board).
"""

import logging
from collections.abc import Callable

from db.connection import get_connection

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# DDL (baseline = latest schema)
# ---------------------------------------------------------------------------

_CREATE_SCHEMA_VERSION = """
CREATE TABLE IF NOT EXISTS schema_version (
    version        INTEGER PRIMARY KEY,
    applied_at_utc TIMESTAMP NOT NULL DEFAULT now(),
    description    VARCHAR
);
"""

_CREATE_ROUTES_QUEUE = """
CREATE TABLE IF NOT EXISTS routes_queue (
    origin              VARCHAR(3)  NOT NULL,
    dest                VARCHAR(3)  NOT NULL,
    airline             VARCHAR(10) NOT NULL DEFAULT 'alaska',  -- scraper slug (queue per airline)
    priority_tier       VARCHAR(4)  NOT NULL DEFAULT 'LOW',
    search_count        INTEGER     NOT NULL DEFAULT 0,
    decayed_demand      DOUBLE      NOT NULL DEFAULT 0,
    last_search_at_utc  TIMESTAMP,
    change_rate         DOUBLE      NOT NULL DEFAULT 0.5,
    last_cheapest       VARCHAR,
    interval_h          DOUBLE,
    last_scraped_at_utc TIMESTAMP,              -- NULL = never scraped
    next_scrape_at_utc  TIMESTAMP   NOT NULL DEFAULT now(),
    created_at_utc      TIMESTAMP   NOT NULL DEFAULT now(),
    PRIMARY KEY (origin, dest, airline)
);
"""

_CREATE_FLIGHTS_SEQ = "CREATE SEQUENCE IF NOT EXISTS flights_id_seq;"

_CREATE_FLIGHTS = """
CREATE TABLE IF NOT EXISTS flights (
    id                BIGINT      PRIMARY KEY DEFAULT nextval('flights_id_seq'),
    -- Route
    origin            VARCHAR(3)  NOT NULL,
    destination       VARCHAR(3)  NOT NULL,
    date              DATE        NOT NULL,
    -- Program
    airline           VARCHAR(10) NOT NULL,
    program           VARCHAR(50) NOT NULL,
    source            VARCHAR(20),   -- scraper that produced this row, e.g. "alaska"
    -- Award cost
    points_cost       INTEGER     NOT NULL CHECK (points_cost > 0),
    cash_cost         DECIMAL(8,2) NOT NULL DEFAULT 0.0,
    -- Itinerary
    stops             TINYINT     NOT NULL DEFAULT 0,
    cabin_class       VARCHAR(20) NOT NULL
        CHECK (cabin_class IN ('economy', 'premium_economy', 'business', 'first')),
    available_seats   INTEGER     NOT NULL DEFAULT -1,
    raw_flight_number VARCHAR(20),   -- "UNKNOWN" sentinel for multi-leg (never NULL)
    partner_airline   VARCHAR(10),
    -- Freshness (UTC instants)
    scraped_at_utc    TIMESTAMP   NOT NULL,
    expires_at_utc    TIMESTAMP   NOT NULL,
    -- Timing (LOCAL airport time, not an instant)
    departure_time_local    TIMESTAMPTZ,
    arrival_time_local      TIMESTAMPTZ,
    duration_minutes        INTEGER,
    -- Aircraft
    aircraft_type           VARCHAR(10),
    -- Fare details
    is_saver                BOOLEAN NOT NULL DEFAULT FALSE,
    fare_class              VARCHAR(10),
    -- Connection details
    layover_airports        VARCHAR,
    layover_duration_minutes INTEGER,
    next_day_arrival        BOOLEAN NOT NULL DEFAULT FALSE,
    mixed_cabin             BOOLEAN NOT NULL DEFAULT FALSE,
    -- One row per route+date+cabin+flight combo
    UNIQUE (origin, destination, date, airline, cabin_class, raw_flight_number)
);
"""

# Baseline indexes — only those on STABLE columns (not renamed by any migration). The two
# indexes on renamed columns (idx_flights_expires on expires_at_utc, idx_routes_queue_due on
# next_scrape_at_utc) are owned by migration v6 so the baseline never references a column that
# doesn't exist yet on a not-yet-migrated DB.
_CREATE_FLIGHTS_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_flights_route_date ON flights (origin, destination, date);",
    "CREATE INDEX IF NOT EXISTS idx_flights_program    ON flights (program, cabin_class);",
]

# ---------------------------------------------------------------------------
# Cash fares (v7+)
# ---------------------------------------------------------------------------

_CREATE_CASH_FARES_SEQ = "CREATE SEQUENCE IF NOT EXISTS cash_fares_id_seq;"

_CREATE_CASH_FARES = """
CREATE TABLE IF NOT EXISTS cash_fares (
    id              BIGINT       PRIMARY KEY DEFAULT nextval('cash_fares_id_seq'),
    -- shared natural key with flights (per-flight grain, nonstop only)
    origin          VARCHAR(3)   NOT NULL,
    destination     VARCHAR(3)   NOT NULL,
    date            DATE         NOT NULL,
    airline         VARCHAR(10)  NOT NULL,   -- marketing IATA; matches flights.airline
    cabin_class     VARCHAR(20)  NOT NULL
        CHECK (cabin_class IN ('economy', 'premium_economy', 'business', 'first')),
    flight_number   VARCHAR(20)  NOT NULL,   -- NOT NULL; no "UNKNOWN" — nonstop only
    cash_price      DECIMAL(10,2) NOT NULL CHECK (cash_price > 0),
    currency        VARCHAR(3)   NOT NULL DEFAULT 'USD',
    source          VARCHAR(20)  NOT NULL DEFAULT 'google_flights',
    scraped_at_utc  TIMESTAMP    NOT NULL,
    expires_at_utc  TIMESTAMP    NOT NULL,
    UNIQUE (origin, destination, date, airline, cabin_class, flight_number)
);
"""

_CREATE_CASH_FARES_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_cash_fares_route_date ON cash_fares (origin, destination, date);",  # noqa: E501
]

# ---------------------------------------------------------------------------
# Banks / Transfer Partners
# ---------------------------------------------------------------------------

_CREATE_BANK_PROGRAMS = """
CREATE TABLE IF NOT EXISTS bank_programs (
    id          SMALLINT    PRIMARY KEY,
    name        VARCHAR     NOT NULL UNIQUE,
    short_code  VARCHAR(10) NOT NULL UNIQUE,
    created_at_utc TIMESTAMP NOT NULL DEFAULT now()
);
"""

_CREATE_TRANSFER_PARTNERS = """
CREATE TABLE IF NOT EXISTS transfer_partners (
    bank_program_id  SMALLINT     NOT NULL,
    airline_code     VARCHAR(10)  NOT NULL,
    program_name     VARCHAR      NOT NULL,
    transfer_ratio   DECIMAL(5,2) NOT NULL DEFAULT 1.00,
    min_transfer     INTEGER      NOT NULL DEFAULT 1000,
    transfer_increment INTEGER    NOT NULL DEFAULT 1000,
    PRIMARY KEY (bank_program_id, airline_code)
);
"""

_CREATE_TRANSFER_BONUSES_SEQ = "CREATE SEQUENCE IF NOT EXISTS transfer_bonuses_id_seq;"

_CREATE_TRANSFER_BONUSES = """
CREATE TABLE IF NOT EXISTS transfer_bonuses (
    id               BIGINT      PRIMARY KEY DEFAULT nextval('transfer_bonuses_id_seq'),
    bank_program_id  SMALLINT    NOT NULL,
    airline_code     VARCHAR(10) NOT NULL,
    bonus_pct        INTEGER     NOT NULL CHECK (bonus_pct > 0),
    starts_at        DATE        NOT NULL,
    ends_at          DATE        NOT NULL,
    notes            VARCHAR,
    created_at_utc   TIMESTAMP   NOT NULL DEFAULT now(),
    updated_at_utc   TIMESTAMP   NOT NULL DEFAULT now(),
    CHECK (ends_at >= starts_at),
    UNIQUE (bank_program_id, airline_code, starts_at)
);
"""

_CREATE_BANK_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_transfer_bonuses_active "
    "ON transfer_bonuses (airline_code, starts_at, ends_at);",
]

_CREATE_AIRLINE_BUDGET = """
CREATE TABLE IF NOT EXISTS airline_budget (
    airline          VARCHAR(10) PRIMARY KEY,
    tokens           DOUBLE      NOT NULL DEFAULT 0,
    capacity         DOUBLE      NOT NULL DEFAULT 0,
    refill_per_hour  DOUBLE      NOT NULL DEFAULT 0,
    last_refill_utc  TIMESTAMP   NOT NULL DEFAULT now()
)
"""


# ---------------------------------------------------------------------------
# Callable migrations (conditional + idempotent)
# ---------------------------------------------------------------------------


def _columns(conn, table: str) -> set[str]:
    # PRAGMA table_info → (cid, name, type, notnull, dflt_value, pk); name is index 1
    return {r[1] for r in conn.execute(f"PRAGMA table_info('{table}')").fetchall()}


def _migrate_v5(conn) -> None:
    """Per-airline routes_queue: add `airline` + repivot PK to (origin, dest, airline).

    No-op if already per-airline (a fresh DB whose baseline is current, or a re-run). DuckDB
    can't ALTER a composite PK in place, so create the new shape, copy rows (tagged 'alaska'),
    drop, rename. Timestamp columns keep their pre-_utc names here; v6 renames them.
    """
    if "airline" in _columns(conn, "routes_queue"):
        return
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS routes_queue_new (
            origin          VARCHAR(3)  NOT NULL,
            dest            VARCHAR(3)  NOT NULL,
            airline         VARCHAR(10) NOT NULL DEFAULT 'alaska',
            priority_tier   VARCHAR(4)  NOT NULL DEFAULT 'LOW',
            search_count    INTEGER     NOT NULL DEFAULT 0,
            last_scraped_at TIMESTAMP,
            next_scrape_at  TIMESTAMP   NOT NULL DEFAULT now(),
            created_at      TIMESTAMP   NOT NULL DEFAULT now(),
            PRIMARY KEY (origin, dest, airline)
        )
        """
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO routes_queue_new
            (origin, dest, airline, priority_tier, search_count,
             last_scraped_at, next_scrape_at, created_at)
        SELECT origin, dest, 'alaska', priority_tier, search_count,
               last_scraped_at, next_scrape_at, created_at
        FROM routes_queue
        """
    )
    conn.execute("DROP TABLE routes_queue")
    conn.execute("ALTER TABLE routes_queue_new RENAME TO routes_queue")


# (table, old_name, new_name) for every timestamp column.
_V6_RENAMES = [
    ("flights", "scraped_at", "scraped_at_utc"),
    ("flights", "expires_at", "expires_at_utc"),
    ("flights", "departure_time", "departure_time_local"),
    ("flights", "arrival_time", "arrival_time_local"),
    ("routes_queue", "last_scraped_at", "last_scraped_at_utc"),
    ("routes_queue", "next_scrape_at", "next_scrape_at_utc"),
    ("routes_queue", "created_at", "created_at_utc"),
    ("schema_version", "applied_at", "applied_at_utc"),
    ("bank_programs", "created_at", "created_at_utc"),
    ("transfer_bonuses", "created_at", "created_at_utc"),
    ("transfer_bonuses", "updated_at", "updated_at_utc"),
]

# DuckDB blocks RENAME COLUMN on a table that has any explicit CREATE INDEX (PK/UNIQUE are
# fine), so drop every index on an affected table before renaming, then recreate on the final
# names. These two are owned entirely by v6 (the baseline omits them).
_V6_DROP_INDEXES = [
    "idx_flights_route_date",
    "idx_flights_expires",
    "idx_flights_program",
    "idx_routes_queue_due",
    "idx_transfer_bonuses_active",
]
_V6_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_flights_route_date ON flights (origin, destination, date)",
    "CREATE INDEX IF NOT EXISTS idx_flights_expires ON flights (expires_at_utc)",
    "CREATE INDEX IF NOT EXISTS idx_flights_program ON flights (program, cabin_class)",
    "CREATE INDEX IF NOT EXISTS idx_routes_queue_due ON routes_queue (next_scrape_at_utc)",
    "CREATE INDEX IF NOT EXISTS idx_transfer_bonuses_active "
    "ON transfer_bonuses (airline_code, starts_at, ends_at)",
]


def _migrate_v6(conn) -> None:
    """Suffix timestamp columns: _utc for system instants, _local for flight times.

    Conditional + idempotent: only renames columns whose OLD name is still present. On a fresh
    DB (baseline already uses the final names) nothing is pending, so it just ensures the two
    v6-owned indexes exist. The system timestamps were always UTC (connection SET
    TimeZone='UTC' + scraper stamps datetime.now(timezone.utc)); this makes that explicit.

    No-op if already on final names (a fresh DB whose baseline is current, or a re-run).
    """
    cols = {t: _columns(conn, t) for t in {r[0] for r in _V6_RENAMES}}
    pending = [
        (t, o, n) for (t, o, n) in _V6_RENAMES if t in cols and o in cols[t] and n not in cols[t]
    ]

    if pending:
        for ix in _V6_DROP_INDEXES:
            conn.execute(f"DROP INDEX IF EXISTS {ix}")
        for table, old, new in pending:
            conn.execute(f"ALTER TABLE {table} RENAME COLUMN {old} TO {new}")

    # Always ensure the final-name indexes exist (a fresh DB has no pending renames but still
    # needs idx_flights_expires / idx_routes_queue_due, which the baseline omits).
    for stmt in _V6_INDEXES:
        conn.execute(stmt)


def _migrate_v7(conn) -> None:
    """v7: cash_fares (baseline creates it), a transfer_bonuses UNIQUE on
    (bank_program_id, airline_code, starts_at) for idempotent upserts, and drop the dead
    airports table. Conditional + idempotent — no-op once applied.

    DuckDB can't ALTER ADD CONSTRAINT, so the UNIQUE is added via create-copy-drop-rename
    (transfer_bonuses is empty in prod, so this copies zero rows). On a fresh DB the baseline
    already created transfer_bonuses WITH the unique, so the rebuild is skipped.
    """
    has_unique = conn.execute(
        "SELECT count(*) FROM duckdb_constraints() "
        "WHERE table_name = 'transfer_bonuses' AND constraint_type = 'UNIQUE'"
    ).fetchone()[0]
    if not has_unique:
        conn.execute("DROP INDEX IF EXISTS idx_transfer_bonuses_active")
        conn.execute(
            """
            CREATE TABLE transfer_bonuses_new (
                id               BIGINT      PRIMARY KEY DEFAULT nextval('transfer_bonuses_id_seq'),
                bank_program_id  SMALLINT    NOT NULL,
                airline_code     VARCHAR(10) NOT NULL,
                bonus_pct        INTEGER     NOT NULL CHECK (bonus_pct > 0),
                starts_at        DATE        NOT NULL,
                ends_at          DATE        NOT NULL,
                notes            VARCHAR,
                created_at_utc   TIMESTAMP   NOT NULL DEFAULT now(),
                updated_at_utc   TIMESTAMP   NOT NULL DEFAULT now(),
                CHECK (ends_at >= starts_at),
                UNIQUE (bank_program_id, airline_code, starts_at)
            )
            """
        )
        conn.execute(
            """
            INSERT INTO transfer_bonuses_new
                (id, bank_program_id, airline_code, bonus_pct, starts_at, ends_at,
                 notes, created_at_utc, updated_at_utc)
            SELECT id, bank_program_id, airline_code, bonus_pct, starts_at, ends_at,
                   notes, created_at_utc, updated_at_utc
            FROM transfer_bonuses
            """
        )
        conn.execute("DROP TABLE transfer_bonuses")
        conn.execute("ALTER TABLE transfer_bonuses_new RENAME TO transfer_bonuses")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_transfer_bonuses_active "
            "ON transfer_bonuses (airline_code, starts_at, ends_at)"
        )
    # drop the dead airports table (0 rows, unused — UI owns airport resolution)
    conn.execute("DROP TABLE IF EXISTS airports")


# ---------------------------------------------------------------------------
# Migrations: add future schema changes here in order.
# Each entry: (version, description, sql | list[sql] | Callable[[conn], None])
# ---------------------------------------------------------------------------
_MIGRATIONS: list[tuple[int, str, str | list[str] | Callable]] = [
    # v1 is the baseline — no migration needed (tables created above)
    (
        2,
        "add timing, aircraft, and fare detail columns",
        [
            "ALTER TABLE flights ADD COLUMN IF NOT EXISTS departure_time_local TIMESTAMPTZ",
            "ALTER TABLE flights ADD COLUMN IF NOT EXISTS arrival_time_local TIMESTAMPTZ",
            "ALTER TABLE flights ADD COLUMN IF NOT EXISTS duration_minutes INTEGER",
            "ALTER TABLE flights ADD COLUMN IF NOT EXISTS aircraft_type VARCHAR(10)",
            "ALTER TABLE flights ADD COLUMN IF NOT EXISTS is_saver BOOLEAN DEFAULT FALSE",
            "ALTER TABLE flights ADD COLUMN IF NOT EXISTS fare_class VARCHAR(10)",
            "ALTER TABLE flights ADD COLUMN IF NOT EXISTS layover_airports VARCHAR",
            "ALTER TABLE flights ADD COLUMN IF NOT EXISTS layover_duration_minutes INTEGER",
            "ALTER TABLE flights ADD COLUMN IF NOT EXISTS next_day_arrival BOOLEAN DEFAULT FALSE",
            "ALTER TABLE flights ADD COLUMN IF NOT EXISTS mixed_cabin BOOLEAN DEFAULT FALSE",
        ],
    ),
    (
        3,
        "add bank/transfer tables; drop scrape_logs",
        [
            "DROP TABLE IF EXISTS scrape_logs",
            "DROP SEQUENCE IF EXISTS scrape_logs_id_seq",
        ],
    ),
    (
        4,
        "tag flights with the scraper that produced them",
        [
            "ALTER TABLE flights ADD COLUMN IF NOT EXISTS source VARCHAR(20)",
        ],
    ),
    (
        5,
        "per-airline routes_queue: add airline column, repivot PK to (origin,dest,airline)",
        _migrate_v5,
    ),
    (
        6,
        "suffix timestamp columns: _utc for system instants, _local for flight times",
        _migrate_v6,
    ),
    (
        7,
        "add cash_fares; transfer_bonuses UNIQUE for idempotent upsert; drop dead airports",
        _migrate_v7,
    ),
    # NOTE: jobs has no v8/v9 (those are scraper-only ondemand_coverage/cash_coverage tables).
    # Numbering this v10 aligns jobs with the shared prod schema; the v8/v9 gap is intentional.
    (
        10,
        "adaptive scheduling: routes_queue scoring columns + airline_budget table",
        [
            # DuckDB cannot ALTER-ADD a NOT NULL column, so these ALTERs drop NOT NULL and
            # backfill via DEFAULT; the baseline CREATE (fresh DBs) carries the full NOT NULL.
            "ALTER TABLE routes_queue ADD COLUMN IF NOT EXISTS decayed_demand DOUBLE DEFAULT 0",
            "ALTER TABLE routes_queue ADD COLUMN IF NOT EXISTS last_search_at_utc TIMESTAMP",
            "ALTER TABLE routes_queue ADD COLUMN IF NOT EXISTS change_rate DOUBLE DEFAULT 0.5",
            "ALTER TABLE routes_queue ADD COLUMN IF NOT EXISTS last_cheapest VARCHAR",
            "ALTER TABLE routes_queue ADD COLUMN IF NOT EXISTS interval_h DOUBLE",
            _CREATE_AIRLINE_BUDGET,
        ],
    ),
]

BASELINE_VERSION = 1


# ---------------------------------------------------------------------------
# migrate()
# ---------------------------------------------------------------------------


def migrate() -> None:
    """
    Idempotent migration runner. Safe to call on every startup.

    1. Creates all baseline tables (IF NOT EXISTS).
    2. Stamps version=1 if not already recorded.
    3. Applies any pending migrations above the current version.
    """
    conn = get_connection()

    # Baseline tables
    for ddl in [
        _CREATE_SCHEMA_VERSION,
        _CREATE_ROUTES_QUEUE,
        _CREATE_FLIGHTS_SEQ,
        _CREATE_FLIGHTS,
        *_CREATE_FLIGHTS_INDEXES,
        _CREATE_CASH_FARES_SEQ,
        _CREATE_CASH_FARES,
        *_CREATE_CASH_FARES_INDEXES,
        _CREATE_BANK_PROGRAMS,
        _CREATE_TRANSFER_PARTNERS,
        _CREATE_TRANSFER_BONUSES_SEQ,
        _CREATE_TRANSFER_BONUSES,
        *_CREATE_BANK_INDEXES,
        _CREATE_AIRLINE_BUDGET,
    ]:
        conn.execute(ddl)

    # Stamp baseline version if not present
    existing = conn.execute(
        "SELECT version FROM schema_version WHERE version = ?", [BASELINE_VERSION]
    ).fetchone()
    if not existing:
        conn.execute(
            "INSERT INTO schema_version (version, description) VALUES (?, ?)",
            [BASELINE_VERSION, "baseline — routes_queue, flights"],
        )
        logger.info("Schema version %d applied (baseline)", BASELINE_VERSION)

    # Apply pending migrations
    current_version: int = (
        conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] or 0
    )

    for version, description, migration in _MIGRATIONS:
        if version > current_version:
            logger.info("Applying migration v%d: %s", version, description)
            if callable(migration):
                migration(conn)
            else:
                statements = [migration] if isinstance(migration, str) else migration
                for stmt in statements:
                    conn.execute(stmt)
            conn.execute(
                "INSERT INTO schema_version (version, description) VALUES (?, ?)",
                [version, description],
            )
            logger.info("Migration v%d applied", version)

    logger.info("Schema up to date at version %d", current_version)
