"""
CTN database — connection, schema, retry helpers, reading aggregation, seeding.

Raw solar readings land in `generation_readings`. They accumulate per device
until one tonne of avoided CO2 has been reached, at which point a discrete
credit is issued in `credits` and the contributing readings are marked consumed
so re-ingesting the same data is a no-op.
"""

import asyncio
import hashlib
import json
import os
from pathlib import Path
from typing import Iterable, Optional

import aiosqlite
from databases import Database
from passlib.context import CryptContext

import config

DATABASE_URL = config.DATABASE_URL

# `sqlite:///relative.db` and `sqlite:////absolute/path.db` both reduce to the
# filesystem path by dropping the scheme and its three slashes; an absolute URL
# keeps its leading slash because it carries a fourth.
DB_PATH = DATABASE_URL.replace("sqlite:///", "")


def _prepare_database_directory() -> Path:
    """
    Ensure the database's parent directory exists and report where data lives.

    On a container host the working directory is ephemeral: unless the path
    points at a mounted volume, every deploy starts from an empty file and all
    accounts, listings, and purchases are lost. Logging the resolved path makes
    that visible instead of silently surprising.
    """
    resolved = Path(DB_PATH).expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)

    existed = resolved.exists()
    print(f"• Database: {resolved} ({'existing' if existed else 'new'})")

    if config.IS_PRODUCTION and not _looks_persistent(resolved):
        print(
            "⚠ The database is not on a mounted volume — this deployment will "
            "lose all data on the next restart. Set DATABASE_URL to a path "
            "inside a persistent volume."
        )

    return resolved


def _looks_persistent(path: Path) -> bool:
    """
    Whether the database path is plausibly on a mounted volume.

    Hosts expose volumes at a dedicated mount point rather than inside the
    application directory, so a path under the working directory is treated as
    ephemeral. `PERSISTENT_DATA_DIR` names the mount when it is known.
    """
    mount = os.getenv("PERSISTENT_DATA_DIR") or os.getenv("RAILWAY_VOLUME_MOUNT_PATH")
    if mount:
        return str(path).startswith(str(Path(mount).resolve()))

    return not str(path).startswith(str(Path.cwd().resolve()))


database = Database(DATABASE_URL)
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


# ── Schema ─────────────────────────────────────────────────────────────────

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('installer', 'buyer', 'admin')),
    wallet_address TEXT UNIQUE,
    created_at REAL NOT NULL DEFAULT (strftime('%s', 'now')),
    updated_at REAL NOT NULL DEFAULT (strftime('%s', 'now'))
);

CREATE TABLE IF NOT EXISTS devices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id TEXT UNIQUE NOT NULL,
    owner_user_id INTEGER REFERENCES users(id),
    location TEXT DEFAULT 'India',
    created_at REAL NOT NULL DEFAULT (strftime('%s', 'now'))
);

CREATE TABLE IF NOT EXISTS generation_readings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    reading_id TEXT UNIQUE NOT NULL,
    device_id TEXT,
    owner_user_id INTEGER REFERENCES users(id),
    total_kwh REAL NOT NULL DEFAULT 0,
    co2_avoided_kg REAL NOT NULL DEFAULT 0,
    timestamp TEXT,
    methodology TEXT,
    standard TEXT,
    location TEXT DEFAULT 'India',
    signature TEXT,
    credit_id INTEGER REFERENCES credits(id),
    consumed_by_credit_id INTEGER REFERENCES credits(id),
    created_at REAL NOT NULL DEFAULT (strftime('%s', 'now'))
);

