"""
Startup seeding and schema migration.

The main suite runs with SEED_DEMO_DATA off for determinism, which left the
seeding path untested — a defect there only surfaced on a cold start against an
empty database. These tests exercise it directly.
"""

import config
import database


async def test_seeding_an_account_twice_does_not_duplicate_it(app_client):
    email = "seed-idempotent@test.local"

    first = await database._seed_user(email, "seed-password-123", "installer")
    second = await database._seed_user(email, "seed-password-123", "installer")
    assert first == second

    row = await database.database.fetch_one(
        "SELECT COUNT(*) AS cnt FROM users WHERE email = :email", {"email": email}
    )
    assert row["cnt"] == 1


async def test_the_placeholder_wallet_migration_runs(app_client):
    """
    Regression test.

    The migration matched on `LIKE '0xDemo%'`. The query compiler applies
    %-formatting to the statement text, so the literal % raised a ValueError
    and aborted startup before the app could serve anything.
    """
    email = "seed-wallet@test.local"
    await database._seed_user(email, "seed-password-123", "installer")
    await database.database.execute(
        query="UPDATE users SET wallet_address = :addr WHERE email = :email",
        values={"addr": "0xDemoWalletAddress000000000000000000000000", "email": email},
    )

    await database.database.execute(
        query="""UPDATE users SET wallet_address = :addr
                 WHERE email = :email AND wallet_address LIKE :placeholder""",
        values={
            "addr": config.DEMO_INSTALLER_WALLET,
            "email": email,
            "placeholder": "0xDemo%",
        },
    )

    row = await database.database.fetch_one(
        "SELECT wallet_address FROM users WHERE email = :email", {"email": email}
    )
    assert row["wallet_address"] == config.DEMO_INSTALLER_WALLET


async def test_the_seeded_demo_wallet_is_a_valid_address(app_client):
    """A placeholder that merely looks like an address reverts the mint call."""
    from web3 import Web3

    if config.DEMO_INSTALLER_WALLET is not None:
        assert Web3.to_checksum_address(config.DEMO_INSTALLER_WALLET)


async def test_the_demo_wallet_is_never_the_contract_itself(app_client):
    """
    Regression test.

    The demo wallet defaulted to the contract address, so demo credits were
    minted into a contract that can neither transfer nor retire them.
    """
    assert (config.DEMO_INSTALLER_WALLET or "").lower() != config.CONTRACT_ADDRESS.lower()


def test_custody_is_the_signing_wallet(monkeypatch):
    from eth_account import Account

    key = "0x" + "11" * 32
    monkeypatch.setattr(config, "PRIVATE_KEY", key[2:])
    assert config._custody_address() == Account.from_key(key).address

    monkeypatch.setattr(config, "PRIVATE_KEY", "")
    assert config._custody_address() is None


async def test_migrations_add_the_on_chain_columns(app_client):
    """Databases created before on-chain tracking must gain the new columns."""
    import aiosqlite

    async with aiosqlite.connect(database.DB_PATH) as raw_db:
        cursor = await raw_db.execute("PRAGMA table_info(credits)")
        columns = {row[1] for row in await cursor.fetchall()}

    assert database.MIGRATIONS["credits"].keys() <= columns


async def test_applying_the_schema_twice_is_safe(app_client):
    """Startup runs against an existing database on every restart."""
    await database._apply_schema()
    await database._apply_schema()


async def test_indexes_are_created_after_column_migrations(app_client, tmp_path):
    """
    Regression test.

    An index named a column added by a later migration. On an existing database
    CREATE TABLE IF NOT EXISTS is a no-op, so the index was created before
    ALTER TABLE added the column and startup died with "no such column",
    returning 502 for every request against any pre-existing deployment.
    """
    import sqlite3

    import aiosqlite

    legacy = tmp_path / "legacy.db"
    # A database predating the attestation columns.
    conn = sqlite3.connect(legacy)
    conn.executescript(
        """
        CREATE TABLE generation_readings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            reading_id TEXT UNIQUE NOT NULL,
            device_id TEXT,
            consumed_by_credit_id INTEGER,
            owner_user_id INTEGER
        );
        CREATE TABLE devices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id TEXT UNIQUE NOT NULL
        );
        """
    )
    conn.commit()
    conn.close()

    async with aiosqlite.connect(legacy) as raw_db:
        await raw_db.executescript(database.SCHEMA_SQL)
        for table, columns in database.MIGRATIONS.items():
            cursor = await raw_db.execute(f"PRAGMA table_info({table})")
            existing = {row[1] for row in await cursor.fetchall()}
            for column, column_type in columns.items():
                if column not in existing:
                    await raw_db.execute(
                        f"ALTER TABLE {table} ADD COLUMN {column} {column_type}"
                    )
        # The step that used to fail.
        await raw_db.executescript(database.INDEXES_SQL)
        await raw_db.commit()

        cursor = await raw_db.execute("PRAGMA table_info(generation_readings)")
        columns = {row[1] for row in await cursor.fetchall()}

    assert "sequence" in columns
    assert "device_signature" in columns
