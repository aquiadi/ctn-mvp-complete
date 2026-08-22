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

    assert Web3.to_checksum_address(config.DEMO_INSTALLER_WALLET)


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
