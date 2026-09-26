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
from typing import Optional

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
    -- Address derived from the device's signing key. The private half stays on
    -- the device, so the platform can verify a reading but never forge one.
    public_key TEXT,
    -- Highest sequence accepted so far; a replayed packet cannot exceed it.
    last_sequence INTEGER NOT NULL DEFAULT 0,
    -- A self-enrolled device records immediately but its credits cannot be sold
    -- until an operator has confirmed the installation is real. Attestation
    -- proves a reading came from this device; it cannot prove the device is
    -- pointed at a real solar array.
    verified INTEGER NOT NULL DEFAULT 0,
    verified_at REAL,
    verified_by INTEGER REFERENCES users(id),
    enrolled_via TEXT,
    -- Nameplate AC capacity. Bounds how much energy an interval can claim.
    rated_capacity_kw REAL,
    -- Site coordinates, for the solar-geometry screen. Optional.
    latitude REAL,
    longitude REAL,
    -- Timestamp and lifetime meter counter of the last accepted reading, the
    -- baseline the next reading is checked against.
    last_reading_at TEXT,
    last_meter_wh INTEGER,
    -- Enclosure-open events the device has reported. Only ever increases.
    tamper_count INTEGER NOT NULL DEFAULT 0,
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
    -- Server-side content hash. Detects corruption at rest; proves nothing
    -- about origin, because anyone who can write the row can recompute it.
    signature TEXT,
    -- The device's own signature and the exact text it signed. Stored verbatim
    -- so a third party can re-verify without trusting this database.
    device_signature TEXT,
    signed_message TEXT,
    sequence INTEGER,
    message_version TEXT,
    meter_wh INTEGER,
    tamper_count INTEGER,
    -- The factor co2_avoided_kg was computed with, frozen at ingestion so a
    -- later methodology change cannot silently restate history.
    emission_factor REAL,
    -- JSON list of screening flags; NULL when nothing stood out.
    anomaly_flags TEXT,
    credit_id INTEGER REFERENCES credits(id),
    -- A reading's CO2 can span two credits. allocated_kg is how much of it has
    -- been assigned so far; consumed_by_credit_id is set once all of it has.
    allocated_kg REAL NOT NULL DEFAULT 0,
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
    -- The exact certificate document whose hash is ipfs_hash. Kept so the
    -- same bytes are pinned whenever pinning happens, not a re-rendering.
    certificate TEXT,
    -- Why a credit is held for review despite its device being confirmed.
    review_hold TEXT,
    -- Mint claim: an opaque token taken atomically before broadcasting, so two
    -- requests can never mint the same credit twice.
    mint_claim TEXT,
    mint_claimed_at REAL,
    mint_tx_hash TEXT,
    retirement_beneficiary TEXT,
    retirement_purpose TEXT,
    created_at REAL NOT NULL DEFAULT (strftime('%s', 'now'))
);

-- Which part of which reading makes up each credit. Every credit's rows sum to
-- exactly one credit's worth of CO2, so it can be recomputed from its evidence.
CREATE TABLE IF NOT EXISTS credit_allocations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    credit_row_id INTEGER NOT NULL REFERENCES credits(id),
    reading_row_id INTEGER NOT NULL REFERENCES generation_readings(id),
    kwh REAL NOT NULL,
    kg REAL NOT NULL
);

-- Device-originated events that change trust: tamper, counter anomalies.
CREATE TABLE IF NOT EXISTS device_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    detail TEXT,
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

CREATE TABLE IF NOT EXISTS device_enrollments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT UNIQUE NOT NULL,
    owner_user_id INTEGER NOT NULL REFERENCES users(id),
    label TEXT,
    location TEXT DEFAULT 'India',
    created_at REAL NOT NULL DEFAULT (strftime('%s', 'now')),
    expires_at REAL NOT NULL,
    used_at REAL,
    device_id TEXT,
    rated_capacity_kw REAL,
    latitude REAL,
    longitude REAL
);

