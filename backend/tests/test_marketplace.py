"""Listing, reservation, and purchase — including concurrent contention."""

import asyncio

import pytest

import config
import database
from tests.conftest import auth


async def _seed_listed_credits(owner_id: int, count: int, first_credit_id: int) -> list[int]:
    """Insert `count` credits already in 'listed' status. Returns their row ids."""
    row_ids = []
    for offset in range(count):
        row_id = await database.database.execute(
            query="""INSERT INTO credits
                     (credit_id, device_id, owner_user_id, total_kwh, co2_avoided_kg,
                      status, listed_at, location)
                     VALUES (:credit_id, 'TEST-DEVICE', :owner, 1220, :co2,
                             'listed', 1, 'Test City')""",
            values={
                "credit_id": first_credit_id + offset,
                "owner": owner_id,
                "co2": config.KG_CO2_PER_CREDIT,
            },
        )
        row_ids.append(row_id)
    return row_ids


@pytest.fixture
async def listed_credits(app_client, make_user):
    """A seller with three credits on the marketplace."""
    _, seller = await make_user("installer")
    ids = await _seed_listed_credits(seller["id"], 3, first_credit_id=90_000 + seller["id"] * 10)
    yield ids
    placeholders = ", ".join(str(i) for i in ids)
    await database.database.execute(f"DELETE FROM credits WHERE id IN ({placeholders})")


async def test_listings_require_authentication(app_client):
    assert (await app_client.get("/api/marketplace/listings")).status_code == 401


async def test_listings_expose_server_side_pricing(app_client, make_user, listed_credits):
    token, _ = await make_user("buyer")
    body = (await app_client.get("/api/marketplace/listings", headers=auth(token))).json()

    assert body["price_per_credit_inr"] == config.CREDIT_VALUE_INR
    assert body["price_per_credit_usd"] == config.CREDIT_VALUE_USD
    assert body["buyer_can_purchase"] is True


async def test_installers_may_browse_but_not_purchase(app_client, make_user, listed_credits):
    token, _ = await make_user("installer")
    body = (await app_client.get("/api/marketplace/listings", headers=auth(token))).json()
    assert body["buyer_can_purchase"] is False


async def test_reserve_then_purchase(app_client, make_user, listed_credits):
    token, _ = await make_user("buyer")

    reserved = await app_client.post(
        "/api/marketplace/reserve", headers=auth(token), json={"credit_ids": listed_credits}
    )
    assert reserved.status_code == 200, reserved.text
    assert reserved.json()["quantity"] == 3
    assert reserved.json()["total_inr"] == 3 * config.CREDIT_VALUE_INR

    purchased = await app_client.post(
        "/api/marketplace/purchase", headers=auth(token), json={"reservation_ids": listed_credits}
    )
    assert purchased.status_code == 200, purchased.text
    body = purchased.json()
    assert body["quantity"] == 3
    assert body["receipt"]["co2_offset_kg"] == 3 * config.KG_CO2_PER_CREDIT


async def test_a_credit_cannot_be_purchased_twice(app_client, make_user, listed_credits):
    token, _ = await make_user("buyer")
    await app_client.post(
        "/api/marketplace/reserve", headers=auth(token), json={"credit_ids": listed_credits}
    )
    await app_client.post(
        "/api/marketplace/purchase", headers=auth(token), json={"reservation_ids": listed_credits}
    )

    repeat = await app_client.post(
        "/api/marketplace/purchase", headers=auth(token), json={"reservation_ids": listed_credits}
    )
    assert repeat.status_code == 409


