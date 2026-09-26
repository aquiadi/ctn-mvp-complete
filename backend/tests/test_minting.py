"""
Minting and retirement against a fake chain.

The chain module is replaced with an in-memory stand-in so these tests pin
down the API's own guarantees: a credit is minted at most once however many
requests race for it, its on-chain id comes from its own mint, a transaction
lost between broadcast and confirmation is reconciled rather than repeated,
and only credits that could be sold reach the chain at all.
"""

import asyncio

import pytest

import chain
import database
from tests.conftest import auth
from tests.sensors import SimulatedSensor

RECIPIENT = "0x" + "ab" * 20


class FakeChain:
    def __init__(self):
        self.broadcasts = []
        self.retirements = []
        self.next_id = 500
        self.confirm_error = None
        self.lookup_result = None

    async def broadcast_mint(self, recipient, ipfs_hash, energy_kwh, co2_kg):
        await asyncio.sleep(0.05)  # widen the race window
        self.broadcasts.append((recipient, ipfs_hash))
        return f"0x{len(self.broadcasts):064x}"

    async def confirm_mint(self, tx_hash):
        if self.confirm_error:
            raise self.confirm_error
        self.next_id += 1
        return self.next_id

    async def lookup_mint(self, tx_hash):
        if isinstance(self.lookup_result, Exception):
            raise self.lookup_result
        return self.lookup_result

    async def retire(self, on_chain_id, beneficiary="", address=None):
        self.retirements.append((on_chain_id, beneficiary, address))
        return f"0x{'f' * 63}{len(self.retirements)}"


@pytest.fixture
def fake_chain(monkeypatch):
    fake = FakeChain()
    monkeypatch.setattr(chain, "is_configured", lambda: True)
    for name in ("broadcast_mint", "confirm_mint", "lookup_mint", "retire"):
        monkeypatch.setattr(chain, name, getattr(fake, name))
    return fake


async def _issue_credit(app_client, admin_token, make_user, device_id, verify=True):
    sensor = SimulatedSensor(device_id)
    _, installer = await make_user("installer")
    await app_client.post(
        "/api/admin/devices", headers=auth(admin_token),
        json={"device_id": device_id, "owner_email": installer["email"],
              "public_key": sensor.public_key},
    )
    if verify:
        await app_client.post(
            f"/api/admin/devices/{device_id}/verify",
            headers=auth(admin_token), json={"note": "Confirmed on site"},
        )
    response = await app_client.post(
        "/api/v1/readings", json={"readings": [sensor.reading(700.0), sensor.reading(700.0)]}
    )
    assert response.json()["credits_issued"] == 1
    row = await database.database.fetch_one(
        "SELECT credit_id FROM credits WHERE device_id = :d", {"d": device_id}
    )
    return row["credit_id"]


async def _mint(app_client, admin_token, credit_id):
    return await app_client.post(
        f"/mint/{credit_id}?recipient={RECIPIENT}", headers=auth(admin_token)
    )


async def _credit(credit_id):
    return dict(await database.database.fetch_one(
        "SELECT * FROM credits WHERE credit_id = :c", {"c": credit_id}
    ))


# ── Gates ──────────────────────────────────────────────────────────────────

async def test_a_pending_credit_cannot_be_minted(app_client, admin_token, make_user, fake_chain):
    credit_id = await _issue_credit(app_client, admin_token, make_user, "MNT-PENDING", verify=False)

    response = await _mint(app_client, admin_token, credit_id)
    assert response.status_code == 409
    assert fake_chain.broadcasts == []


async def test_a_held_credit_cannot_be_minted(app_client, admin_token, make_user, fake_chain):
    credit_id = await _issue_credit(app_client, admin_token, make_user, "MNT-HELD")
    await database.database.execute(
        "UPDATE credits SET review_hold = 'flagged' WHERE credit_id = :c", {"c": credit_id}
    )

    assert (await _mint(app_client, admin_token, credit_id)).status_code == 409
    assert fake_chain.broadcasts == []


# ── Exactly once ───────────────────────────────────────────────────────────

