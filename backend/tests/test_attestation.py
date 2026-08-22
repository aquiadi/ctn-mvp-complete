"""
Device attestation: the pipeline the whole product rests on.

A credit is only trustworthy if the reading behind it provably came from the
device that measured it. These tests drive a simulated sensor — a keypair and
the reference signing routine — and assert that forged, tampered, and replayed
packets are refused.
"""

import attestation
import config
import database
from tests.conftest import auth


class SimulatedSensor:
    """
    Stands in for firmware: holds a private key, counts its own sequence, and
    signs every reading it emits. The private key never reaches the server.
    """

    def __init__(self, device_id: str):
        self.device_id = device_id
        self.private_key, self.public_key = attestation.generate_device_keypair()
        self.sequence = 0

    def reading(self, delta_kwh: float, timestamp: str = None, sequence: int = None) -> dict:
        if sequence is None:
            self.sequence += 1
            sequence = self.sequence
        timestamp = timestamp or f"2026-05-01T{sequence % 24:02d}:00:00Z"

        return {
            "device_id": self.device_id,
            "sequence": sequence,
            "timestamp": timestamp,
            "delta_kwh": delta_kwh,
            "signature": attestation.sign_reading(
                self.private_key, self.device_id, sequence, timestamp, delta_kwh
            ),
        }


async def _register(app_client, admin_token, make_user, sensor, with_key=True):
    """Provision the sensor's device so it is allowed to report."""
    _, installer = await make_user("installer")
    response = await app_client.post(
        "/api/admin/devices",
        headers=auth(admin_token),
        json={
            "device_id": sensor.device_id,
            "owner_email": installer["email"],
            "location": "Test Site",
            "public_key": sensor.public_key if with_key else "",
        },
    )
    assert response.status_code == 200, response.text
    return installer


async def _post(app_client, readings):
    return await app_client.post("/api/v1/readings", json={"readings": readings})


# ── The canonical message ──────────────────────────────────────────────────

def test_the_signed_message_is_stable():
    """Firmware must be able to reproduce this byte for byte, in any language."""
    message = attestation.canonical_message("INV-1", 42, "2026-01-01T00:00:00Z", 0.61)
    assert message == (
        "CTN-READING-V1\n"
        "device:INV-1\n"
        "sequence:42\n"
        "timestamp:2026-01-01T00:00:00Z\n"
        "delta_kwh:0.610000"
    )


def test_energy_is_signed_at_fixed_precision():
    """Float formatting must not vary, or a device and the server disagree."""
    assert "delta_kwh:0.610000" in attestation.canonical_message("D", 1, "T", 0.61)
    assert "delta_kwh:0.610000" in attestation.canonical_message("D", 1, "T", 0.6100000000000001)


def test_every_signed_field_is_covered():
    """Any field left out could be altered in transit without breaking the signature."""
    base = attestation.canonical_message("D1", 1, "T1", 1.0)
    for altered in (
        attestation.canonical_message("D2", 1, "T1", 1.0),
        attestation.canonical_message("D1", 2, "T1", 1.0),
        attestation.canonical_message("D1", 1, "T2", 1.0),
        attestation.canonical_message("D1", 1, "T1", 2.0),
    ):
        assert altered != base


# ── Accepting genuine readings ─────────────────────────────────────────────

async def test_a_signed_reading_is_accepted(app_client, admin_token, make_user):
    sensor = SimulatedSensor("ATT-OK")
    await _register(app_client, admin_token, make_user, sensor)

    response = await _post(app_client, [sensor.reading(0.61)])
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["attested"] is True
    assert body["readings_accepted"] == 1


async def test_ingestion_needs_no_session(app_client, admin_token, make_user):
    """
    The signature is the credential.

    A sensor on a roof cannot hold a login, and a shared API key would be
    extractable from firmware. Nothing here is sent that an attacker could reuse.
    """
    sensor = SimulatedSensor("ATT-NOAUTH")
    await _register(app_client, admin_token, make_user, sensor)

    app_client.cookies.clear()
    response = await _post(app_client, [sensor.reading(0.5)])
    assert response.status_code == 200


async def test_signed_readings_accumulate_into_a_credit(app_client, admin_token, make_user):
    """The full thesis: attested measurements become an issued credit."""
    sensor = SimulatedSensor("ATT-CREDIT")
    await _register(app_client, admin_token, make_user, sensor)

    # 1000 kg CO2 / 0.82 == 1219.52 kWh, sent as two intervals.
    needed = config.KG_CO2_PER_CREDIT / config.EMISSION_FACTOR_KG_PER_KWH
    response = await _post(app_client, [
        sensor.reading(round(needed / 2 + 1, 6)),
        sensor.reading(round(needed / 2 + 1, 6)),
    ])
    assert response.status_code == 200, response.text
    assert response.json()["credits_issued"] == 1


