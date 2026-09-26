"""
The settlement split: every sale divides 70/20/10 between the generator, the
treasury, and the reserve, exactly, in integer minor units.
"""

import pytest

import config
import database
import settlement
from tests.conftest import auth
from tests.test_marketplace import _seed_listed_credits


def test_the_default_split_is_70_20_10():
    assert settlement.split_bps() == {"seller": 7000, "treasury": 2000, "reserve": 1000}
    assert settlement.split_amount(500) == {"seller": 350, "treasury": 100, "reserve": 50}


@pytest.mark.parametrize("amount", [0, 1, 7, 99, 333, 41_500, 10**9 + 7])
def test_the_parts_always_add_back_to_the_price(amount):
    parts = settlement.split_amount(amount)
    assert sum(parts.values()) == amount
    assert min(parts.values()) >= 0


def test_rounding_goes_to_the_reserve():
    # 333 * 0.7 = 233.1 and 333 * 0.2 = 66.6: the 0.7 left over is the reserve's.
    assert settlement.split_amount(333) == {"seller": 233, "treasury": 66, "reserve": 34}


def test_a_split_that_does_not_total_100_percent_refuses_to_start(monkeypatch):
    monkeypatch.setattr(config, "SPLIT_TREASURY_BPS", 2500)
    with pytest.raises(config.ConfigError, match="10000"):
        config.validate()


async def _buy(app_client, token, ids):
    reserved = await app_client.post(
        "/api/marketplace/reserve", headers=auth(token), json={"credit_ids": ids})
    assert reserved.status_code == 200, reserved.text
    purchased = await app_client.post(
        "/api/marketplace/purchase", headers=auth(token), json={"reservation_ids": ids})
    assert purchased.status_code == 200, purchased.text
    return purchased.json()


async def test_a_purchase_records_its_split(app_client, make_user):
    _, seller = await make_user("installer")
    ids = await _seed_listed_credits(seller["id"], 2, first_credit_id=700_000 + seller["id"] * 10)
    token, _ = await make_user("buyer")

    body = await _buy(app_client, token, ids)
    split = body["settlement"]
    assert split["seller"]["usd"] == round(2 * config.CREDIT_VALUE_USD * 0.7, 2)
    assert split["treasury"]["usd"] == round(2 * config.CREDIT_VALUE_USD * 0.2, 2)
    assert split["reserve"]["usd"] == round(2 * config.CREDIT_VALUE_USD * 0.1, 2)
    assert round(sum(p["inr"] for p in split.values()), 2) == body["total_inr"]

    rows = await database.database.fetch_all(
        "SELECT payee, payee_user_id, status FROM settlement_payouts WHERE transaction_id = :t",
        {"t": body["transaction_id"]},
    )
    assert len(rows) == 6
    assert {r["payee_user_id"] for r in rows if r["payee"] == "seller"} == {seller["id"]}
    assert {r["status"] for r in rows} == {"simulated"}


async def test_a_mixed_basket_pays_each_generator_for_their_own_credits(app_client, make_user):
    seller_a_token, seller_a = await make_user("installer")
    seller_b_token, seller_b = await make_user("installer")
    a = await _seed_listed_credits(seller_a["id"], 1, first_credit_id=710_000 + seller_a["id"] * 10)
    b = await _seed_listed_credits(seller_b["id"], 3, first_credit_id=720_000 + seller_b["id"] * 10)
    token, _ = await make_user("buyer")
    await _buy(app_client, token, a + b)

    per_credit = config.CREDIT_VALUE_USD * config.SPLIT_SELLER_BPS / 10_000
    earned_a = (await app_client.get("/api/installer/earnings", headers=auth(seller_a_token))).json()
    earned_b = (await app_client.get("/api/installer/earnings", headers=auth(seller_b_token))).json()
    assert (earned_a["credits_sold"], earned_a["usd"]) == (1, round(per_credit, 2))
    assert (earned_b["credits_sold"], earned_b["usd"]) == (3, round(3 * per_credit, 2))


async def test_admins_see_totals_per_payee(app_client, admin_token, make_user):
    _, seller = await make_user("installer")
    ids = await _seed_listed_credits(seller["id"], 1, first_credit_id=730_000 + seller["id"] * 10)
    token, _ = await make_user("buyer")
    await _buy(app_client, token, ids)

    body = (await app_client.get("/api/admin/settlements", headers=auth(admin_token))).json()
    assert body["split_bps"] == {"seller": 7000, "treasury": 2000, "reserve": 1000}
    assert {t["payee"] for t in body["totals"]} == {"seller", "treasury", "reserve"}
    assert any(s["user_id"] == seller["id"] for s in body["sellers"])


async def test_only_admins_see_settlements(app_client, make_user):
    token, _ = await make_user("installer")
    assert (await app_client.get("/api/admin/settlements", headers=auth(token))).status_code == 403
