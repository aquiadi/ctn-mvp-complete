"""
Physical plausibility and the V2 meter/tamper protocol.

A valid signature only says who produced a reading. These tests pin down the
rules that reject what no real installation could have produced, and the V2
counters that tie each interval to the device's own meter and enclosure.
"""

import time

import attestation
import database
from tests.conftest import auth
from tests.sensors import SimulatedSensor, iso

V2 = attestation.MESSAGE_VERSION_V2


async def _register(app_client, admin_token, make_user, sensor, **site):
    _, installer = await make_user("installer")
    response = await app_client.post(
        "/api/admin/devices",
        headers=auth(admin_token),
        json={
            "device_id": sensor.device_id,
            "owner_email": installer["email"],
            "location": "Test Site",
            "public_key": sensor.public_key,
            **site,
        },
    )
    assert response.status_code == 200, response.text
    return installer


async def _post(app_client, readings):
    return await app_client.post("/api/v1/readings", json={"readings": readings})


def _code(response) -> str:
    return response.json()["detail"]["code"]


# ── Timestamps ─────────────────────────────────────────────────────────────

async def test_the_reported_exploit_is_refused(app_client, admin_token, make_user):
    """
    Regression test.

    One signed reading of 50,000 kWh dated 2099 was accepted and issued 41
    credits on the spot.
    """
    sensor = SimulatedSensor("PLB-EXPLOIT")
    await _register(app_client, admin_token, make_user, sensor, rated_capacity_kw=10)

    response = await _post(app_client, [sensor.reading(50000.0, timestamp="2099-01-01T00:00:00Z")])
    assert response.status_code == 422
    assert _code(response) == "timestamp_future"


async def test_a_malformed_timestamp_is_refused(app_client, admin_token, make_user):
    sensor = SimulatedSensor("PLB-FMT")
    await _register(app_client, admin_token, make_user, sensor)

    for bad in ("yesterday-ish", "2026-09-26 10:00:00", "2026-02-30T00:00:00Z"):
        response = await _post(app_client, [sensor.reading(0.1, timestamp=bad)])
        assert response.status_code == 422, bad
        assert _code(response) == "timestamp_format"


async def test_a_reading_from_before_registration_is_refused(app_client, admin_token, make_user):
    sensor = SimulatedSensor("PLB-BACKDATE")
    await _register(app_client, admin_token, make_user, sensor)

    response = await _post(app_client, [sensor.reading(0.1, timestamp=iso(time.time() - 3600))])
    assert response.status_code == 422
    assert _code(response) == "timestamp_before_registration"


async def test_a_reading_outside_the_backfill_window_is_refused(app_client, admin_token, make_user):
    sensor = SimulatedSensor("PLB-STALE")
    await _register(app_client, admin_token, make_user, sensor)
    await database.database.execute(
        "UPDATE devices SET created_at = :t WHERE device_id = :d",
        {"t": time.time() - 30 * 86400, "d": sensor.device_id},
    )

    response = await _post(app_client, [sensor.reading(0.1, timestamp=iso(time.time() - 5 * 86400))])
    assert response.status_code == 422
    assert _code(response) == "timestamp_stale"


async def test_timestamps_must_move_forward(app_client, admin_token, make_user):
    sensor = SimulatedSensor("PLB-ORDER")
    await _register(app_client, admin_token, make_user, sensor)

    first = sensor.reading(0.1)
    assert (await _post(app_client, [first])).status_code == 200

    response = await _post(app_client, [sensor.reading(0.1, timestamp=first["timestamp"])])
    assert response.status_code == 422
    assert _code(response) == "timestamp_not_monotonic"


async def test_authentication_is_checked_before_content(app_client, admin_token, make_user):
    """A forged packet learns nothing about the device's plausibility state."""
    sensor = SimulatedSensor("PLB-AUTHFIRST")
    await _register(app_client, admin_token, make_user, sensor)

    forged = sensor.reading(0.1)
    forged["timestamp"] = "2099-01-01T00:00:00Z"
    assert (await _post(app_client, [forged])).status_code == 401