CREATE TABLE IF NOT EXISTS device_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id TEXT NOT NULL,
    requested_by INTEGER NOT NULL REFERENCES users(id),
    location TEXT DEFAULT 'India',
    public_key TEXT,
    notes TEXT,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'approved', 'rejected')),
    reviewed_by INTEGER REFERENCES users(id),
    reviewed_at REAL,
    review_note TEXT,
    rated_capacity_kw REAL,
    latitude REAL,
    longitude REAL,
    created_at REAL NOT NULL DEFAULT (strftime('%s', 'now'))
);
"""

# Indexes are applied after the column migrations below, not with the tables.
# On an existing database CREATE TABLE IF NOT EXISTS is a no-op, so an index
# naming a newly added column would be created before ALTER TABLE adds it.
INDEXES_SQL = """
CREATE INDEX IF NOT EXISTS idx_credits_owner ON credits(owner_user_id);
CREATE INDEX IF NOT EXISTS idx_credits_status ON credits(status);
CREATE INDEX IF NOT EXISTS idx_credits_device ON credits(device_id);
CREATE INDEX IF NOT EXISTS idx_credits_reserved ON credits(reserved_by, reserved_at);
CREATE INDEX IF NOT EXISTS idx_credits_on_chain ON credits(on_chain_id);
CREATE INDEX IF NOT EXISTS idx_readings_owner ON generation_readings(owner_user_id);
CREATE INDEX IF NOT EXISTS idx_readings_unconsumed ON generation_readings(consumed_by_credit_id);
CREATE INDEX IF NOT EXISTS idx_readings_attested ON generation_readings(device_id, sequence);
CREATE INDEX IF NOT EXISTS idx_enrollments_code ON device_enrollments(code);
CREATE INDEX IF NOT EXISTS idx_enrollments_owner ON device_enrollments(owner_user_id);
CREATE INDEX IF NOT EXISTS idx_device_requests_status ON device_requests(status);
CREATE INDEX IF NOT EXISTS idx_device_requests_user ON device_requests(requested_by);
CREATE INDEX IF NOT EXISTS idx_audit_admin ON audit_log(admin_user_id);
CREATE INDEX IF NOT EXISTS idx_transactions_buyer ON marketplace_transactions(buyer_user_id);
CREATE INDEX IF NOT EXISTS idx_readings_device_open
    ON generation_readings(device_id, consumed_by_credit_id);
