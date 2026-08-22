"""Authentication, session handling, and role enforcement."""

import pytest

import config
from tests.conftest import auth


async def test_signup_creates_session(app_client, make_user):
    token, user = await make_user("buyer")
    assert token
    assert user["role"] == "buyer"
    assert user["wallet_address"] is None


async def test_signup_rejects_short_password(app_client):
    response = await app_client.post(
        "/api/auth/signup",
        json={"email": "short@test.local", "password": "abc", "role": "buyer"},
    )
    assert response.status_code == 422


async def test_signup_rejects_invalid_email(app_client):
    response = await app_client.post(
        "/api/auth/signup",
        json={"email": "not-an-email", "password": "test-password-123", "role": "buyer"},
    )
    assert response.status_code == 422


async def test_admin_role_cannot_be_self_registered(app_client):
    response = await app_client.post(
        "/api/auth/signup",
        json={"email": "wannabe@test.local", "password": "test-password-123", "role": "admin"},
    )
    assert response.status_code == 422
    assert "admin" in response.text.lower()


async def test_duplicate_email_is_rejected(app_client, make_user):
    _, user = await make_user("buyer")
    response = await app_client.post(
        "/api/auth/signup",
        json={"email": user["email"], "password": "test-password-123", "role": "buyer"},
    )
    assert response.status_code == 409


async def test_login_succeeds_and_sets_cookie(app_client):
    response = await app_client.post(
        "/api/auth/login",
        json={"email": config.ADMIN_EMAIL, "password": config.ADMIN_PASSWORD},
    )
    assert response.status_code == 200
    assert config.COOKIE_NAME in response.cookies
    assert response.json()["user"]["role"] == "admin"


async def test_login_with_wrong_password_is_rejected(app_client):
    response = await app_client.post(
        "/api/auth/login",
        json={"email": config.ADMIN_EMAIL, "password": "definitely-wrong"},
    )
    assert response.status_code == 401


async def test_login_failures_do_not_reveal_whether_the_account_exists(app_client):
    """An attacker must not be able to enumerate registered addresses."""
    unknown = await app_client.post(
        "/api/auth/login",
        json={"email": "nobody@test.local", "password": "whatever-123"},
    )
    known = await app_client.post(
        "/api/auth/login",
        json={"email": config.ADMIN_EMAIL, "password": "definitely-wrong"},
    )
    assert unknown.status_code == known.status_code == 401
    assert unknown.json()["detail"] == known.json()["detail"]


async def test_email_is_normalised(app_client, make_user):
    _, user = await make_user("buyer")
    response = await app_client.post(
        "/api/auth/login",
        json={"email": f"  {user['email'].upper()}  ", "password": "test-password-123"},
    )
    assert response.status_code == 200


async def test_me_requires_authentication(app_client):
    assert (await app_client.get("/api/auth/me")).status_code == 401


async def test_bearer_token_is_accepted_without_a_cookie(app_client, admin_token):
    """Cross-origin deployments fall back to the header when cookies are blocked."""
    response = await app_client.get("/api/auth/me", headers=auth(admin_token))
    assert response.status_code == 200
    assert response.json()["user"]["email"] == config.ADMIN_EMAIL


async def test_tampered_token_is_rejected(app_client, admin_token):
    response = await app_client.get("/api/auth/me", headers=auth(admin_token + "x"))
    assert response.status_code == 401


async def test_token_signed_with_another_key_is_rejected(app_client):
    from datetime import datetime, timedelta, timezone

    from jose import jwt

    forged = jwt.encode(
        {
            "sub": "1",
            "email": config.ADMIN_EMAIL,
            "role": "admin",
            "exp": datetime.now(timezone.utc) + timedelta(days=1),
        },
        "attacker-supplied-secret",
        algorithm=config.JWT_ALGORITHM,
    )
    assert (await app_client.get("/api/auth/me", headers=auth(forged))).status_code == 401


async def test_expired_token_is_rejected(app_client):
    from datetime import datetime, timedelta, timezone

    from jose import jwt

    import auth as auth_module

    expired = jwt.encode(
        {
            "sub": "1",
            "email": config.ADMIN_EMAIL,
            "role": "admin",
            "exp": datetime.now(timezone.utc) - timedelta(days=1),
        },
        auth_module.SECRET_KEY,
        algorithm=config.JWT_ALGORITHM,
    )
    assert (await app_client.get("/api/auth/me", headers=auth(expired))).status_code == 401