# ── Capacity ───────────────────────────────────────────────────────────────

async def test_energy_is_bounded_by_rated_capacity(app_client, admin_token, make_user):
    sensor = SimulatedSensor("PLB-CAP")
    await _register(app_client, admin_token, make_user, sensor, rated_capacity_kw=5)

    # A 5 kW system over the 15-minute floor exports at most 5 * 0.25 * 1.1 kWh.
    response = await _post(app_client, [sensor.reading(2.0)])
    assert response.status_code == 422
    assert _code(response) == "exceeds_capacity"

    assert (await _post(app_client, [sensor.reading(1.2)])).status_code == 200


async def test_a_rejected_batch_advances_nothing(app_client, admin_token, make_user):
    sensor = SimulatedSensor("PLB-ATOMIC")
    await _register(app_client, admin_token, make_user, sensor, rated_capacity_kw=5)

    response = await _post(app_client, [sensor.reading(0.5), sensor.reading(99.0)])
    assert response.status_code == 422

    record = (await app_client.get(f"/api/v1/devices/{sensor.device_id}")).json()
    assert record["last_sequence"] == 0
    assert record["readings_total"] == 0


# ── V2: meter continuity ───────────────────────────────────────────────────

async def test_v2_readings_are_accepted_and_tracked(app_client, admin_token, make_user):
    sensor = SimulatedSensor("PLB-V2", version=V2)
    await _register(app_client, admin_token, make_user, sensor)

    response = await _post(app_client, [sensor.reading(0.25), sensor.reading(0.5)])
    assert response.status_code == 200, response.text
    assert response.json()["meter_wh"] == 750

    record = (await app_client.get(f"/api/v1/devices/{sensor.device_id}")).json()
    assert record["last_meter_wh"] == 750
    assert record["last_sequence"] == 2


async def test_energy_must_match_the_meter_advance(app_client, admin_token, make_user):
    sensor = SimulatedSensor("PLB-V2-CONT", version=V2)
    await _register(app_client, admin_token, make_user, sensor)
    assert (await _post(app_client, [sensor.reading(0.25)])).status_code == 200

    # Claims 400 Wh while the counter moved 100 Wh: double counting.
    response = await _post(app_client, [sensor.reading(0.4, meter_wh=350)])
    assert response.status_code == 422
    assert _code(response) == "meter_discontinuity"


async def test_the_meter_can_never_run_backwards(app_client, admin_token, make_user):
    sensor = SimulatedSensor("PLB-V2-BACK", version=V2)
    await _register(app_client, admin_token, make_user, sensor)
    assert (await _post(app_client, [sensor.reading(0.5)])).status_code == 200

    response = await _post(app_client, [sensor.reading(0.0, meter_wh=100)])
    assert response.status_code == 422
    assert _code(response) == "meter_regressed"


async def test_a_v2_device_cannot_downgrade_to_v1(app_client, admin_token, make_user):
    sensor = SimulatedSensor("PLB-V2-DOWN", version=V2)
    await _register(app_client, admin_token, make_user, sensor)
    assert (await _post(app_client, [sensor.reading(0.25)])).status_code == 200

    sensor.version = attestation.MESSAGE_VERSION_V1
    response = await _post(app_client, [sensor.reading(0.25)])
    assert response.status_code == 422
    assert _code(response) == "version_downgrade"


async def test_v2_requires_its_counters(app_client, admin_token, make_user):
    sensor = SimulatedSensor("PLB-V2-MISSING", version=V2)
    await _register(app_client, admin_token, make_user, sensor)

    reading = sensor.reading(0.25)
    del reading["meter_wh"]
    assert (await _post(app_client, [reading])).status_code == 422