CREATE INDEX IF NOT EXISTS idx_allocations_credit ON credit_allocations(credit_row_id);
CREATE INDEX IF NOT EXISTS idx_allocations_reading ON credit_allocations(reading_row_id);
CREATE INDEX IF NOT EXISTS idx_device_events_device ON device_events(device_id);
"""

# Columns added after the initial release. SQLite has no "ADD COLUMN IF NOT
# EXISTS", so existing databases are upgraded by inspecting the table first.
MIGRATIONS = {
    "devices": {
        "public_key": "TEXT",
        "last_sequence": "INTEGER NOT NULL DEFAULT 0",
        "verified": "INTEGER NOT NULL DEFAULT 0",
        "verified_at": "REAL",
        "verified_by": "INTEGER",
        "enrolled_via": "TEXT",
        "rated_capacity_kw": "REAL",
        "latitude": "REAL",
        "longitude": "REAL",
        "last_reading_at": "TEXT",
        "last_meter_wh": "INTEGER",
        "tamper_count": "INTEGER NOT NULL DEFAULT 0",
    },
    "generation_readings": {
        "device_signature": "TEXT",
        "signed_message": "TEXT",
        "sequence": "INTEGER",
        "message_version": "TEXT",
        "meter_wh": "INTEGER",
        "tamper_count": "INTEGER",
        "emission_factor": "REAL",
        "anomaly_flags": "TEXT",
        "allocated_kg": "REAL NOT NULL DEFAULT 0",
    },
    "device_requests": {
        "public_key": "TEXT",
        "rated_capacity_kw": "REAL",
        "latitude": "REAL",
        "longitude": "REAL",
    },
    "device_enrollments": {
        "rated_capacity_kw": "REAL",
        "latitude": "REAL",
        "longitude": "REAL",
    },
    "users": {
        # Set when an account is closed. The row is kept and anonymised rather
        # than deleted, because credits, transactions, and audit entries
        # reference it and the ledger has to stay readable.
        "deleted_at": "REAL",
    },
    "credits": {
        "on_chain_id": "INTEGER",
        "ipfs_hash": "TEXT",
        "tx_hash": "TEXT",
        "minted_at": "REAL",
        "retired_at": "REAL",
        "retire_tx_hash": "TEXT",
        "certificate": "TEXT",
        "review_hold": "TEXT",
        "mint_claim": "TEXT",
        "mint_claimed_at": "REAL",
        "mint_tx_hash": "TEXT",
        "retirement_beneficiary": "TEXT",
        "retirement_purpose": "TEXT",
    },
}

# Data fixes that accompany the column migrations. Each is idempotent.
DATA_MIGRATIONS = (
    # Readings consumed under the old whole-reading model were fully assigned
    # to their credit; record that in the allocation column.
    """UPDATE generation_readings SET allocated_kg = co2_avoided_kg
       WHERE consumed_by_credit_id IS NOT NULL AND allocated_kg = 0""",
)


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

        for statement in DATA_MIGRATIONS:
            await raw_db.execute(statement)

        # Only now that every column exists can the indexes reference them.
        await raw_db.executescript(INDEXES_SQL)
        await raw_db.commit()


# ── Credit issuance ────────────────────────────────────────────────────────

# CO2 is accounted in kilograms as floats. Anything below a microgram is
# rounding residue, not carbon.
_KG_EPSILON = 1e-6

# The certificate schema version. Bumped whenever its shape changes, so an
# auditor knows which fields to expect.
CERTIFICATE_SCHEMA = "ctn-certificate/v2"


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


def _content_hash(value: dict) -> str:
    """SHA-256 of a document's canonical JSON form."""
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def canonical_json(value) -> str:
    """One byte-exact serialisation, so a hash can be recomputed anywhere."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


async def _next_credit_id() -> int:
    """
    Allocate the next public credit number.

    Derived from the current maximum rather than a row count, so deleting a
    credit cannot produce an id that collides with an existing one.
    """
    row = await database.fetch_one("SELECT COALESCE(MAX(credit_id), 0) AS max_id FROM credits")
    return (row["max_id"] if row else 0) + 1


async def _existing_fingerprints(fingerprints: list[str]) -> set[str]:
    """Which of these reading ids are already stored. One indexed lookup per chunk."""
    found: set[str] = set()
    for start in range(0, len(fingerprints), 500):
        chunk = fingerprints[start:start + 500]
        placeholders = ", ".join(f":f{i}" for i in range(len(chunk)))
        rows = await database.fetch_all(
            query=f"SELECT reading_id FROM generation_readings WHERE reading_id IN ({placeholders})",
            values={f"f{i}": value for i, value in enumerate(chunk)},
        )
        found.update(row["reading_id"] for row in rows)
    return found


async def _insert_readings(readings: list[dict], owner_user_id: int) -> int:
    """Insert readings that aren't already stored. Returns the number added."""
    fingerprints = [_reading_fingerprint(r) for r in readings]
    existing = await _existing_fingerprints(fingerprints)

    inserted = 0
    for reading, fingerprint in zip(readings, fingerprints):
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
        flags = reading.get("anomaly_flags")

        await database.execute(
            query="""INSERT INTO generation_readings
                (reading_id, device_id, owner_user_id, total_kwh, co2_avoided_kg,
                 timestamp, methodology, standard, location, signature,
                 device_signature, signed_message, sequence, message_version,
                 meter_wh, tamper_count, emission_factor, anomaly_flags)
                VALUES (:reading_id, :device_id, :owner_user_id, :total_kwh, :co2_avoided_kg,
                        :timestamp, :methodology, :standard, :location, :signature,
                        :device_signature, :signed_message, :sequence, :message_version,
                        :meter_wh, :tamper_count, :emission_factor, :anomaly_flags)""",
            values={
                "reading_id": fingerprint,
                "owner_user_id": reading.get("owner_user_id", owner_user_id),
                # Server-side content hash: detects corruption at rest, proves
                # nothing about origin.
                "signature": hashlib.sha256(
                    json.dumps(canonical, sort_keys=True).encode("utf-8")
                ).hexdigest(),
                # Present only for readings a device signed; CSV rows carry none,
                # which is what distinguishes attested data from asserted data.
                "device_signature": reading.get("device_signature"),
                "signed_message": reading.get("signed_message"),
                "sequence": reading.get("sequence"),
                "message_version": reading.get("message_version"),
                "meter_wh": reading.get("meter_wh"),
                "tamper_count": reading.get("tamper_count"),
                "emission_factor": reading.get(
                    "emission_factor", config.EMISSION_FACTOR_KG_PER_KWH
                ),
                "anomaly_flags": json.dumps(flags) if flags else None,
                **canonical,
            },
        )
        existing.add(fingerprint)
        inserted += 1

    return inserted