CREATE TABLE IF NOT EXISTS credits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    credit_id INTEGER UNIQUE NOT NULL,
    device_id TEXT,
    owner_user_id INTEGER REFERENCES users(id),
    total_kwh REAL NOT NULL DEFAULT 0,
    co2_avoided_kg REAL NOT NULL DEFAULT 1000,
    period_start TEXT,
    period_end TEXT,
    methodology TEXT,
    standard TEXT,
    location TEXT DEFAULT 'India',
    contributing_readings TEXT, -- JSON array of {reading_id, signature, id}
    status TEXT NOT NULL DEFAULT 'verified'
        CHECK (status IN ('pending', 'verified', 'listed', 'reserved', 'sold', 'retired')),
    on_chain_id INTEGER,
    ipfs_hash TEXT,
    tx_hash TEXT,
    minted_at REAL,
    retired_at REAL,
    retire_tx_hash TEXT,
    listed_at REAL,
    reserved_by INTEGER REFERENCES users(id),
    reserved_at REAL,
    sold_at REAL,
    buyer_user_id INTEGER REFERENCES users(id),
    contract_version TEXT DEFAULT 'new'
        CHECK (contract_version IN ('old', 'new')),
    created_at REAL NOT NULL DEFAULT (strftime('%s', 'now'))
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    admin_user_id INTEGER NOT NULL REFERENCES users(id),
    action TEXT NOT NULL,
    target_type TEXT NOT NULL,
    target_id TEXT,
    reason TEXT NOT NULL,
    details TEXT,
    created_at REAL NOT NULL DEFAULT (strftime('%s', 'now'))
);

CREATE TABLE IF NOT EXISTS marketplace_transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    buyer_user_id INTEGER NOT NULL REFERENCES users(id),
    credit_ids TEXT NOT NULL,
    quantity INTEGER NOT NULL DEFAULT 1,
    total_amount_usd REAL NOT NULL DEFAULT 0,
    total_amount_inr REAL NOT NULL DEFAULT 0,
    payment_status TEXT NOT NULL DEFAULT 'pending'
        CHECK (payment_status IN ('pending', 'completed', 'failed', 'refunded')),
    payment_method TEXT DEFAULT 'simulated',
    tx_hash TEXT,
    created_at REAL NOT NULL DEFAULT (strftime('%s', 'now')),
    completed_at REAL
);