async def test_a_batch_is_accepted(app_client, admin_token, make_user):
    """Devices buffer while offline, so batches are normal rather than exceptional."""
    sensor = SimulatedSensor("ATT-BATCH")
    await _register(app_client, admin_token, make_user, sensor)

    response = await _post(app_client, [sensor.reading(0.4) for _ in range(10)])
    assert response.status_code == 200, response.text
    assert response.json()["readings_accepted"] == 10
    assert response.json()["sequence"] == 10


# ── Refusing forged and tampered readings ──────────────────────────────────

async def test_a_reading_signed_by_the_wrong_key_is_refused(
    app_client, admin_token, make_user
):
    """Someone else's valid signature must not authorise this device."""
    sensor = SimulatedSensor("ATT-WRONGKEY")
    await _register(app_client, admin_token, make_user, sensor)

    impostor = SimulatedSensor("ATT-WRONGKEY")  # same id, different key
    response = await _post(app_client, [impostor.reading(0.61)])

    assert response.status_code == 401
    assert "attestation" in response.json()["detail"].lower()


async def test_altering_the_energy_after_signing_is_refused(
    app_client, admin_token, make_user
):
    """The central attack: inflate generation to mint credits that were not earned."""
    sensor = SimulatedSensor("ATT-TAMPER")
    await _register(app_client, admin_token, make_user, sensor)

    reading = sensor.reading(0.61)
    reading["delta_kwh"] = 9999.0  # signature still covers 0.61

    response = await _post(app_client, [reading])
    assert response.status_code == 401

    row = await database.database.fetch_one(
        "SELECT COUNT(*) AS c FROM generation_readings WHERE device_id = 'ATT-TAMPER'")
    assert row["c"] == 0


async def test_altering_the_timestamp_is_refused(app_client, admin_token, make_user):
    sensor = SimulatedSensor("ATT-TIME")
    await _register(app_client, admin_token, make_user, sensor)

    reading = sensor.reading(0.61)
    reading["timestamp"] = "2030-01-01T00:00:00Z"
    assert (await _post(app_client, [reading])).status_code == 401


async def test_a_garbage_signature_is_refused(app_client, admin_token, make_user):
    sensor = SimulatedSensor("ATT-GARBAGE")
    await _register(app_client, admin_token, make_user, sensor)

    reading = sensor.reading(0.61)
    reading["signature"] = "0x" + "ab" * 65
    assert (await _post(app_client, [reading])).status_code == 401


async def test_an_unregistered_device_is_refused(app_client):
    sensor = SimulatedSensor("ATT-UNKNOWN")
    response = await _post(app_client, [sensor.reading(0.61)])
    assert response.status_code == 404


async def test_a_device_without_a_key_cannot_attest(app_client, admin_token, make_user):
    """A device onboarded without a key must not be able to claim attestation."""
    sensor = SimulatedSensor("ATT-NOKEY")
    await _register(app_client, admin_token, make_user, sensor, with_key=False)

    response = await _post(app_client, [sensor.reading(0.61)])
    assert response.status_code == 409
    assert "signing key" in response.json()["detail"]


# ── Replay protection ──────────────────────────────────────────────────────

async def test_a_captured_packet_cannot_be_replayed(app_client, admin_token, make_user):
    """
    A signature stays valid forever, so without sequencing an attacker who
    captured one packet could resubmit it endlessly and mint unlimited credits
    from a single genuine reading.
    """
    sensor = SimulatedSensor("ATT-REPLAY")
    await _register(app_client, admin_token, make_user, sensor)

    reading = sensor.reading(0.61)
    assert (await _post(app_client, [reading])).status_code == 200

    replayed = await _post(app_client, [reading])
    assert replayed.status_code == 409
    assert "replay" in replayed.json()["detail"].lower()


async def test_sequences_must_move_forward(app_client, admin_token, make_user):
    sensor = SimulatedSensor("ATT-BACKWARD")
    await _register(app_client, admin_token, make_user, sensor)

    await _post(app_client, [sensor.reading(0.5) for _ in range(5)])
    stale = sensor.reading(0.5, sequence=3)
    assert (await _post(app_client, [stale])).status_code == 409


async def test_a_batch_must_be_ordered(app_client, admin_token, make_user):
    sensor = SimulatedSensor("ATT-ORDER")
    await _register(app_client, admin_token, make_user, sensor)

    first, second = sensor.reading(0.5), sensor.reading(0.5)
    assert (await _post(app_client, [second, first])).status_code == 400


