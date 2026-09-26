"""
Credit issuance: exact allocation, self-verifying certificates, review holds.

Every credit must be recomputable from its own evidence. These tests check
that the readings behind a credit add up to exactly one credit's worth, that
the certificate alone is enough to verify every attested reading, and that
screening flags hold a credit for a person rather than releasing it unseen.
"""

import hashlib
import json

import config
import database
from attestation import recover_signer
from tests.conftest import auth
from tests.sensors import SimulatedSensor

KG = config.KG_CO2_PER_CREDIT
FACTOR = config.EMISSION_FACTOR_KG_PER_KWH


async def _register(app_client, admin_token, make_user, sensor, verify=True):
    _, installer = await make_user("installer")
    response = await app_client.post(
        "/api/admin/devices",
        headers=auth(admin_token),
        json={
            "device_id": sensor.device_id,
            "owner_email": installer["email"],
            "location": "Test Site",
            "public_key": sensor.public_key,
        },
    )
    assert response.status_code == 200, response.text
    if verify:
        await app_client.post(
            f"/api/admin/devices/{sensor.device_id}/verify",
            headers=auth(admin_token), json={"note": "Confirmed on site"},
        )
    return installer


async def _post(app_client, readings):
    response = await app_client.post("/api/v1/readings", json={"readings": readings})
    assert response.status_code == 200, response.text
    return response.json()


async def _credits(device_id):
    return [
        dict(r) for r in await database.database.fetch_all(
            "SELECT * FROM credits WHERE device_id = :d ORDER BY credit_id", {"d": device_id}
        )
    ]


async def _allocated_kg(credit_row_id):
    row = await database.database.fetch_one(
        "SELECT SUM(kg) AS kg FROM credit_allocations WHERE credit_row_id = :c",
        {"c": credit_row_id},
    )
    return row["kg"]


# ── Allocation ─────────────────────────────────────────────────────────────

async def test_a_reading_can_straddle_two_credits(app_client, admin_token, make_user):
    """1000 kWh readings are 820 kg each; the second completes one credit and opens the next."""
    sensor = SimulatedSensor("ISS-SPLIT")
    await _register(app_client, admin_token, make_user, sensor)

    body = await _post(app_client, [sensor.reading(1000.0), sensor.reading(1000.0)])
    assert body["credits_issued"] == 1

    [credit] = await _credits(sensor.device_id)
    assert abs(await _allocated_kg(credit["id"]) - KG) < 1e-6
    assert abs(credit["total_kwh"] - KG / FACTOR) < 1e-6


async def test_the_remainder_survives_between_ingests(app_client, admin_token, make_user):
    """
    Regression test.

    The overshoot of the reading that completed a credit lived only in memory
    and was lost when the request ended, so every credit silently under-counted
    the readings after it.
    """
    sensor = SimulatedSensor("ISS-CARRY")
    await _register(app_client, admin_token, make_user, sensor)

    await _post(app_client, [sensor.reading(1000.0), sensor.reading(1000.0)])  # 1640 kg: 1 credit, 640 left
    body = await _post(app_client, [sensor.reading(500.0)])                    # +410 kg: 1050 kg
    assert body["credits_issued"] == 1

    credits = await _credits(sensor.device_id)
    assert len(credits) == 2
    for credit in credits:
        assert abs(await _allocated_kg(credit["id"]) - KG) < 1e-6

    open_kg = await database.database.fetch_one(
        """SELECT SUM(co2_avoided_kg - allocated_kg) AS kg FROM generation_readings
           WHERE device_id = :d""",
        {"d": sensor.device_id},
    )
    assert abs(open_kg["kg"] - 50.0) < 1e-6


async def test_a_large_reading_can_fill_several_credits(app_client, admin_token, make_user):
    sensor = SimulatedSensor("ISS-MULTI")
    await _register(app_client, admin_token, make_user, sensor)

    body = await _post(app_client, [sensor.reading(4000.0)])  # 3280 kg
    assert body["credits_issued"] == 3

    reading = await database.database.fetch_one(
        "SELECT allocated_kg, consumed_by_credit_id FROM generation_readings WHERE device_id = :d",
        {"d": sensor.device_id},
    )
    assert abs(reading["allocated_kg"] - 3000.0) < 1e-6
    assert reading["consumed_by_credit_id"] is None  # 280 kg still open


