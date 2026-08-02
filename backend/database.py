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

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./ctn.db")
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

CREATE TABLE IF NOT EXISTS credits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    credit_id INTEGER NOT NULL,
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

    # Seed credits from IPFS data (if credits table is empty)
    existing_credits = await database.fetch_one(
        query="SELECT COUNT(*) as cnt FROM credits"
    )
    if existing_credits["cnt"] == 0:
        await seed_credits_from_ipfs(demo_user_id)


async def seed_credits_from_ipfs(owner_user_id):
    """Seed credits table from the IPFS-loaded data."""
    import requests as req

    IPFS_URL = "https://ivory-geographical-lungfish-400.mypinata.cloud/ipfs/bafybeifpn7y2r2rsjtvm4hun3dy63jkp5ah7qxfwhk5u6bemkapmis2qku"

    try:
        r = req.get(IPFS_URL, timeout=30)
        data = r.json()
        credits_list = data if isinstance(data, list) else data.get("credits", [])

        # The IPFS data contains cumulative totals. We need the delta for each individual credit.
        credits_list.sort(key=lambda x: x.get("credit_id", 0))
        prev_kwh = 0
        prev_co2 = 0

        for c in credits_list:
            cum_kwh = c.get("total_kwh", 0)
            cum_co2 = c.get("co2_avoided_kg", 0)
            
            delta_kwh = max(0, cum_kwh - prev_kwh)
            delta_co2 = max(0, cum_co2 - prev_co2)
            
            prev_kwh = cum_kwh
            prev_co2 = cum_co2

            await database.execute(
                query="""INSERT INTO credits 
                    (credit_id, device_id, owner_user_id, total_kwh, co2_avoided_kg,
                     timestamp, period_start, period_end, methodology, standard,
                     location, signature, status, contract_version)
                    VALUES (:credit_id, :device_id, :owner_user_id, :total_kwh, :co2_avoided_kg,
                            :timestamp, :period_start, :period_end, :methodology, :standard,
                            :location, :signature, :status, :contract_version)""",
                values={
                    "credit_id": c.get("credit_id"),
                    "device_id": c.get("device_id"),
                    "owner_user_id": owner_user_id,
                    "total_kwh": delta_kwh,
                    "co2_avoided_kg": delta_co2,
                    "timestamp": c.get("timestamp"),
                    "period_start": c.get("period_start"),
                    "period_end": c.get("period_end"),
                    "methodology": c.get("methodology", "CEA Grid Emission Factor 0.82 kg/kWh"),
                    "standard": c.get("standard", "CTN-SOLAR-V1"),
                    "location": c.get("location", "India"),
                    "signature": c.get("signature"),
                    "status": "verified",
                    "contract_version": "old"
                }
            )
        print(f"✓ Seeded {len(credits_list)} credits from IPFS")
    except Exception as e:
        print(f"⚠ Failed to seed credits from IPFS: {e}")


async def shutdown_db():
    """Disconnect from database."""
    await database.disconnect()
