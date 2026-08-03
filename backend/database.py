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

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./ctn_v2.db")
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
    reading_id INTEGER UNIQUE NOT NULL,
    device_id TEXT,
    owner_user_id INTEGER REFERENCES users(id),
    total_kwh REAL NOT NULL DEFAULT 0,
    co2_avoided_kg REAL NOT NULL DEFAULT 0,
    timestamp TEXT,
    period_start TEXT,
    period_end TEXT,
    methodology TEXT DEFAULT 'CEA Grid Emission Factor 0.82 kg/kWh',
    standard TEXT DEFAULT 'CTN-SOLAR-V1',
    location TEXT DEFAULT 'India',
    signature TEXT,
    credit_id INTEGER REFERENCES credits(id),
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


async def sync_credits_from_ipfs(owner_user_id):
    """Sync generation_readings from IPFS and accumulate into 1-tonne credits."""
    import requests as req
    import json

    IPFS_URL = "https://ivory-geographical-lungfish-400.mypinata.cloud/ipfs/bafybeifpn7y2r2rsjtvm4hun3dy63jkp5ah7qxfwhk5u6bemkapmis2qku"

    try:
        r = req.get(IPFS_URL, timeout=30)
        data = r.json()
        credits_list = data if isinstance(data, list) else data.get("credits", [])

        from data_utils import parse_discrete_credits
        credits_list = parse_discrete_credits(credits_list)
        
        # 1. Sync generation_readings
        existing_rows = await database.fetch_all("SELECT reading_id FROM generation_readings")
        existing_ids = {row["reading_id"] for row in existing_rows}
        
        inserts = 0
        for c in credits_list:
            cid = c.get("credit_id")
            if cid not in existing_ids:
                await database.execute(
                    query="""INSERT INTO generation_readings 
                        (reading_id, device_id, owner_user_id, total_kwh, co2_avoided_kg,
                         timestamp, period_start, period_end, methodology, standard,
                         location, signature)
                        VALUES (:reading_id, :device_id, :owner_user_id, :total_kwh, :co2_avoided_kg,
                                :timestamp, :period_start, :period_end, :methodology, :standard,
                                :location, :signature)""",
                    values={
                        "reading_id": cid,
                        "device_id": c.get("device_id"),
                        "owner_user_id": owner_user_id,
                        "total_kwh": c.get("total_kwh", 0),
                        "co2_avoided_kg": c.get("co2_avoided_kg", 0),
                        "timestamp": c.get("timestamp"),
                        "period_start": c.get("period_start"),
                        "period_end": c.get("period_end"),
                        "methodology": c.get("methodology", "CEA Grid Emission Factor 0.82 kg/kWh"),
                        "standard": c.get("standard", "CTN-SOLAR-V1"),
                        "location": c.get("location", "India"),
                        "signature": c.get("signature")
                    }
                )
                inserts += 1
                
        # 2. Accumulate into 1-tonne credits
        readings = await database.fetch_all("SELECT reading_id, co2_avoided_kg, total_kwh, timestamp, device_id FROM generation_readings ORDER BY timestamp ASC")
        
        existing_credits_count = dict(await database.fetch_one("SELECT COUNT(*) as cnt FROM credits"))["cnt"]
        
        acc_co2 = 0.0
        acc_kwh = 0.0
        acc_readings = []
        period_start = None
        credits_minted_this_run = 0
        new_credits = 0
        
        for r in readings:
            if period_start is None:
                period_start = r["timestamp"]
                
            acc_co2 += r["co2_avoided_kg"]
            acc_kwh += r["total_kwh"]
            acc_readings.append(r["reading_id"])
            
            while acc_co2 >= 1000.0:
                if credits_minted_this_run >= existing_credits_count:
                    # Mint a new 1-tonne credit
                    new_credit_id = existing_credits_count + new_credits + 1
                    await database.execute(
                        query="""INSERT INTO credits 
                            (credit_id, device_id, owner_user_id, total_kwh, co2_avoided_kg,
                             period_start, period_end, status, contributing_readings)
                            VALUES (:credit_id, :device_id, :owner_user_id, :kwh, 1000,
                                    :p_start, :p_end, 'verified', :readings)""",
                        values={
                            "credit_id": new_credit_id,
                            "device_id": r["device_id"],
                            "owner_user_id": owner_user_id,
                            "kwh": acc_kwh,
                            "p_start": period_start,
                            "p_end": r["timestamp"],
                            "readings": json.dumps(acc_readings)
                        }
                    )
                    new_credits += 1
                
                credits_minted_this_run += 1
                acc_co2 -= 1000.0
                acc_kwh = 0.0
                acc_readings = []
                period_start = r["timestamp"]
                
        print(f"✓ Synced IPFS: {inserts} new readings, accumulated {new_credits} new 1-tonne credits.")

    except Exception as e:
        print(f"⚠ Failed to seed credits from IPFS: {e}")


async def shutdown_db():
    """Disconnect from database."""
    await database.disconnect()
