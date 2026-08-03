"""
CTN Database — SQLite setup, schema, retry wrapper, and seeding.
"""

import os
import asyncio
import json
import hashlib
import time
from databases import Database
from passlib.context import CryptContext

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./ctn_v3.db")
if "sqlite" in DATABASE_URL and ("ctn.db" in DATABASE_URL or "ctn_v2.db" in DATABASE_URL):
    DATABASE_URL = DATABASE_URL.replace("ctn.db", "ctn_v3.db").replace("ctn_v2.db", "ctn_v3.db")
DB_PATH = DATABASE_URL.replace("sqlite:///", "")

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
    methodology TEXT DEFAULT 'CEA Grid Emission Factor 0.82 kg/kWh',
    standard TEXT DEFAULT 'CTN-SOLAR-V1',
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
    methodology TEXT DEFAULT 'CEA Grid Emission Factor 0.82 kg/kWh',
    standard TEXT DEFAULT 'CTN-SOLAR-V1',
    location TEXT DEFAULT 'India',
    contributing_readings TEXT, -- JSON array of reading_ids
    status TEXT NOT NULL DEFAULT 'verified'
        CHECK (status IN ('pending', 'verified', 'listed', 'reserved', 'sold', 'retired')),
    on_chain_id INTEGER,
    ipfs_hash TEXT,
    tx_hash TEXT,
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
CREATE INDEX IF NOT EXISTS idx_audit_admin ON audit_log(admin_user_id);
CREATE INDEX IF NOT EXISTS idx_transactions_buyer ON marketplace_transactions(buyer_user_id);
"""

# ── Retry wrapper for SQLite write contention ──────────────────────────────

async def db_execute_with_retry(query, values=None, retries=3):
    """
    Execute a write query with exponential backoff retry on 'database is locked'.
    Delays: 50ms, 100ms, 200ms.
    """
    delays = [0.05, 0.1, 0.2]
    last_error = None
    for attempt in range(retries + 1):
        try:
            if values is not None:
                return await database.execute(query=query, values=values)
            else:
                return await database.execute(query=query)
        except Exception as e:
            if "database is locked" in str(e).lower() and attempt < retries:
                last_error = e
                await asyncio.sleep(delays[attempt])
            else:
                raise
    raise last_error


async def db_fetch_with_retry(query, values=None, retries=3):
    """Fetch rows with retry on lock."""
    delays = [0.05, 0.1, 0.2]
    last_error = None
    for attempt in range(retries + 1):
        try:
            if values is not None:
                return await database.fetch_all(query=query, values=values)
            else:
                return await database.fetch_all(query=query)
        except Exception as e:
            if "database is locked" in str(e).lower() and attempt < retries:
                last_error = e
                await asyncio.sleep(delays[attempt])
            else:
                raise
    raise last_error


# ── Init and seeding ───────────────────────────────────────────────────────

async def init_db():
    """Create tables and seed initial data if needed."""
    await database.connect()

    # Use raw aiosqlite for DDL — the `databases` library misinterprets
    # the % in strftime('%s','now') defaults as format-string placeholders.
    import aiosqlite
    async with aiosqlite.connect(DB_PATH) as raw_db:
        await raw_db.executescript(SCHEMA_SQL)
        await raw_db.commit()

    # Seed admin account from env vars
    admin_email = os.getenv("ADMIN_EMAIL", "admin@ctn.org")
    admin_password = os.getenv("ADMIN_PASSWORD", "ctn-admin-2024")

    existing_admin = await database.fetch_one(
        query="SELECT id FROM users WHERE email = :email",
        values={"email": admin_email}
    )
    if not existing_admin:
        await database.execute(
            query="""INSERT INTO users (email, password_hash, role) 
                     VALUES (:email, :password_hash, :role)""",
            values={
                "email": admin_email,
                "password_hash": pwd_context.hash(admin_password),
                "role": "admin"
            }
        )
        print(f"✓ Seeded admin account: {admin_email}")

    # Seed demo installer account
    demo_email = "demo@installer.ctn"
    existing_demo = await database.fetch_one(
        query="SELECT id FROM users WHERE email = :email",
        values={"email": demo_email}
    )
    demo_user_id = None
    if not existing_demo:
        demo_user_id = await database.execute(
            query="""INSERT INTO users (email, password_hash, role, wallet_address)
                     VALUES (:email, :password_hash, :role, :wallet_address)""",
            values={
                "email": demo_email,
                "password_hash": pwd_context.hash("demo-installer-2024"),
                "role": "installer",
                "wallet_address": "0xDemoWalletAddress000000000000000000000000"
            }
        )
        print(f"✓ Seeded demo installer: {demo_email}")
    else:
        demo_user_id = existing_demo["id"]

    # Seed demo device
    existing_device = await database.fetch_one(
        query="SELECT id FROM devices WHERE device_id = :device_id",
        values={"device_id": "1BY6WEcLGh8j5v7"}
    )
    if not existing_device:
        await database.execute(
            query="""INSERT INTO devices (device_id, owner_user_id, location)
                     VALUES (:device_id, :owner_user_id, :location)""",
            values={
                "device_id": "1BY6WEcLGh8j5v7",
                "owner_user_id": demo_user_id,
                "location": "Patna, Bihar, India"
            }
        )
        print("✓ Seeded demo device: 1BY6WEcLGh8j5v7")

    # Sync credits from IPFS data (single source of truth)
    await sync_credits_from_ipfs(demo_user_id)


async def process_raw_readings(readings: list[dict], owner_user_id: int) -> int:
    """
    Ingest, sign, and idempotently aggregate raw readings into 1-tonne credits.
    Assumes single owner per ingestion batch — revisit if multi-device batches are needed.
    """
    from ipfs_utils import upload_credit_to_ipfs
    
    inserts = 0
    # 1. Ingestion: Serialize, hash, and insert
    existing_rows = await database.fetch_all("SELECT reading_id FROM generation_readings")
    existing_ids = {row["reading_id"] for row in existing_rows}
    
    for r in readings:
        cid = str(r.get("reading_id", ""))
        if not cid:
            # Fallback for CSV: hash device_id + timestamp to ensure idempotency if no reading_id provided
            cid = hashlib.sha256(f"{r.get('device_id')}_{r.get('timestamp')}".encode('utf-8')).hexdigest()[:16]
            
        if cid not in existing_ids:
            reading_dict = {
                "device_id": r.get("device_id"),
                "timestamp": r.get("timestamp"),
                "total_kwh": r.get("total_kwh", 0),
                "co2_avoided_kg": r.get("co2_avoided_kg", 0),
                "methodology": "CEA Grid Emission Factor 0.82 kg/kWh",
                "standard": "CTN-SOLAR-V1",
                "location": r.get("location", "India")
            }
            serialized = json.dumps(reading_dict, sort_keys=True)
            signature = hashlib.sha256(serialized.encode('utf-8')).hexdigest()
            
            await database.execute(
                query="""INSERT INTO generation_readings 
                    (reading_id, device_id, owner_user_id, total_kwh, co2_avoided_kg,
                     timestamp, methodology, standard, location, signature)
                    VALUES (:reading_id, :device_id, :owner_user_id, :total_kwh, :co2_avoided_kg,
                            :timestamp, :methodology, :standard, :location, :signature)""",
                values={
                    "reading_id": cid,
                    "device_id": reading_dict["device_id"],
                    "owner_user_id": owner_user_id,
                    "total_kwh": reading_dict["total_kwh"],
                    "co2_avoided_kg": reading_dict["co2_avoided_kg"],
                    "timestamp": reading_dict["timestamp"],
                    "methodology": reading_dict["methodology"],
                    "standard": reading_dict["standard"],
                    "location": reading_dict["location"],
                    "signature": signature
                }
            )
            inserts += 1

    # 2. Accumulation: Fetch all unconsumed readings (idempotent remainder tracking)
    unconsumed = await database.fetch_all(
        "SELECT id, reading_id, co2_avoided_kg, total_kwh, timestamp, device_id, signature "
        "FROM generation_readings WHERE consumed_by_credit_id IS NULL ORDER BY timestamp ASC"
    )
    
    new_credits = 0
    
    # We group unconsumed readings by device_id since 1-tonne credits are per-device
    grouped = {}
    for row in unconsumed:
        d_id = row["device_id"]
        if d_id not in grouped:
            grouped[d_id] = []
        grouped[d_id].append(row)
        
    for device_id, group in grouped.items():
        acc_co2 = 0.0
        acc_kwh = 0.0
        acc_readings = []
        period_start = None
        
        for r in group:
            if period_start is None:
                period_start = r["timestamp"]
                
            acc_co2 += r["co2_avoided_kg"]
            acc_kwh += r["total_kwh"]
            acc_readings.append({"reading_id": r["reading_id"], "signature": r["signature"], "id": r["id"]})
            
            while acc_co2 >= 1000.0:
                # Mint a new 1-tonne credit
                existing_credits_count = dict(await database.fetch_one("SELECT COUNT(*) as cnt FROM credits"))["cnt"]
                new_credit_id = existing_credits_count + 1
                
                credit_json_payload = {
                    "credit_id": new_credit_id,
                    "device_id": device_id,
                    "owner_user_id": owner_user_id,
                    "total_kwh": acc_kwh,
                    "co2_avoided_kg": 1000.0,
                    "period_start": period_start,
                    "period_end": r["timestamp"],
                    "methodology": "CEA Grid Emission Factor 0.82 kg/kWh",
                    "standard": "CTN-SOLAR-V1",
                    "contributing_readings": acc_readings
                }
                
                # Unconditionally upload IPFS certificate on mint
                ipfs_hash = upload_credit_to_ipfs(credit_json_payload)
                
                # Insert into DB
                db_id = await database.execute(
                    query="""INSERT INTO credits 
                        (credit_id, device_id, owner_user_id, total_kwh, co2_avoided_kg,
                         period_start, period_end, status, contributing_readings, ipfs_hash)
                        VALUES (:credit_id, :device_id, :owner_user_id, :kwh, 1000,
                                :p_start, :p_end, 'verified', :readings, :ipfs_hash)""",
                    values={
                        "credit_id": new_credit_id,
                        "device_id": device_id,
                        "owner_user_id": owner_user_id,
                        "kwh": acc_kwh,
                        "p_start": period_start,
                        "p_end": r["timestamp"],
                        "readings": json.dumps(acc_readings),
                        "ipfs_hash": ipfs_hash
                    }
                )
                new_credits += 1
                
                # Mark these readings as consumed
                placeholders = ", ".join([str(item["id"]) for item in acc_readings])
                await database.execute(f"UPDATE generation_readings SET consumed_by_credit_id = {db_id} WHERE id IN ({placeholders})")
                
                acc_co2 -= 1000.0
                acc_kwh = 0.0
                acc_readings = []
                period_start = r["timestamp"]

    return inserts, new_credits


async def sync_credits_from_ipfs(owner_user_id):
    """Fetch from IPFS and feed into shared processor."""
    import requests as req
    import json

    IPFS_URL = "https://ivory-geographical-lungfish-400.mypinata.cloud/ipfs/bafybeifpn7y2r2rsjtvm4hun3dy63jkp5ah7qxfwhk5u6bemkapmis2qku"

    try:
        r = req.get(IPFS_URL, timeout=30)
        data = r.json()
        credits_list = data if isinstance(data, list) else data.get("credits", [])

        from data_utils import parse_discrete_credits
        credits_list = parse_discrete_credits(credits_list)
        
        inserts, new_credits = await process_raw_readings(credits_list, owner_user_id)
                
        print(f"✓ Synced IPFS: {inserts} new readings, accumulated {new_credits} new 1-tonne credits.")

    except Exception as e:
        print(f"⚠ Failed to seed credits from IPFS: {e}")


async def shutdown_db():
    """Disconnect from database."""
    await database.disconnect()