# ── Certificates ───────────────────────────────────────────────────────────

async def test_a_certificate_verifies_without_the_api(app_client, admin_token, make_user):
    sensor = SimulatedSensor("ISS-CERT")
    await _register(app_client, admin_token, make_user, sensor)
    await _post(app_client, [sensor.reading(700.0), sensor.reading(700.0)])

    [credit] = await _credits(sensor.device_id)
    document = json.loads(credit["certificate"])

    # The stored text is exactly what the recorded hash commits to.
    assert credit["ipfs_hash"] == "local-" + hashlib.sha256(
        credit["certificate"].encode("utf-8")
    ).hexdigest()

    assert document["schema"] == "ctn-certificate/v2"
    assert document["methodology"]["factor_value"] == FACTOR
    assert document["methodology"]["factor_source"]
    assert document["methodology"]["factor_vintage"]

    total = 0.0
    for entry in document["readings"]:
        attestation = entry["attestation"]
        assert recover_signer(
            attestation["signed_message"], attestation["device_signature"]
        ).lower() == document["device"]["public_key"].lower()
        total += entry["allocated_kg"]
    assert abs(total - document["co2_avoided_kg"]) < 1e-4


async def test_imported_readings_are_marked_in_the_certificate(app_client, admin_token):
    _ = admin_token
    readings = [
        {"device_id": "ISS-IMPORT", "timestamp": f"2025-01-01 {h:02d}:00:00",
         "total_kwh": 700.0, "co2_avoided_kg": 700.0 * FACTOR}
        for h in range(2)
    ]
    await database.process_raw_readings(readings, owner_user_id=1)

    [credit] = await _credits("ISS-IMPORT")
    document = json.loads(credit["certificate"])
    assert document["readings_attested"] == 0
    assert all(r["provenance"] == "imported" and r["attestation"] is None
               for r in document["readings"])


# ── Review holds ───────────────────────────────────────────────────────────

async def test_a_flatlined_meter_holds_its_credit(app_client, admin_token, make_user):
    sensor = SimulatedSensor("ISS-FLAT")
    await _register(app_client, admin_token, make_user, sensor)

    # Identical values for a whole run: what a stuck meter, or a placeholder
    # left in firmware, looks like.
    body = await _post(app_client, [sensor.reading(110.0) for _ in range(config.ANOMALY_FLATLINE_RUN)])
    assert body["readings_flagged"] >= 1
    assert body["credits_issued"] == 1

    [credit] = await _credits(sensor.device_id)
    assert credit["status"] == "pending"
    assert credit["review_hold"]

    queue = (await app_client.get("/api/admin/review-queue", headers=auth(admin_token))).json()
    entry = next(c for c in queue["credits"] if c["credit_id"] == credit["credit_id"])
    assert entry["flagged_readings"][0]["anomaly_flags"][0]["code"] == "flatline"


async def test_confirming_the_device_does_not_release_a_held_credit(
    app_client, admin_token, make_user
):
    sensor = SimulatedSensor("ISS-FLAT-UNVERIFIED")
    await _register(app_client, admin_token, make_user, sensor, verify=False)
    await _post(app_client, [sensor.reading(110.0) for _ in range(config.ANOMALY_FLATLINE_RUN)])

    await app_client.post(
        f"/api/admin/devices/{sensor.device_id}/verify",
        headers=auth(admin_token), json={"note": "Confirmed on site"},
    )
    [credit] = await _credits(sensor.device_id)
    assert credit["status"] == "pending"


async def test_a_reviewer_can_release_a_held_credit(app_client, admin_token, make_user):
    sensor = SimulatedSensor("ISS-FLAT-RELEASE")
    await _register(app_client, admin_token, make_user, sensor)
    await _post(app_client, [sensor.reading(110.0) for _ in range(config.ANOMALY_FLATLINE_RUN)])
    [credit] = await _credits(sensor.device_id)

    too_short = await app_client.post(
        f"/api/admin/credits/{credit['credit_id']}/release",
        headers=auth(admin_token), json={"note": "ok"},
    )
    assert too_short.status_code == 400

    response = await app_client.post(
        f"/api/admin/credits/{credit['credit_id']}/release",
        headers=auth(admin_token), json={"note": "Inverter log confirms constant export"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "verified"

    [credit] = await _credits(sensor.device_id)
    assert credit["review_hold"] is None