async def _device_record(device_id: str) -> Optional[dict]:
    row = await database.fetch_one(
        query="""SELECT device_id, public_key, verified, latitude, longitude,
                        rated_capacity_kw, location
                 FROM devices WHERE device_id = :device_id""",
        values={"device_id": device_id},
    )
    return dict(row) if row else None


def _device_is_verified(device: Optional[dict]) -> bool:
    """
    Whether an operator has confirmed this device's installation.

    Attestation proves a reading came from a particular device and was not
    altered. It cannot prove the device is measuring a real solar array — a
    signed reading from a bench-top ESP32 verifies perfectly. Confirming the
    installation is a human step, and credits stay unsellable until it happens.
    """
    if config.TRUST_SELF_ENROLLED_DEVICES:
        return True
    return bool(device and device["verified"])


def _evidence_entry(row: dict, kg: float, kwh: float) -> dict:
    """
    One reading's contribution to a certificate, with everything needed to
    check it: the signed text and signature for attested readings, or an
    explicit statement that the reading was imported and carries none.
    """
    entry = {
        "reading_id": row["reading_id"],
        "timestamp": row["timestamp"],
        "reading_kwh": round(row["total_kwh"], 6),
        "allocated_kwh": round(kwh, 6),
        "allocated_kg": round(kg, 6),
        "emission_factor": row["emission_factor"],
    }
    if row["device_signature"]:
        entry["attestation"] = {
            "message_version": row["message_version"] or "CTN-READING-V1",
            "signed_message": row["signed_message"],
            "device_signature": row["device_signature"],
        }
    else:
        entry["attestation"] = None
        entry["provenance"] = "imported"
    if row["anomaly_flags"]:
        entry["anomaly_flags"] = json.loads(row["anomaly_flags"])
    return entry


def build_certificate(
    credit_id: int,
    device: Optional[dict],
    device_id: str,
    owner_user_id: int,
    bucket: list[tuple[dict, float, float]],
    review_hold: Optional[str],
) -> dict:
    """
    The evidence document for one credit.

    Self-contained: a third party holding only this document can recover each
    attested reading's signer, compare it with the device key recorded here,
    and re-add the allocations to confirm they total one credit's worth of CO2
    under the stated factor — without calling this API at all.
    """
    readings = [_evidence_entry(row, kg, kwh) for row, kg, kwh in bucket]
    return {
        "schema": CERTIFICATE_SCHEMA,
        "credit_id": credit_id,
        "co2_avoided_kg": config.KG_CO2_PER_CREDIT,
        "energy_kwh": round(sum(kwh for _, _, kwh in bucket), 6),
        "period_start": bucket[0][0]["timestamp"],
        "period_end": bucket[-1][0]["timestamp"],
        "methodology": config.methodology_record(),
        "device": {
            "device_id": device_id,
            "public_key": device["public_key"] if device else None,
            "installation_confirmed": _device_is_verified(device),
            "location": device["location"] if device else None,
            "latitude": device["latitude"] if device else None,
            "longitude": device["longitude"] if device else None,
            "rated_capacity_kw": device["rated_capacity_kw"] if device else None,
        },
        "owner_user_id": owner_user_id,
        "readings_attested": sum(1 for r in readings if r["attestation"]),
        "readings_imported": sum(1 for r in readings if not r["attestation"]),
        "review_hold": review_hold,
        "verification": (
            "For each reading with an attestation, recover the EIP-191 signer of "
            "signed_message from device_signature and compare it to device.public_key. "
            "Sum allocated_kg across readings; it equals co2_avoided_kg."
        ),
        "readings": readings,
    }