@pytest.mark.parametrize(
    "path,method",
    [
        ("/api/admin/overview", "get"),
        ("/api/installer/dashboard", "get"),
        ("/api/marketplace/my-purchases", "get"),
    ],
)
async def test_protected_routes_reject_anonymous_callers(app_client, path, method):
    response = await getattr(app_client, method)(path)
    assert response.status_code == 401


async def test_roles_are_enforced_across_route_groups(app_client, make_user, admin_token):
    buyer_token, _ = await make_user("buyer")
    installer_token, _ = await make_user("installer")

    # Each role is refused the other two role-scoped areas.
    assert (await app_client.get("/api/admin/overview", headers=auth(buyer_token))).status_code == 403
    assert (await app_client.get("/api/installer/dashboard", headers=auth(buyer_token))).status_code == 403
    assert (await app_client.get("/api/admin/overview", headers=auth(installer_token))).status_code == 403
    assert (await app_client.get("/api/marketplace/my-purchases", headers=auth(installer_token))).status_code == 403
    assert (await app_client.get("/api/installer/dashboard", headers=auth(admin_token))).status_code == 403


async def test_logout_clears_the_cookie(app_client, admin_token):
    response = await app_client.post("/api/auth/logout", headers=auth(admin_token))
    assert response.status_code == 200
    assert response.cookies.get(config.COOKIE_NAME) in (None, "")


async def test_wallet_nonce_requires_a_session(app_client):
    assert (await app_client.post("/api/auth/nonce")).status_code == 401


async def test_wallet_link_rejects_an_unsigned_nonce(app_client, make_user):
    token, _ = await make_user("installer")
    nonce = (await app_client.post("/api/auth/nonce", headers=auth(token))).json()["nonce"]

    response = await app_client.post(
        "/api/auth/link-wallet",
        headers=auth(token),
        json={
            "wallet_address": "0x" + "a" * 40,
            "nonce": nonce,
            "signature": "0x" + "0" * 130,
        },
    )
    assert response.status_code == 400


async def test_wallet_link_verifies_a_real_signature(app_client, make_user):
    """The happy path: a genuine EIP-191 signature links the address."""
    from eth_account import Account
    from eth_account.messages import encode_defunct

    token, _ = await make_user("installer")
    account = Account.create()

    nonce = (await app_client.post("/api/auth/nonce", headers=auth(token))).json()["nonce"]
    signature = Account.sign_message(encode_defunct(text=nonce), account.key).signature.hex()
    if not signature.startswith("0x"):
        signature = "0x" + signature

    response = await app_client.post(
        "/api/auth/link-wallet",
        headers=auth(token),
        json={"wallet_address": account.address, "nonce": nonce, "signature": signature},
    )
    assert response.status_code == 200, response.text
    assert response.json()["wallet_address"].lower() == account.address.lower()


async def test_a_nonce_cannot_be_replayed(app_client, make_user):
    from eth_account import Account
    from eth_account.messages import encode_defunct

    token, _ = await make_user("installer")
    account = Account.create()

    nonce = (await app_client.post("/api/auth/nonce", headers=auth(token))).json()["nonce"]
    signature = Account.sign_message(encode_defunct(text=nonce), account.key).signature.hex()
    if not signature.startswith("0x"):
        signature = "0x" + signature
    payload = {"wallet_address": account.address, "nonce": nonce, "signature": signature}

    assert (await app_client.post("/api/auth/link-wallet", headers=auth(token), json=payload)).status_code == 200
    replay = await app_client.post("/api/auth/link-wallet", headers=auth(token), json=payload)
    assert replay.status_code == 400


async def test_a_wallet_cannot_be_linked_to_two_accounts(app_client, make_user):
    from eth_account import Account
    from eth_account.messages import encode_defunct

    account = Account.create()

    async def link(token):
        nonce = (await app_client.post("/api/auth/nonce", headers=auth(token))).json()["nonce"]
        signature = Account.sign_message(encode_defunct(text=nonce), account.key).signature.hex()
        if not signature.startswith("0x"):
            signature = "0x" + signature
        return await app_client.post(
            "/api/auth/link-wallet",
            headers=auth(token),
            json={"wallet_address": account.address, "nonce": nonce, "signature": signature},
        )

    first_token, _ = await make_user("installer")
    second_token, _ = await make_user("installer")

    assert (await link(first_token)).status_code == 200
    assert (await link(second_token)).status_code == 409