async def test_racing_mints_broadcast_once(app_client, admin_token, make_user, fake_chain):
    """
    Regression test.

    Two concurrent requests both passed the "not yet minted" check and both
    minted the same credit on-chain.
    """
    credit_id = await _issue_credit(app_client, admin_token, make_user, "MNT-RACE")

    responses = await asyncio.gather(
        *(_mint(app_client, admin_token, credit_id) for _ in range(4))
    )
    codes = sorted(r.status_code for r in responses)
    assert codes == [200, 409, 409, 409]
    assert len(fake_chain.broadcasts) == 1


async def test_the_on_chain_id_comes_from_the_mint_itself(
    app_client, admin_token, make_user, fake_chain
):
    credit_id = await _issue_credit(app_client, admin_token, make_user, "MNT-ID")

    response = await _mint(app_client, admin_token, credit_id)
    assert response.status_code == 200, response.text
    assert response.json()["on_chain_id"] == fake_chain.next_id

    credit = await _credit(credit_id)
    assert credit["on_chain_id"] == fake_chain.next_id
    assert credit["mint_claim"] is None


async def test_a_revert_releases_the_claim(app_client, admin_token, make_user, fake_chain):
    credit_id = await _issue_credit(app_client, admin_token, make_user, "MNT-REVERT")

    fake_chain.confirm_error = chain.ChainReverted("reverted")
    assert (await _mint(app_client, admin_token, credit_id)).status_code == 502

    fake_chain.confirm_error = None
    assert (await _mint(app_client, admin_token, credit_id)).status_code == 200
    assert len(fake_chain.broadcasts) == 2


async def test_an_unconfirmed_mint_is_reconciled_not_repeated(
    app_client, admin_token, make_user, fake_chain
):
    credit_id = await _issue_credit(app_client, admin_token, make_user, "MNT-LOST")

    fake_chain.confirm_error = chain.ChainPending("no receipt yet")
    assert (await _mint(app_client, admin_token, credit_id)).status_code == 504

    # The transaction may still land, so a second mint is refused.
    assert (await _mint(app_client, admin_token, credit_id)).status_code == 409
    assert len(fake_chain.broadcasts) == 1

    fake_chain.lookup_result = 9001
    response = await app_client.post(f"/mint/{credit_id}/reconcile", headers=auth(admin_token))
    assert response.status_code == 200
    assert response.json()["status"] == "minted"
    assert (await _credit(credit_id))["on_chain_id"] == 9001


async def test_a_mint_the_network_never_saw_is_released(
    app_client, admin_token, make_user, fake_chain
):
    credit_id = await _issue_credit(app_client, admin_token, make_user, "MNT-DROPPED")

    fake_chain.confirm_error = chain.ChainPending("no receipt yet")
    await _mint(app_client, admin_token, credit_id)

    fake_chain.lookup_result = None
    response = await app_client.post(f"/mint/{credit_id}/reconcile", headers=auth(admin_token))
    assert response.json()["status"] == "released"

    fake_chain.confirm_error = None
    assert (await _mint(app_client, admin_token, credit_id)).status_code == 200


# ── Retirement ─────────────────────────────────────────────────────────────

async def test_a_listed_credit_cannot_be_retired(app_client, admin_token, make_user, fake_chain):
    credit_id = await _issue_credit(app_client, admin_token, make_user, "MNT-LISTED")
    await _mint(app_client, admin_token, credit_id)
    await database.database.execute(
        "UPDATE credits SET status = 'listed' WHERE credit_id = :c", {"c": credit_id}
    )

    response = await app_client.post(f"/retire/{credit_id}", headers=auth(admin_token))
    assert response.status_code == 409
    assert fake_chain.retirements == []


async def test_a_sold_credit_is_retired_for_its_buyer(app_client, admin_token, make_user, fake_chain):
    credit_id = await _issue_credit(app_client, admin_token, make_user, "MNT-SOLD")
    await _mint(app_client, admin_token, credit_id)
    await database.database.execute(
        "UPDATE credits SET status = 'sold', buyer_user_id = 42 WHERE credit_id = :c",
        {"c": credit_id},
    )

    response = await app_client.post(f"/retire/{credit_id}", headers=auth(admin_token))
    assert response.status_code == 200, response.text
    assert response.json()["beneficiary"] == "CTN buyer account #42"

    credit = await _credit(credit_id)
    assert credit["status"] == "retired"
    assert credit["retirement_beneficiary"] == "CTN buyer account #42"
    assert fake_chain.retirements[-1][1] == "CTN buyer account #42"


