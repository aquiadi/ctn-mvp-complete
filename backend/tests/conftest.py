"""
Shared test fixtures.

Each test session runs against a throwaway SQLite file with demo seeding and
network-dependent startup work disabled, so the suite is deterministic and
needs no external services.
"""

import os
import sys
import tempfile
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

_TEMP_DB = Path(tempfile.mkdtemp(prefix="ctn-tests-")) / "test.db"

# Configuration is read at import time, so it must be set before the
# application modules are loaded.
os.environ.update(
    {
        "DATABASE_URL": f"sqlite:///{_TEMP_DB}",
        "JWT_SECRET": "test-secret-key-long-enough-for-validation-32+",
        "ADMIN_EMAIL": "admin@test.local",
        "ADMIN_PASSWORD": "admin-test-password",
        "SEED_DEMO_DATA": "false",
        "ENVIRONMENT": "development",
        "COOKIE_SECURE": "false",
        # Generous limits so functional tests aren't throttled; the rate-limit
        # test sets its own.
        "LOGIN_RATE_LIMIT": "1000/minute",
        "SIGNUP_RATE_LIMIT": "1000/minute",
        "PINATA_API_KEY": "",
        "PINATA_SECRET": "",
    }
)

import config  # noqa: E402
import database  # noqa: E402


@pytest.fixture(scope="session")
def anyio_backend():
    return "asyncio"


@pytest.fixture(scope="session")
async def app_client():
    """An HTTP client bound to the application, with the schema initialised."""
    import httpx

    import main

    await database.database.connect()
    await database._apply_schema()
    await database._seed_user(config.ADMIN_EMAIL, config.ADMIN_PASSWORD, "admin")

    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client

    await database.database.disconnect()


@pytest.fixture(autouse=True)
def isolate_session(app_client):
    """
    Drop any cookies left over from a previous test.

    The client is session-scoped for speed, but it also stores Set-Cookie
    responses. Without this, a test that logs in would silently authenticate
    every later request — including the ones asserting a 401.
    """
    app_client.cookies.clear()
    yield
    app_client.cookies.clear()


@pytest.fixture
async def admin_token(app_client):
    response = await app_client.post(
        "/api/auth/login",
        json={"email": config.ADMIN_EMAIL, "password": config.ADMIN_PASSWORD},
    )
    assert response.status_code == 200, response.text
    # Discard the session cookie so tests authenticate only via the token they
    # pass explicitly, and an omitted token really means anonymous.
    app_client.cookies.clear()
    return response.json()["token"]


def auth(token: str) -> dict:
    """Authorization header for a token."""
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
async def make_user(app_client):
    """Factory creating a fresh account and returning (token, user)."""
    counter = {"n": 0}

    async def _make(role: str):
        counter["n"] += 1
        email = f"{role}{counter['n']}-{os.urandom(3).hex()}@test.local"
        response = await app_client.post(
            "/api/auth/signup",
            json={"email": email, "password": "test-password-123", "role": role},
        )
        assert response.status_code == 200, response.text
        app_client.cookies.clear()
        body = response.json()
        return body["token"], body["user"]

    return _make
