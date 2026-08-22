"""
Account self-management: viewing a profile, transferring the account to another
email, and closing it.

Closing anonymises the row rather than deleting it, because credits,
marketplace transactions, and audit entries reference the user and the ledger
has to remain readable for the credits to stay verifiable.
"""

import database
from tests.conftest import auth


# ── Profile ────────────────────────────────────────────────────────────────

async def test_profile_returns_the_account(app_client, make_user):
    token, user = await make_user("buyer")
    body = (await app_client.get("/api/auth/profile", headers=auth(token))).json()

    assert body["user"]["email"] == user["email"]
    assert body["user"]["role"] == "buyer"
    assert body["can_delete"] is True
    assert body["delete_blockers"] == []


async def test_profile_requires_authentication(app_client):
    assert (await app_client.get("/api/auth/profile")).status_code == 401


# ── Changing the email ─────────────────────────────────────────────────────

async def test_the_account_can_move_to_a_new_email(app_client, make_user):
    token, user = await make_user("installer")
    new_email = "moved-" + user["email"]

    response = await app_client.post(
        "/api/auth/change-email",
        headers=auth(token),
        json={"email": new_email, "password": "test-password-123"},
    )
    assert response.status_code == 200, response.text

    # The new address signs in; the old one no longer exists.
    assert (await app_client.post("/api/auth/login",
            json={"email": new_email, "password": "test-password-123"})).status_code == 200
    assert (await app_client.post("/api/auth/login",
            json={"email": user["email"], "password": "test-password-123"})).status_code == 401


async def test_changing_the_email_requires_the_password(app_client, make_user):
    """Otherwise a borrowed session could quietly take the account over."""
    token, user = await make_user("installer")
    response = await app_client.post(
        "/api/auth/change-email",
        headers=auth(token),
        json={"email": "attacker@test.local", "password": "not-the-password"},
    )
    assert response.status_code == 401

    row = await database.database.fetch_one(
        "SELECT email FROM users WHERE email = :e", {"e": user["email"]})
    assert row is not None


async def test_the_new_email_cannot_belong_to_someone_else(app_client, make_user):
    token, _ = await make_user("installer")
    _, other = await make_user("buyer")

    response = await app_client.post(
        "/api/auth/change-email",
        headers=auth(token),
        json={"email": other["email"], "password": "test-password-123"},
    )
    assert response.status_code == 409


async def test_a_malformed_new_email_is_rejected(app_client, make_user):
    token, _ = await make_user("buyer")
    response = await app_client.post(
        "/api/auth/change-email",
        headers=auth(token),
        json={"email": "not-an-email", "password": "test-password-123"},
    )
    assert response.status_code == 422


# ── Closing the account ────────────────────────────────────────────────────

async def _close(app_client, token, password="test-password-123", confirm="DELETE"):
    return await app_client.post(
        "/api/auth/delete-account",
        headers=auth(token),
        json={"password": password, "confirm": confirm},
    )


async def test_closing_an_account_ends_the_session_and_login(app_client, make_user):
    token, user = await make_user("buyer")
    assert (await _close(app_client, token)).status_code == 200

    # The old token stops resolving and the credentials stop working.
    assert (await app_client.get("/api/auth/me", headers=auth(token))).status_code == 401
    assert (await app_client.post("/api/auth/login",
            json={"email": user["email"], "password": "test-password-123"})).status_code == 401


async def test_closing_removes_personal_details_but_keeps_the_row(app_client, make_user):
    token, user = await make_user("installer")
    user_id = user["id"]
    await _close(app_client, token)

    row = await database.database.fetch_one(
        "SELECT email, wallet_address, deleted_at FROM users WHERE id = :id", {"id": user_id})
    assert row is not None, "the row must survive so ledger references stay valid"
    assert row["deleted_at"] is not None
    assert row["email"] != user["email"]
    assert row["email"].endswith("@ctn.invalid")
    assert row["wallet_address"] is None