async def test_a_named_beneficiary_is_recorded(app_client, admin_token, make_user, fake_chain):
    credit_id = await _issue_credit(app_client, admin_token, make_user, "MNT-NAMED")
    await _mint(app_client, admin_token, credit_id)

    response = await app_client.post(
        f"/retire/{credit_id}",
        params={"beneficiary": "Acme Ltd", "purpose": "FY2026 Scope 2"},
        headers=auth(admin_token),
    )
    assert response.status_code == 200
    credit = await _credit(credit_id)
    assert (credit["retirement_beneficiary"], credit["retirement_purpose"]) == (
        "Acme Ltd", "FY2026 Scope 2",
    )


# ── Moving to a new contract ───────────────────────────────────────────────

V2_ADDRESS = "0x" + "c2" * 20


async def test_a_mint_records_the_contract_it_went_to(app_client, admin_token, make_user, fake_chain):
    credit_id = await _issue_credit(app_client, admin_token, make_user, "MNT-WHERE")
    await _mint(app_client, admin_token, credit_id)
    assert (await _credit(credit_id))["contract_address"] == chain.contract.address


async def test_old_credits_stay_on_their_contract_after_a_switch(
    app_client, admin_token, make_user, fake_chain, monkeypatch
):
    """
    Pointing CONTRACT_ADDRESS at a V2 deployment must not send reads or
    retirements for V1 credits to the new contract, where their ids mean
    something else or nothing at all.
    """
    import config

    credit_id = await _issue_credit(app_client, admin_token, make_user, "MNT-SWITCH")
    await _mint(app_client, admin_token, credit_id)
    v1_address = (await _credit(credit_id))["contract_address"]

    monkeypatch.setattr(config, "CONTRACT_ADDRESS", V2_ADDRESS)
    monkeypatch.setattr(config, "CONTRACT_VERSION", 2)
    monkeypatch.setattr(chain, "contract", chain.contract_at(V2_ADDRESS))

    reads = []

    async def fake_get_credit(on_chain_id, address=None):
        reads.append(address)
        return None

    monkeypatch.setattr(chain, "get_credit", fake_get_credit)
    await app_client.get(f"/verify/{credit_id}")
    assert reads == [v1_address]

    response = await app_client.post(
        f"/retire/{credit_id}", params={"beneficiary": "Acme"}, headers=auth(admin_token))
    assert response.status_code == 200, response.text
    assert fake_chain.retirements[-1][2] == v1_address
    # V1 has no beneficiary field; the API must not claim it went on-chain.
    assert response.json()["beneficiary_on_chain"] is False

    # New mints go to the new contract.
    new_credit = await _issue_credit(app_client, admin_token, make_user, "MNT-SWITCH-NEW")
    await _mint(app_client, admin_token, new_credit)
    assert (await _credit(new_credit))["contract_address"].lower() == V2_ADDRESS


def test_contract_versions_follow_the_address(monkeypatch):
    import config

    monkeypatch.setattr(config, "CONTRACT_ADDRESS", V2_ADDRESS)
    monkeypatch.setattr(config, "CONTRACT_VERSION", 2)
    assert chain.version_at(V2_ADDRESS) == 2
    assert chain.version_at(config.LEGACY_CONTRACT_ADDRESS) == 1


async def test_credits_minted_before_the_column_existed_are_attributed_to_v1(app_client):
    import config

    row = await database.database.execute(
        """INSERT INTO credits (credit_id, device_id, status, on_chain_id, tx_hash)
           VALUES (880001, 'LEGACY', 'verified', 77, '0xabc')"""
    )
    await database._apply_schema()
    record = await database.database.fetch_one(
        "SELECT contract_address FROM credits WHERE id = :id", {"id": row})
    assert record["contract_address"] == config.LEGACY_CONTRACT_ADDRESS