CREATE TABLE IF NOT EXISTS wallet_nonces (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    nonce TEXT NOT NULL,
    created_at REAL NOT NULL DEFAULT (strftime('%s', 'now')),
    used INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_credits_owner ON credits(owner_user_id);
CREATE INDEX IF NOT EXISTS idx_credits_status ON credits(status);
CREATE INDEX IF NOT EXISTS idx_credits_device ON credits(device_id);
CREATE INDEX IF NOT EXISTS idx_credits_reserved ON credits(reserved_by, reserved_at);
CREATE INDEX IF NOT EXISTS idx_credits_on_chain ON credits(on_chain_id);
CREATE INDEX IF NOT EXISTS idx_readings_owner ON generation_readings(owner_user_id);
CREATE INDEX IF NOT EXISTS idx_readings_unconsumed ON generation_readings(consumed_by_credit_id);
CREATE INDEX IF NOT EXISTS idx_audit_admin ON audit_log(admin_user_id);
CREATE INDEX IF NOT EXISTS idx_transactions_buyer ON marketplace_transactions(buyer_user_id);
"""

# Columns added after the initial release. SQLite has no "ADD COLUMN IF NOT
# EXISTS", so existing databases are upgraded by inspecting the table first.
MIGRATIONS = {
    "credits": {
        "on_chain_id": "INTEGER",
        "ipfs_hash": "TEXT",
        "tx_hash": "TEXT",
        "minted_at": "REAL",
        "retired_at": "REAL",
        "retire_tx_hash": "TEXT",
    },
}


# ── Retry helpers ──────────────────────────────────────────────────────────

_RETRY_DELAYS = (0.05, 0.1, 0.2)


async def _with_lock_retry(operation, query, values):
    """
    Run a database operation, retrying with backoff while SQLite reports the
    file as locked. Any other error propagates immediately.
    """
    for attempt, delay in enumerate((*_RETRY_DELAYS, None)):
        try:
            if values is not None:
                return await operation(query=query, values=values)
            return await operation(query=query)
        except Exception as exc:
            if delay is None or "database is locked" not in str(exc).lower():
                raise
            await asyncio.sleep(delay)


async def db_execute_with_retry(query, values=None):
    """Execute a write, retrying briefly on lock contention."""
    return await _with_lock_retry(database.execute, query, values)


async def db_fetch_with_retry(query, values=None):
    """Fetch rows, retrying briefly on lock contention."""
    return await _with_lock_retry(database.fetch_all, query, values)


# ── Schema management ──────────────────────────────────────────────────────

async def _apply_schema():
    """
    Create tables and add any columns missing from an older database.

    DDL runs through raw aiosqlite because the `databases` query compiler treats
    the % in strftime('%s','now') defaults as a parameter placeholder.
    """
    async with aiosqlite.connect(DB_PATH) as raw_db:
        await raw_db.executescript(SCHEMA_SQL)

        for table, columns in MIGRATIONS.items():
            cursor = await raw_db.execute(f"PRAGMA table_info({table})")
            existing = {row[1] for row in await cursor.fetchall()}
            for column, column_type in columns.items():
                if column not in existing:
                    await raw_db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}")
                    print(f"✓ Added column {table}.{column}")

        await raw_db.commit()


# ── Credit issuance ────────────────────────────────────────────────────────

def _reading_fingerprint(reading: dict) -> str:
    """
    Deterministic identifier for a reading. Providers that supply their own
    reading_id keep it; otherwise device and timestamp identify the sample, so
    re-ingesting the same CSV cannot create duplicates.
    """
    supplied = str(reading.get("reading_id", "") or "")
    if supplied:
        return supplied

    basis = f"{reading.get('device_id')}_{reading.get('timestamp')}"
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]


def _sign_reading(reading: dict) -> str:
    """Hash the canonical form of a reading so later tampering is detectable."""
    return hashlib.sha256(json.dumps(reading, sort_keys=True).encode("utf-8")).hexdigest()


async def _next_credit_id() -> int:
    """
    Allocate the next public credit number.

    Derived from the current maximum rather than a row count, so deleting a
    credit cannot produce an id that collides with an existing one.
    """
    row = await database.fetch_one("SELECT COALESCE(MAX(credit_id), 0) AS max_id FROM credits")
    return (row["max_id"] if row else 0) + 1


async def _insert_readings(readings: Iterable[dict], owner_user_id: int) -> int:
    """Insert readings that aren't already stored. Returns the number added."""
    existing = {
        row["reading_id"]
        for row in await database.fetch_all("SELECT reading_id FROM generation_readings")
    }

    inserted = 0
    for reading in readings:
        fingerprint = _reading_fingerprint(reading)
        if fingerprint in existing:
            continue

        canonical = {
            "device_id": reading.get("device_id"),
            "timestamp": reading.get("timestamp"),
            "total_kwh": reading.get("total_kwh", 0),
            "co2_avoided_kg": reading.get("co2_avoided_kg", 0),
            "methodology": config.METHODOLOGY,
            "standard": config.STANDARD,
            "location": reading.get("location", "India"),
        }

        await database.execute(
            query="""INSERT INTO generation_readings
                (reading_id, device_id, owner_user_id, total_kwh, co2_avoided_kg,
                 timestamp, methodology, standard, location, signature)
                VALUES (:reading_id, :device_id, :owner_user_id, :total_kwh, :co2_avoided_kg,
                        :timestamp, :methodology, :standard, :location, :signature)""",
            values={
                "reading_id": fingerprint,
                "owner_user_id": reading.get("owner_user_id", owner_user_id),
                "signature": _sign_reading(canonical),
                **canonical,
            },
        )
        existing.add(fingerprint)
        inserted += 1

    return inserted


async def _issue_credit(
    device_id: str,
    owner_user_id: int,
    total_kwh: float,
    period_start: str,
    period_end: str,
    location: str,
    contributing: list[dict],
) -> int:
    """Create one discrete credit and mark its contributing readings consumed."""
    from ipfs_utils import upload_credit_to_ipfs

    credit_id = await _next_credit_id()
    certificate = {
        "credit_id": credit_id,
        "device_id": device_id,
        "owner_user_id": owner_user_id,
        "total_kwh": total_kwh,
        "co2_avoided_kg": config.KG_CO2_PER_CREDIT,
        "period_start": period_start,
        "period_end": period_end,
        "methodology": config.METHODOLOGY,
        "standard": config.STANDARD,
        "location": location,
        "contributing_readings": contributing,
    }

    row_id = await database.execute(
        query="""INSERT INTO credits
            (credit_id, device_id, owner_user_id, total_kwh, co2_avoided_kg,
             period_start, period_end, methodology, standard, location,
             status, contributing_readings, ipfs_hash)
            VALUES (:credit_id, :device_id, :owner_user_id, :total_kwh, :co2_avoided_kg,
                    :period_start, :period_end, :methodology, :standard, :location,
                    'verified', :contributing_readings, :ipfs_hash)""",
        values={
            "credit_id": credit_id,
            "device_id": device_id,
            "owner_user_id": owner_user_id,
            "total_kwh": total_kwh,
            "co2_avoided_kg": config.KG_CO2_PER_CREDIT,
            "period_start": period_start,
            "period_end": period_end,
            "methodology": config.METHODOLOGY,
            "standard": config.STANDARD,
            "location": location,
            "contributing_readings": json.dumps(contributing),
            "ipfs_hash": upload_credit_to_ipfs(certificate),
        },
    )

    reading_ids = [item["id"] for item in contributing]
    placeholders = ", ".join(f":r{i}" for i in range(len(reading_ids)))
    await database.execute(
        query=f"""UPDATE generation_readings SET consumed_by_credit_id = :credit_row_id
                  WHERE id IN ({placeholders})""",
        values={"credit_row_id": row_id, **{f"r{i}": rid for i, rid in enumerate(reading_ids)}},
    )

    return row_id


async def process_raw_readings(
    readings: list[dict],
    owner_user_id: int,
) -> tuple[int, int]:
    """
    Ingest readings and aggregate them into whole-tonne credits.

    Returns (readings_inserted, credits_issued). Both steps are idempotent:
    readings are keyed by fingerprint and only unconsumed readings contribute to
    a new credit, so any leftover CO2 carries forward to the next ingestion.
    """
    inserted = await _insert_readings(readings, owner_user_id)

    unconsumed = await database.fetch_all(
        """SELECT id, reading_id, device_id, owner_user_id, total_kwh,
                  co2_avoided_kg, timestamp, location, signature
           FROM generation_readings
           WHERE consumed_by_credit_id IS NULL
           ORDER BY device_id, timestamp ASC"""
    )

    # Credits are per-device, so each device accumulates its own remainder.
    by_device: dict[str, list] = {}
    for row in unconsumed:
        by_device.setdefault(row["device_id"], []).append(row)

    credits_issued = 0
    for device_id, group in by_device.items():
        acc_co2 = 0.0
        acc_kwh = 0.0
        acc_readings: list[dict] = []
        period_start: Optional[str] = None

        for row in group:
            if period_start is None:
                period_start = row["timestamp"]

            acc_co2 += row["co2_avoided_kg"]
            acc_kwh += row["total_kwh"]
            acc_readings.append(
                {"reading_id": row["reading_id"], "signature": row["signature"], "id": row["id"]}
            )

            while acc_co2 >= config.KG_CO2_PER_CREDIT:
                await _issue_credit(
                    device_id=device_id,
                    owner_user_id=row["owner_user_id"] or owner_user_id,
                    total_kwh=acc_kwh,
                    period_start=period_start,
                    period_end=row["timestamp"],
                    location=row["location"] or "India",
                    contributing=acc_readings,
                )
                credits_issued += 1

                acc_co2 -= config.KG_CO2_PER_CREDIT
                acc_kwh = 0.0
                acc_readings = []
                period_start = row["timestamp"]

    return inserted, credits_issued


# ── Seeding ────────────────────────────────────────────────────────────────

async def _seed_user(email: str, password: str, role: str, wallet_address: str = None) -> int:
    """Create the account if absent. Returns its id either way."""
    existing = await database.fetch_one(
        query="SELECT id FROM users WHERE email = :email", values={"email": email}
    )
    if existing:
        return existing["id"]

    user_id = await database.execute(
        query="""INSERT INTO users (email, password_hash, role, wallet_address)
                 VALUES (:email, :password_hash, :role, :wallet_address)""",
        values={
            "email": email,
            "password_hash": pwd_context.hash(password),
            "role": role,
            "wallet_address": wallet_address,
        },
    )
    print(f"✓ Seeded {role} account: {email}")
    return user_id


async def _seed_demo_data():
    """Create the demo installer, its device, and the seed reading history."""
    installer_id = await _seed_user(
        config.DEMO_INSTALLER_EMAIL,
        config.DEMO_INSTALLER_PASSWORD,
        "installer",
        wallet_address=config.DEMO_INSTALLER_WALLET,
    )

    # Earlier builds seeded a placeholder that only looked like an address.
    # It is not valid hex, so any mint to it would have reverted on-chain.
    #
    # The wildcard is bound as a parameter rather than written into the SQL:
    # the query compiler applies %-formatting to the statement text, so a
    # literal % in a LIKE pattern raises at execution time.
    await database.execute(
        query="""UPDATE users SET wallet_address = :addr
                 WHERE email = :email AND wallet_address LIKE :placeholder""",
        values={
            "addr": config.DEMO_INSTALLER_WALLET,
            "email": config.DEMO_INSTALLER_EMAIL,
            "placeholder": "0xDemo%",
        },
    )

    existing_device = await database.fetch_one(
        query="SELECT id FROM devices WHERE device_id = :device_id",
        values={"device_id": config.DEMO_DEVICE_ID},
    )
    if not existing_device:
        await database.execute(
            query="""INSERT INTO devices (device_id, owner_user_id, location)
                     VALUES (:device_id, :owner_user_id, :location)""",
            values={
                "device_id": config.DEMO_DEVICE_ID,
                "owner_user_id": installer_id,
                "location": config.DEMO_DEVICE_LOCATION,
            },
        )
        print(f"✓ Seeded demo device: {config.DEMO_DEVICE_ID}")

    await sync_readings_from_ipfs(installer_id)


async def sync_readings_from_ipfs(owner_user_id: int):
    """
    Load the published seed dataset and feed it through the normal ingestion
    path. A network failure here leaves the API running on whatever is already
    in the database rather than blocking startup.
    """
    import requests

    from data_utils import to_discrete_readings

    try:
        response = requests.get(config.IPFS_SEED_URL, timeout=config.IPFS_SEED_TIMEOUT_SECONDS)
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:
        print(f"⚠ Could not load seed dataset from IPFS: {exc}")
        return

    raw = payload if isinstance(payload, list) else payload.get("credits", [])
    inserted, issued = await process_raw_readings(to_discrete_readings(raw), owner_user_id)
    print(f"✓ Synced seed dataset: {inserted} new readings, {issued} new credits")


# ── Lifecycle ──────────────────────────────────────────────────────────────

async def init_db():
    """Connect, bring the schema up to date, and seed baseline accounts."""
    _prepare_database_directory()

    await database.connect()
    await _apply_schema()

    await _seed_user(config.ADMIN_EMAIL, config.ADMIN_PASSWORD, "admin")

    if config.SEED_DEMO_DATA:
        await _seed_demo_data()


async def shutdown_db():
    """Close the connection pool."""
    await database.disconnect()