async def test_a_batch_cannot_repeat_a_sequence(app_client, admin_token, make_user):
    sensor = SimulatedSensor("ATT-DUPSEQ")
    await _register(app_client, admin_token, make_user, sensor)

    reading = sensor.reading(0.5)
    assert (await _post(app_client, [reading, dict(reading)])).status_code == 400


async def test_a_rejected_batch_writes_nothing(app_client, admin_token, make_user):
    """A partial apply would leave a gap indistinguishable from missing generation."""
    sensor = SimulatedSensor("ATT-ATOMIC")
    await _register(app_client, admin_token, make_user, sensor)

    good = sensor.reading(0.5)
    bad = sensor.reading(0.5)
    bad["delta_kwh"] = 500.0  # breaks its signature

    assert (await _post(app_client, [good, bad])).status_code == 401

    row = await database.database.fetch_one(
        "SELECT COUNT(*) AS c FROM generation_readings WHERE device_id = 'ATT-ATOMIC'")
    assert row["c"] == 0


async def test_a_batch_must_come_from_one_device(app_client, admin_token, make_user):
    sensor = SimulatedSensor("ATT-MIXED")
    await _register(app_client, admin_token, make_user, sensor)

    other = SimulatedSensor("ATT-MIXED-2")
    assert (await _post(app_client, [sensor.reading(0.5), other.reading(0.5)])).status_code == 400


# ── Public verification ────────────────────────────────────────────────────

async def test_a_proof_can_be_verified_independently(app_client, admin_token, make_user):
    """
    The point of the proof endpoint: a third party recovers the signer from the
    published message and signature, without trusting anything this server says.
    """
    sensor = SimulatedSensor("ATT-PROOF")
    await _register(app_client, admin_token, make_user, sensor)
    await _post(app_client, [sensor.reading(0.61)])

    row = await database.database.fetch_one(
        "SELECT reading_id FROM generation_readings WHERE device_id = 'ATT-PROOF'")
    proof = (await app_client.get(f"/api/v1/readings/{row['reading_id']}/proof")).json()

    assert proof["attested"] is True
    assert proof["signature_valid"] is True

    # Verify it ourselves from the published fields alone.
    recovered = attestation.recover_signer(proof["signed_message"], proof["device_signature"])
    assert recovered.lower() == sensor.public_key.lower()
    assert recovered.lower() == proof["device_public_key"].lower()


async def test_an_imported_reading_is_not_presented_as_attested(
    app_client, admin_token, make_user
):
    """
    CSV rows carry only a server-computed hash, which proves nothing about
    origin. The proof must say so rather than implying the data was signed.
    """
    _, installer = await make_user("installer")
    await app_client.post(
        "/api/admin/devices", headers=auth(admin_token),
        json={"device_id": "CSV-PLAIN", "owner_email": installer["email"], "location": "X"})
    await app_client.post(
        "/api/admin/ingest-csv", headers=auth(admin_token),
        files={"file": ("r.csv",
                        "device_id,timestamp,delta_kwh\nCSV-PLAIN,2026-08-01 06:00:00,10\n",
                        "text/csv")})

    row = await database.database.fetch_one(
        "SELECT reading_id FROM generation_readings WHERE device_id = 'CSV-PLAIN'")
    proof = (await app_client.get(f"/api/v1/readings/{row['reading_id']}/proof")).json()

    assert proof["attested"] is False
    assert "signature_valid" not in proof
    assert "imported" in proof["detail"]


async def test_the_device_record_is_public(app_client, admin_token, make_user):
    """Verifying someone's credits requires their key, so it cannot be private."""
    sensor = SimulatedSensor("ATT-PUBLIC")
    await _register(app_client, admin_token, make_user, sensor)
    await _post(app_client, [sensor.reading(0.61)])

    app_client.cookies.clear()
    body = (await app_client.get("/api/v1/devices/ATT-PUBLIC")).json()
    assert body["public_key"].lower() == sensor.public_key.lower()
    assert body["attestation_enabled"] is True
    assert body["readings_attested"] == 1


async def test_the_signing_spec_is_published(app_client):
    """Firmware authors need the contract, served from the code that enforces it."""
    spec = (await app_client.get("/api/v1/spec")).json()
    assert spec["message_version"] == attestation.MESSAGE_VERSION
    assert spec["kwh_decimals"] == attestation.KWH_DECIMALS
    assert "EIP-191" in spec["signature_scheme"]