async def test_closing_requires_the_password(app_client, make_user):
    token, _ = await make_user("buyer")
    assert (await _close(app_client, token, password="wrong-password")).status_code == 401
    assert (await app_client.get("/api/auth/me", headers=auth(token))).status_code == 200


async def test_closing_requires_typing_the_confirmation(app_client, make_user):
    token, _ = await make_user("buyer")
    assert (await _close(app_client, token, confirm="yes")).status_code == 422


async def test_the_freed_email_can_be_registered_again(app_client, make_user):
    token, user = await make_user("buyer")
    await _close(app_client, token)

    response = await app_client.post(
        "/api/auth/signup",
        json={"email": user["email"], "password": "a-new-password-123", "role": "buyer"},
    )
    assert response.status_code == 200, response.text


async def test_an_installer_with_credits_on_sale_cannot_close(app_client, make_user):
    """Closing mid-sale would strand a buyer partway through a purchase."""
    token, user = await make_user("installer")
    await database.database.execute(
        query="""INSERT INTO credits (credit_id, device_id, owner_user_id,
                                      co2_avoided_kg, status, listed_at)
                 VALUES (:cid, 'CLOSE-DEV', :owner, 1000, 'listed', 1)""",
        values={"cid": 800_000 + user["id"], "owner": user["id"]},
    )

    profile = (await app_client.get("/api/auth/profile", headers=auth(token))).json()
    assert profile["can_delete"] is False
    assert profile["delete_blockers"]

    response = await _close(app_client, token)
    assert response.status_code == 409
    assert "listed" in response.json()["detail"]


async def test_the_last_administrator_cannot_close_their_account(app_client, admin_token):
    """Leaving the platform with no administrator would be unrecoverable."""
    profile = (await app_client.get("/api/auth/profile", headers=auth(admin_token))).json()
    assert profile["can_delete"] is False
    assert "only administrator" in " ".join(profile["delete_blockers"])


async def test_a_closed_installer_no_longer_appears_to_admins(
    app_client, admin_token, make_user
):
    token, user = await make_user("installer")
    await _close(app_client, token)

    body = (await app_client.get("/api/admin/installers", headers=auth(admin_token))).json()
    assert user["email"] not in {i["email"] for i in body["installers"]}


# ── Admin buyer visibility ─────────────────────────────────────────────────

async def test_admins_can_see_buyers(app_client, admin_token, make_user):
    _, buyer = await make_user("buyer")
    body = (await app_client.get("/api/admin/buyers", headers=auth(admin_token))).json()

    entry = next(b for b in body["buyers"] if b["email"] == buyer["email"])
    assert entry["purchase_count"] == 0
    assert entry["credits_bought"] == 0
    assert entry["total_spent_inr"] == 0


async def test_buyer_purchase_totals_are_reported(app_client, admin_token, make_user):
    import config

    seller_token, seller = await make_user("installer")
    buyer_token, buyer = await make_user("buyer")

    row_id = await database.database.execute(
        query="""INSERT INTO credits (credit_id, device_id, owner_user_id,
                                      co2_avoided_kg, status, listed_at)
                 VALUES (:cid, 'BUY-DEV', :owner, 1000, 'listed', 1)""",
        values={"cid": 810_000 + seller["id"], "owner": seller["id"]},
    )
    await app_client.post("/api/marketplace/reserve",
                          headers=auth(buyer_token), json={"credit_ids": [row_id]})
    await app_client.post("/api/marketplace/purchase",
                          headers=auth(buyer_token), json={"reservation_ids": [row_id]})

    body = (await app_client.get("/api/admin/buyers", headers=auth(admin_token))).json()
    entry = next(b for b in body["buyers"] if b["email"] == buyer["email"])
    assert entry["purchase_count"] == 1
    assert entry["credits_bought"] == 1
    assert entry["total_spent_inr"] == config.CREDIT_VALUE_INR


async def test_the_buyer_list_is_admin_only(app_client, make_user):
    token, _ = await make_user("installer")
    assert (await app_client.get("/api/admin/buyers", headers=auth(token))).status_code == 403