async def test_concurrent_buyers_cannot_both_reserve_one_credit(
    app_client, make_user, listed_credits
):
    """
    Regression test.

    Reservation used to read the status and then write it in a separate step.
    Two buyers racing on the same credit could both observe 'listed' and both
    receive a success response, while only one actually held the credit.
    """
    first_token, first_user = await make_user("buyer")
    second_token, second_user = await make_user("buyer")
    target = [listed_credits[0]]

    first, second = await asyncio.gather(
        app_client.post("/api/marketplace/reserve", headers=auth(first_token),
                        json={"credit_ids": target}),
        app_client.post("/api/marketplace/reserve", headers=auth(second_token),
                        json={"credit_ids": target}),
    )

    statuses = sorted([first.status_code, second.status_code])
    assert statuses == [200, 409], f"expected exactly one winner, got {statuses}"

    # The successful response must match who actually holds the credit.
    row = await database.database.fetch_one(
        "SELECT reserved_by FROM credits WHERE id = :id", {"id": target[0]}
    )
    winner = first_user["id"] if first.status_code == 200 else second_user["id"]
    assert row["reserved_by"] == winner


async def test_a_partly_unavailable_batch_reserves_nothing(
    app_client, make_user, listed_credits
):
    """A buyer must not end up holding a subset they never agreed to."""
    first_token, _ = await make_user("buyer")
    second_token, _ = await make_user("buyer")

    await app_client.post(
        "/api/marketplace/reserve", headers=auth(first_token),
        json={"credit_ids": [listed_credits[0]]},
    )

    contested = await app_client.post(
        "/api/marketplace/reserve", headers=auth(second_token),
        json={"credit_ids": listed_credits},
    )
    assert contested.status_code == 409

    # The two uncontested credits must be back on the marketplace.
    for credit_id in listed_credits[1:]:
        row = await database.database.fetch_one(
            "SELECT status, reserved_by FROM credits WHERE id = :id", {"id": credit_id}
        )
        assert row["status"] == "listed"
        assert row["reserved_by"] is None


async def test_cancelling_a_reservation_relists_the_credits(
    app_client, make_user, listed_credits
):
    token, _ = await make_user("buyer")
    await app_client.post(
        "/api/marketplace/reserve", headers=auth(token), json={"credit_ids": listed_credits}
    )

    cancelled = await app_client.post(
        "/api/marketplace/cancel-reservation", headers=auth(token),
        json={"credit_ids": listed_credits},
    )
    assert cancelled.json()["credits_released"] == 3

    row = await database.database.fetch_one(
        "SELECT status FROM credits WHERE id = :id", {"id": listed_credits[0]}
    )
    assert row["status"] == "listed"


async def test_a_buyer_cannot_cancel_another_buyers_reservation(
    app_client, make_user, listed_credits
):
    owner_token, _ = await make_user("buyer")
    other_token, _ = await make_user("buyer")

    await app_client.post(
        "/api/marketplace/reserve", headers=auth(owner_token), json={"credit_ids": listed_credits}
    )
    response = await app_client.post(
        "/api/marketplace/cancel-reservation", headers=auth(other_token),
        json={"credit_ids": listed_credits},
    )
    assert response.json()["credits_released"] == 0


async def test_a_buyer_cannot_purchase_someone_elses_reservation(
    app_client, make_user, listed_credits
):
    owner_token, _ = await make_user("buyer")
    thief_token, _ = await make_user("buyer")

    await app_client.post(
        "/api/marketplace/reserve", headers=auth(owner_token), json={"credit_ids": listed_credits}
    )
    response = await app_client.post(
        "/api/marketplace/purchase", headers=auth(thief_token),
        json={"reservation_ids": listed_credits},
    )
    assert response.status_code == 409


async def test_purchases_appear_in_the_buyers_history(app_client, make_user, listed_credits):
    token, _ = await make_user("buyer")
    await app_client.post(
        "/api/marketplace/reserve", headers=auth(token), json={"credit_ids": listed_credits}
    )
    await app_client.post(
        "/api/marketplace/purchase", headers=auth(token), json={"reservation_ids": listed_credits}
    )

    history = (await app_client.get("/api/marketplace/my-purchases", headers=auth(token))).json()
    assert history["total"] == 1
    assert history["purchases"][0]["quantity"] == 3
    assert history["purchases"][0]["payment_status"] == "completed"


async def test_an_empty_selection_is_rejected(app_client, make_user):
    token, _ = await make_user("buyer")
    response = await app_client.post(
        "/api/marketplace/reserve", headers=auth(token), json={"credit_ids": []}
    )
    assert response.status_code == 422