async def test_a_device_resyncs_after_a_lost_response(app_client, admin_token, make_user):
    """
    The server commits a batch but the response never reaches the device. It
    retries the same sequence, gets a 409, and resumes from the device record
    without counting the committed energy twice.
    """
    sensor = SimulatedSensor("PLB-V2-RESYNC", version=V2)
    await _register(app_client, admin_token, make_user, sensor)

    committed = sensor.reading(0.3)
    assert (await _post(app_client, [committed])).status_code == 200

    # Device never saw the 200, so it re-sends the same packet.
    assert (await _post(app_client, [committed])).status_code == 409

    record = (await app_client.get(f"/api/v1/devices/{sensor.device_id}")).json()
    sensor.sequence, sensor.meter_wh = record["last_sequence"], record["last_meter_wh"]

    assert (await _post(app_client, [sensor.reading(0.2)])).status_code == 200
    record = (await app_client.get(f"/api/v1/devices/{sensor.device_id}")).json()
    assert record["last_meter_wh"] == 500


# ── V2: tamper ─────────────────────────────────────────────────────────────

async def test_opening_the_enclosure_withdraws_confirmation(app_client, admin_token, make_user):
    sensor = SimulatedSensor("PLB-TAMPER", version=V2)
    await _register(app_client, admin_token, make_user, sensor)
    await app_client.post(
        f"/api/admin/devices/{sensor.device_id}/verify",
        headers=auth(admin_token), json={"note": "Site visit, meter sealed"},
    )
    assert (await _post(app_client, [sensor.reading(0.2)])).status_code == 200

    sensor.tamper_count = 1
    response = await _post(app_client, [sensor.reading(0.2)])
    assert response.status_code == 200
    assert response.json()["installation_confirmed"] is False

    record = (await app_client.get(f"/api/v1/devices/{sensor.device_id}")).json()
    assert record["verified"] is False
    assert record["tamper_count"] == 1

    events = (await app_client.get(
        "/api/admin/device-events", params={"device_id": sensor.device_id},
        headers=auth(admin_token),
    )).json()["events"]
    assert [e["kind"] for e in events] == ["tamper"]


async def test_credits_after_tampering_are_pending(app_client, admin_token, make_user):
    sensor = SimulatedSensor("PLB-TAMPER-CR", version=V2)
    await _register(app_client, admin_token, make_user, sensor)
    await app_client.post(
        f"/api/admin/devices/{sensor.device_id}/verify",
        headers=auth(admin_token), json={"note": "Site visit, meter sealed"},
    )

    sensor.tamper_count = 1
    needed_kwh = 1000 / 0.82
    response = await _post(app_client, [sensor.reading(round(needed_kwh + 1, 3))])
    assert response.json()["credits_issued"] == 1

    row = await database.database.fetch_one(
        "SELECT status FROM credits WHERE device_id = :d", {"d": sensor.device_id}
    )
    assert row["status"] == "pending"


async def test_the_tamper_counter_cannot_be_reset(app_client, admin_token, make_user):
    sensor = SimulatedSensor("PLB-TAMPER-RESET", version=V2)
    await _register(app_client, admin_token, make_user, sensor)
    sensor.tamper_count = 2
    assert (await _post(app_client, [sensor.reading(0.1)])).status_code == 200

    sensor.tamper_count = 0
    response = await _post(app_client, [sensor.reading(0.1)])
    assert response.status_code == 422
    assert _code(response) == "tamper_counter_regressed"


# ── The published spec ─────────────────────────────────────────────────────

async def test_the_spec_documents_both_versions(app_client):
    spec = (await app_client.get("/api/v1/spec")).json()
    assert spec["message_version"] == V2
    assert set(spec["supported_versions"]) == {attestation.MESSAGE_VERSION_V1, V2}
    assert spec["example_message"].endswith("meter_wh:1284610\ntamper_count:0")
    assert spec["limits"]["max_clock_skew_seconds"] == 300