async def _issue_credit(
    device_id: str,
    owner_user_id: int,
    device: Optional[dict],
    bucket: list[tuple[dict, float, float]],
) -> int:
    """
    Create one credit from a full bucket of allocations and record them.

    A credit is held as pending when its device is unconfirmed, or when any
    contributing reading was flagged by screening. The latter is recorded as a
    review hold so confirming the device does not release it unseen.
    """
    credit_id = await _next_credit_id()
    flagged = [row["reading_id"] for row, _, _ in bucket if row["anomaly_flags"]]
    review_hold = (
        f"{len(flagged)} contributing reading(s) flagged by anomaly screening"
        if flagged else None
    )
    status = "verified" if _device_is_verified(device) and not review_hold else "pending"

    certificate = build_certificate(
        credit_id, device, device_id, owner_user_id, bucket, review_hold
    )
    total_kwh = certificate["energy_kwh"]
    contributing = [
        {"id": row["id"], "reading_id": row["reading_id"], "signature": row["signature"],
         "kg": round(kg, 6), "kwh": round(kwh, 6)}
        for row, kg, kwh in bucket
    ]

    row_id = await database.execute(
        query="""INSERT INTO credits
            (credit_id, device_id, owner_user_id, total_kwh, co2_avoided_kg,
             period_start, period_end, methodology, standard, location,
             status, contributing_readings, ipfs_hash, certificate, review_hold)
            VALUES (:credit_id, :device_id, :owner_user_id, :total_kwh, :co2_avoided_kg,
                    :period_start, :period_end, :methodology, :standard, :location,
                    :status, :contributing_readings, :ipfs_hash, :certificate, :review_hold)""",
        values={
            "credit_id": credit_id,
            "device_id": device_id,
            "owner_user_id": owner_user_id,
            "total_kwh": total_kwh,
            "co2_avoided_kg": config.KG_CO2_PER_CREDIT,
            "period_start": certificate["period_start"],
            "period_end": certificate["period_end"],
            "methodology": config.METHODOLOGY,
            "standard": config.STANDARD,
            "location": bucket[-1][0]["location"] or "India",
            "status": status,
            "contributing_readings": json.dumps(contributing),
            # Pinning is a network call and must not happen inside the ingest
            # transaction; pin_unpinned_certificates() does it afterwards.
            "ipfs_hash": f"local-{_content_hash(certificate)}",
            "certificate": canonical_json(certificate),
            "review_hold": review_hold,
        },
    )

    for row, kg, kwh in bucket:
        await database.execute(
            query="""INSERT INTO credit_allocations (credit_row_id, reading_row_id, kwh, kg)
                     VALUES (:credit, :reading, :kwh, :kg)""",
            values={"credit": row_id, "reading": row["id"], "kwh": kwh, "kg": kg},
        )
        row["allocated_kg"] += kg
        fully_allocated = row["allocated_kg"] >= row["co2_avoided_kg"] - _KG_EPSILON
        await database.execute(
            query="""UPDATE generation_readings
                     SET allocated_kg = :allocated,
                         consumed_by_credit_id = CASE WHEN :full THEN :credit
                                                      ELSE consumed_by_credit_id END
                     WHERE id = :id""",
            values={
                "allocated": row["allocated_kg"],
                "full": 1 if fully_allocated else 0,
                "credit": row_id,
                "id": row["id"],
            },
        )

    return row_id


async def _issue_for_device(device_id: str, owner_user_id: int) -> int:
    """
    Walk a device's unallocated CO2 in time order and cut whole credits from it.

    A reading's CO2 may straddle two credits: the part that completes one is
    allocated to it and the rest opens the next. Nothing is rounded away and
    nothing is held only in memory, so the remainder survives between ingests
    and every credit's allocations add up to exactly one credit's worth.
    """
    rows = [
        dict(r) for r in await database.fetch_all(
            query="""SELECT id, reading_id, device_id, owner_user_id, total_kwh,
                            co2_avoided_kg, allocated_kg, timestamp, location, signature,
                            device_signature, signed_message, message_version,
                            emission_factor, anomaly_flags
                     FROM generation_readings
                     WHERE device_id = :device_id AND consumed_by_credit_id IS NULL
                     ORDER BY timestamp ASC, id ASC""",
            values={"device_id": device_id},
        )
    ]
    if not rows:
        return 0

    device = await _device_record(device_id)
    issued = 0
    bucket: list[tuple[dict, float, float]] = []
    needed = config.KG_CO2_PER_CREDIT

    for row in rows:
        remaining = row["co2_avoided_kg"] - row["allocated_kg"]
        kwh_per_kg = row["total_kwh"] / row["co2_avoided_kg"] if row["co2_avoided_kg"] > 0 else 0.0

        while True:
            take = min(max(remaining, 0.0), needed)
            bucket.append((row, take, take * kwh_per_kg))
            remaining -= take
            needed -= take

            if needed > _KG_EPSILON:
                break

            await _issue_credit(
                device_id, row["owner_user_id"] or owner_user_id, device, bucket
            )
            issued += 1
            bucket, needed = [], config.KG_CO2_PER_CREDIT
            if remaining <= _KG_EPSILON:
                break

    return issued


async def process_raw_readings(
    readings: list[dict],
    owner_user_id: int,
) -> tuple[int, int]:
    """
    Ingest readings and aggregate them into whole-tonne credits.

    Returns (readings_inserted, credits_issued). Both steps are idempotent:
    readings are keyed by fingerprint and only unallocated CO2 contributes to a
    new credit, so leftover CO2 carries forward to the next ingestion. Only the
    devices named in this batch are re-examined.
    """
    inserted = await _insert_readings(readings, owner_user_id)

    credits_issued = 0
    for device_id in sorted({r.get("device_id") for r in readings if r.get("device_id")}):
        credits_issued += await _issue_for_device(device_id, owner_user_id)

    return inserted, credits_issued


async def pin_unpinned_certificates(limit: int = 20) -> int:
    """
    Pin stored certificates that were only hashed locally at issuance.

    Runs outside any transaction. The stored certificate text is pinned byte
    for byte, so the CID always addresses the document the credit was issued
    with. Returns how many were pinned; failures leave the local hash in place
    for the next attempt.
    """
    import ipfs_utils

    if not ipfs_utils.pinning_configured():
        return 0

    rows = await database.fetch_all(
        query="""SELECT credit_id, certificate FROM credits
                 WHERE ipfs_hash LIKE 'local-%' AND certificate IS NOT NULL
                   AND on_chain_id IS NULL
                 ORDER BY credit_id LIMIT :limit""",
        values={"limit": limit},
    )

    pinned = 0
    for row in rows:
        try:
            cid = await asyncio.to_thread(
                ipfs_utils.pin_certificate, json.loads(row["certificate"]), row["credit_id"]
            )
        except ipfs_utils.IPFSUploadError as exc:
            print(f"⚠ {exc}")
            break
        await db_execute_with_retry(
            query="UPDATE credits SET ipfs_hash = :cid WHERE credit_id = :credit_id",
            values={"cid": cid, "credit_id": row["credit_id"]},
        )
        pinned += 1
    return pinned


async def record_device_event(device_id: str, kind: str, detail: str) -> None:
    await database.execute(
        query="INSERT INTO device_events (device_id, kind, detail) VALUES (:d, :k, :detail)",
        values={"d": device_id, "k": kind, "detail": detail},
    )


# ── Seeding ────────────────────────────────────────────────────────────────

async def _seed_user(email: str, password: str, role: str, wallet_address: str = None) -> int:
    """Create the account if absent. Returns its id either way."""
    existing = await database.fetch_one(
        query="SELECT id FROM users WHERE email = :email AND deleted_at IS NULL",
        values={"email": email},
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

    # Earlier builds seeded a placeholder that only looked like an address, and
    # later the contract's own address. The first reverts any mint; the second
    # mints into a contract that can never move or retire what it holds.
    #
    # The wildcard is bound as a parameter rather than written into the SQL:
    # the query compiler applies %-formatting to the statement text, so a
    # literal % in a LIKE pattern raises at execution time.
    await database.execute(
        query="""UPDATE users SET wallet_address = :addr
                 WHERE email = :email
                   AND (wallet_address LIKE :placeholder
                        OR lower(wallet_address) = lower(:contract))""",
        values={
            "addr": config.DEMO_INSTALLER_WALLET,
            "email": config.DEMO_INSTALLER_EMAIL,
            "placeholder": "0xDemo%",
            "contract": config.CONTRACT_ADDRESS,
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
