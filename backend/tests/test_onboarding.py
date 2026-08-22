"""
The newcomer path: sign up, pair a device, let it report, earn a credit.

Data flows the moment a device is paired, so a new seller is never blocked
waiting on an operator. Selling is what waits: a signed reading proves it came
from that device unaltered, but nothing about a signature shows the device is
pointed at a real solar array, so credits stay unsellable until someone has
confirmed the installation.
"""

import config
import database
from tests.conftest import auth
from tests.test_attestation import SimulatedSensor


async def _pair(app_client, installer_token, device_id):
    """Everything a seller and a device do between signup and first reading."""
    code = (await app_client.post(
        "/api/installer/enrollment-codes",
        headers=auth(installer_token),
        json={"label": "Rooftop", "location": "Nashik"},
    )).json()["enrollment_code"]

    sensor = SimulatedSensor(device_id)
    response = await app_client.post("/api/v1/devices/enroll", json={
        "enrollment_code": code,
        "device_id": device_id,
        "public_key": sensor.public_key,
    })
    return code, sensor, response


# ── Pairing ────────────────────────────────────────────────────────────────

async def test_a_new_seller_can_pair_a_device_unaided(app_client, make_user):
    """No operator involved between signing up and the device reporting."""
    token, _ = await make_user("installer")
    _, sensor, response = await _pair(app_client, token, "NEW-01")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "enrolled"
    assert body["verified"] is False
    assert body["public_key"].lower() == sensor.public_key.lower()


async def test_enrolment_needs_no_session(app_client, make_user):
    """A sensor cannot hold a login; the pairing code stands in for one."""
    token, _ = await make_user("installer")
    code = (await app_client.post(
        "/api/installer/enrollment-codes", headers=auth(token), json={},
    )).json()["enrollment_code"]

    sensor = SimulatedSensor("NEW-NOAUTH")
    app_client.cookies.clear()
    response = await app_client.post("/api/v1/devices/enroll", json={
        "enrollment_code": code, "device_id": "NEW-NOAUTH",
        "public_key": sensor.public_key,
    })
    assert response.status_code == 200


async def test_the_device_is_bound_to_the_seller_who_issued_the_code(
    app_client, make_user
):
    token, installer = await make_user("installer")
    await _pair(app_client, token, "NEW-OWNED")

    row = await database.database.fetch_one(
        "SELECT owner_user_id, enrolled_via FROM devices WHERE device_id = 'NEW-OWNED'")
    assert row["owner_user_id"] == installer["id"]
    assert row["enrolled_via"] == "pairing_code"


async def test_a_pairing_code_works_only_once(app_client, make_user):
    """Otherwise one leaked code would enrol an unlimited number of devices."""
    token, _ = await make_user("installer")
    code, _, first = await _pair(app_client, token, "NEW-ONCE")
    assert first.status_code == 200

    intruder = SimulatedSensor("NEW-ONCE-2")
    second = await app_client.post("/api/v1/devices/enroll", json={
        "enrollment_code": code, "device_id": "NEW-ONCE-2",
        "public_key": intruder.public_key,
    })
    assert second.status_code == 409


async def test_an_unknown_code_is_refused(app_client):
    sensor = SimulatedSensor("NEW-BADCODE")
    response = await app_client.post("/api/v1/devices/enroll", json={
        "enrollment_code": "CTN-DEAD-BEEF", "device_id": "NEW-BADCODE",
        "public_key": sensor.public_key,
    })
    assert response.status_code == 404


async def test_an_expired_code_is_refused(app_client, make_user):
    import time

    token, _ = await make_user("installer")
    code = (await app_client.post(
        "/api/installer/enrollment-codes", headers=auth(token), json={},
    )).json()["enrollment_code"]

    await database.database.execute(
        query="UPDATE device_enrollments SET expires_at = :past WHERE code = :code",
        values={"past": time.time() - 1, "code": code},
    )

    sensor = SimulatedSensor("NEW-EXPIRED")
    response = await app_client.post("/api/v1/devices/enroll", json={
        "enrollment_code": code, "device_id": "NEW-EXPIRED",
        "public_key": sensor.public_key,
    })
    assert response.status_code == 410


async def test_a_taken_device_id_is_refused(app_client, make_user):
    token, _ = await make_user("installer")
    await _pair(app_client, token, "NEW-DUP")

    code = (await app_client.post(
        "/api/installer/enrollment-codes", headers=auth(token), json={},
    )).json()["enrollment_code"]
    other = SimulatedSensor("NEW-DUP")
    response = await app_client.post("/api/v1/devices/enroll", json={
        "enrollment_code": code, "device_id": "NEW-DUP",
        "public_key": other.public_key,
    })
    assert response.status_code == 409


async def test_a_seller_sees_their_codes_and_status(app_client, make_user):
    token, _ = await make_user("installer")
    await _pair(app_client, token, "NEW-LIST")

    codes = (await app_client.get(
        "/api/installer/enrollment-codes", headers=auth(token))).json()["codes"]
    redeemed = next(c for c in codes if c["device_id"] == "NEW-LIST")
    assert redeemed["status"] == "redeemed"


# ── Reporting straight after pairing ───────────────────────────────────────

async def test_a_paired_device_reports_immediately(app_client, make_user):
    """The point of self-service: nothing stands between pairing and data."""
    token, _ = await make_user("installer")
    _, sensor, _ = await _pair(app_client, token, "NEW-REPORT")

    response = await app_client.post(
        "/api/v1/readings", json={"readings": [sensor.reading(0.61)]})
    assert response.status_code == 200, response.text
    assert response.json()["attested"] is True


async def test_generation_becomes_a_credit_at_one_tonne(app_client, make_user):
    """Sign up, pair, generate, earn — the whole newcomer journey."""
    token, _ = await make_user("installer")
    _, sensor, _ = await _pair(app_client, token, "NEW-TONNE")

    per_reading = config.KG_CO2_PER_CREDIT / config.EMISSION_FACTOR_KG_PER_KWH / 2 + 1
    response = await app_client.post("/api/v1/readings", json={
        "readings": [sensor.reading(round(per_reading, 6)) for _ in range(2)]
    })
    assert response.json()["credits_issued"] == 1

    dashboard = (await app_client.get(
        "/api/installer/dashboard", headers=auth(token))).json()
    assert dashboard["stats"]["total_credits"] == 1


# ── Selling waits for a human ──────────────────────────────────────────────

async def test_credits_from_an_unverified_device_are_pending(app_client, make_user):
    """
    A signature proves origin, not that the meter is measuring anything real —
    a bench-top ESP32 signs perfectly well.
    """
    token, _ = await make_user("installer")
    _, sensor, _ = await _pair(app_client, token, "NEW-PENDING")

    per_reading = config.KG_CO2_PER_CREDIT / config.EMISSION_FACTOR_KG_PER_KWH / 2 + 1
    await app_client.post("/api/v1/readings", json={
        "readings": [sensor.reading(round(per_reading, 6)) for _ in range(2)]})

    row = await database.database.fetch_one(
        "SELECT status FROM credits WHERE device_id = 'NEW-PENDING'")
    assert row["status"] == "pending"

    dashboard = (await app_client.get(
        "/api/installer/dashboard", headers=auth(token))).json()
    assert dashboard["credits_by_status"]["pending"] == 1
    assert dashboard["sell_threshold"]["eligible"] is False


async def test_a_pending_credit_cannot_be_listed(app_client, make_user):
    token, _ = await make_user("installer")
    _, sensor, _ = await _pair(app_client, token, "NEW-NOSELL")

    per_reading = config.KG_CO2_PER_CREDIT / config.EMISSION_FACTOR_KG_PER_KWH / 2 + 1
    await app_client.post("/api/v1/readings", json={
        "readings": [sensor.reading(round(per_reading, 6)) for _ in range(2)]})

    row = await database.database.fetch_one(
        "SELECT id FROM credits WHERE device_id = 'NEW-NOSELL'")
    await database.database.execute(
        query="UPDATE users SET wallet_address = :w WHERE id = "
              "(SELECT owner_user_id FROM credits WHERE id = :cid)",
        values={"w": "0x" + "1a" * 20, "cid": row["id"]},
    )

    response = await app_client.post(
        "/api/installer/sell", headers=auth(token), json={"credit_ids": [row["id"]]})
    assert response.status_code == 400
    assert "verified" in response.json()["detail"]


async def test_verifying_the_device_releases_its_credits(
    app_client, admin_token, make_user
):
    """Confirmation is about the installation, so earlier readings stay valid."""
    token, _ = await make_user("installer")
    _, sensor, _ = await _pair(app_client, token, "NEW-RELEASE")

    per_reading = config.KG_CO2_PER_CREDIT / config.EMISSION_FACTOR_KG_PER_KWH / 2 + 1
    await app_client.post("/api/v1/readings", json={
        "readings": [sensor.reading(round(per_reading, 6)) for _ in range(2)]})

    response = await app_client.post(
        "/api/admin/devices/NEW-RELEASE/verify",
        headers=auth(admin_token),
        json={"note": "Site visit confirmed a 5kW rooftop array"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["credits_released"] == 1

    row = await database.database.fetch_one(
        "SELECT status FROM credits WHERE device_id = 'NEW-RELEASE'")
    assert row["status"] == "verified"


async def test_verification_requires_an_explanation(app_client, admin_token, make_user):
    token, _ = await make_user("installer")
    await _pair(app_client, token, "NEW-NONOTE")

    response = await app_client.post(
        "/api/admin/devices/NEW-NONOTE/verify",
        headers=auth(admin_token), json={"note": ""})
    assert response.status_code == 400


async def test_a_device_cannot_be_verified_twice(app_client, admin_token, make_user):
    token, _ = await make_user("installer")
    await _pair(app_client, token, "NEW-TWICE")

    body = {"note": "Confirmed against the commissioning invoice"}
    first = await app_client.post(
        "/api/admin/devices/NEW-TWICE/verify", headers=auth(admin_token), json=body)
    assert first.status_code == 200
    second = await app_client.post(
        "/api/admin/devices/NEW-TWICE/verify", headers=auth(admin_token), json=body)
    assert second.status_code == 409


async def test_only_an_admin_can_verify_a_device(app_client, make_user):
    """A seller confirming their own installation would defeat the check."""
    token, _ = await make_user("installer")
    await _pair(app_client, token, "NEW-SELFVERIFY")

    response = await app_client.post(
        "/api/admin/devices/NEW-SELFVERIFY/verify",
        headers=auth(token), json={"note": "verifying myself"})
    assert response.status_code == 403

    row = await database.database.fetch_one(
        "SELECT verified FROM devices WHERE device_id = 'NEW-SELFVERIFY'")
    assert row["verified"] == 0


async def test_verification_is_audited(app_client, admin_token, make_user):
    token, _ = await make_user("installer")
    await _pair(app_client, token, "NEW-AUDIT")
    await app_client.post(
        "/api/admin/devices/NEW-AUDIT/verify",
        headers=auth(admin_token), json={"note": "Meter serial matched the invoice"})

    logs = (await app_client.get(
        "/api/admin/audit-log", headers=auth(admin_token))).json()["logs"]
    entry = next(e for e in logs if e["target_id"] == "NEW-AUDIT")
    assert entry["action"] == "verify_device"
    assert "invoice" in entry["reason"]


# ── The spreadsheet path, for sellers without a sensor ─────────────────────

def _csv(rows: str) -> dict:
    return {"file": ("readings.csv", rows, "text/csv")}


async def test_a_seller_can_add_a_meter_and_upload_readings(app_client, make_user):
    """Someone with no ESP32 still needs a way in at this stage."""
    token, _ = await make_user("installer")

    added = await app_client.post(
        "/api/installer/devices", headers=auth(token),
        json={"device_id": "METER-01", "location": "Pune"})
    assert added.status_code == 200, added.text
    assert added.json()["attestation_enabled"] is False

    response = await app_client.post(
        "/api/installer/upload-readings", headers=auth(token),
        files=_csv("device_id,timestamp,delta_kwh\n"
                   "METER-01,2026-03-01 06:00:00,610\n"
                   "METER-01,2026-03-01 06:15:00,610\n"))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["readings_added"] == 2
    assert body["credits_issued"] == 1
    assert body["attested"] is False


async def test_uploaded_readings_are_not_attested(app_client, make_user):
    """The weaker guarantee has to stay visible, not be quietly equated."""
    token, _ = await make_user("installer")
    await app_client.post("/api/installer/devices", headers=auth(token),
                          json={"device_id": "METER-PLAIN", "location": "X"})
    await app_client.post(
        "/api/installer/upload-readings", headers=auth(token),
        files=_csv("device_id,timestamp,delta_kwh\nMETER-PLAIN,2026-03-02 06:00:00,10\n"))

    row = await database.database.fetch_one(
        "SELECT device_signature FROM generation_readings WHERE device_id = 'METER-PLAIN'")
    assert row["device_signature"] is None


async def test_a_seller_cannot_upload_for_someone_elses_meter(app_client, make_user):
    """Otherwise generation could be credited to the wrong account."""
    owner_token, _ = await make_user("installer")
    await app_client.post("/api/installer/devices", headers=auth(owner_token),
                          json={"device_id": "METER-OWNED", "location": "X"})

    intruder_token, _ = await make_user("installer")
    await app_client.post("/api/installer/devices", headers=auth(intruder_token),
                          json={"device_id": "METER-OTHER", "location": "X"})

    response = await app_client.post(
        "/api/installer/upload-readings", headers=auth(intruder_token),
        files=_csv("device_id,timestamp,delta_kwh\nMETER-OWNED,2026-03-03 06:00:00,10\n"))
    assert response.status_code == 400
    assert "not yours" in response.text


async def test_uploading_without_a_meter_explains_what_to_do(app_client, make_user):
    token, _ = await make_user("installer")
    response = await app_client.post(
        "/api/installer/upload-readings", headers=auth(token),
        files=_csv("device_id,timestamp,delta_kwh\nX,2026-03-04 06:00:00,10\n"))
    assert response.status_code == 400
    assert "Add a meter" in response.json()["detail"]


async def test_a_duplicate_meter_name_is_refused(app_client, make_user):
    token, _ = await make_user("installer")
    body = {"device_id": "METER-DUP", "location": "X"}
    assert (await app_client.post("/api/installer/devices",
                                  headers=auth(token), json=body)).status_code == 200
    assert (await app_client.post("/api/installer/devices",
                                  headers=auth(token), json=body)).status_code == 409


async def test_uploaded_credits_are_also_pending(app_client, make_user):
    """An uploaded number is an assertion, so it is gated like a self-enrolled one."""
    token, _ = await make_user("installer")
    await app_client.post("/api/installer/devices", headers=auth(token),
                          json={"device_id": "METER-PENDING", "location": "X"})
    await app_client.post(
        "/api/installer/upload-readings", headers=auth(token),
        files=_csv("device_id,timestamp,delta_kwh\n"
                   "METER-PENDING,2026-03-05 06:00:00,610\n"
                   "METER-PENDING,2026-03-05 06:15:00,610\n"))

    row = await database.database.fetch_one(
        "SELECT status FROM credits WHERE device_id = 'METER-PENDING'")
    assert row["status"] == "pending"


# ── Reading whatever shape the meter exported ──────────────────────────────

async def _meter(app_client, token, device_id="ANY-01"):
    await app_client.post("/api/installer/devices", headers=auth(token),
                          json={"device_id": device_id, "location": "X"})
    return device_id


async def _preview(app_client, token, text):
    return await app_client.post(
        "/api/installer/upload-readings/preview", headers=auth(token),
        files={"file": ("export.csv", text, "text/csv")})


async def _upload(app_client, token, text, device_id=""):
    return await app_client.post(
        "/api/installer/upload-readings", headers=auth(token),
        files={"file": ("export.csv", text, "text/csv")},
        data={"device_id": device_id})


async def test_a_cumulative_export_is_differenced(app_client, make_user):
    """
    Inverters usually report a lifetime total. Treating that as per-interval
    generation would credit the entire history on every single row.
    """
    token, _ = await make_user("installer")
    device = await _meter(app_client, token, "CUM-01")

    text = ("Date/Time,Total Yield (kWh)\n"
            "2026-05-01 06:00:00,1000.0\n"
            "2026-05-01 06:15:00,1000.5\n"
            "2026-05-01 06:30:00,1001.0\n")

    preview = (await _preview(app_client, token, text)).json()
    assert preview["cumulative"] is True
    assert preview["total_kwh"] == 1.0          # not 3001.5

    response = await _upload(app_client, token, text, device)
    assert response.status_code == 200, response.text


async def test_power_readings_are_converted_to_energy(app_client, make_user):
    """Some exports give kW at an instant, which is not energy until timed."""
    token, _ = await make_user("installer")
    device = await _meter(app_client, token, "PWR-01")

    text = ("Time,AC Power (kW)\n"
            "2026-05-02T06:00:00,2.0\n"
            "2026-05-02T06:15:00,2.0\n")

    preview = (await _preview(app_client, token, text)).json()
    assert preview["value_kind"] == "power"
    assert preview["interval_minutes"] == 15
    assert preview["total_kwh"] == 1.0          # 2 kW * 0.25 h * 2

    assert (await _upload(app_client, token, text, device)).status_code == 200


async def test_watt_hours_are_scaled(app_client, make_user):
    token, _ = await make_user("installer")
    device = await _meter(app_client, token, "WH-01")

    text = "timestamp,Energy (Wh)\n2026-05-03 06:00:00,1000\n2026-05-03 06:15:00,500\n"
    preview = (await _preview(app_client, token, text)).json()
    assert preview["unit"] == "Wh"
    assert preview["total_kwh"] == 1.5

    assert (await _upload(app_client, token, text, device)).status_code == 200


async def test_semicolons_and_european_dates_are_read(app_client, make_user):
    token, _ = await make_user("installer")
    device = await _meter(app_client, token, "EU-01")

    text = "Datum;Ertrag kWh\n01/05/2026 06:00;0.5\n01/05/2026 06:15;0.7\n"
    preview = (await _preview(app_client, token, text)).json()
    assert preview["rows"] == 2
    assert (await _upload(app_client, token, text, device)).status_code == 200


async def test_a_device_column_is_used_when_present(app_client, make_user):
    token, _ = await make_user("installer")
    await _meter(app_client, token, "INV-A")
    await _meter(app_client, token, "INV-B")

    text = ("Serial,Date,Generation kWh\n"
            "INV-A,2026-05-04 06:00:00,0.5\n"
            "INV-B,2026-05-04 06:00:00,0.7\n")

    preview = (await _preview(app_client, token, text)).json()
    assert preview["device_column"] == "Serial"
    assert preview["device_required"] is False

    assert (await _upload(app_client, token, text)).status_code == 200


async def test_a_file_without_a_device_column_must_be_assigned(app_client, make_user):
    """Nothing in the file says which meter it is, so it cannot be guessed."""
    token, _ = await make_user("installer")
    await _meter(app_client, token, "SOLO-01")

    text = "Time,kWh\n2026-05-05 06:00:00,0.5\n2026-05-05 06:15:00,0.6\n"
    preview = (await _preview(app_client, token, text)).json()
    assert preview["device_required"] is True

    unassigned = await _upload(app_client, token, text)
    assert unassigned.status_code == 400
    assert "choose which meter" in unassigned.json()["detail"]

    assert (await _upload(app_client, token, text, "SOLO-01")).status_code == 200


async def test_readings_for_another_sellers_meter_are_refused(app_client, make_user):
    owner_token, _ = await make_user("installer")
    await _meter(app_client, owner_token, "THEIRS-01")

    other_token, _ = await make_user("installer")
    await _meter(app_client, other_token, "MINE-01")

    text = "Serial,Time,kWh\nTHEIRS-01,2026-05-06 06:00:00,0.5\n"
    response = await _upload(app_client, other_token, text)
    assert response.status_code == 400
    assert "not yours" in response.json()["detail"]


async def test_a_file_with_no_time_column_is_explained(app_client, make_user):
    token, _ = await make_user("installer")
    await _meter(app_client, token, "NOTIME-01")

    response = await _preview(app_client, token, "name,amount\nfoo,1\nbar,2\n")
    assert response.status_code == 400
    assert "dates or times" in response.json()["detail"]


async def test_preview_writes_nothing(app_client, make_user):
    """The point of a preview is that a wrong guess costs nothing."""
    token, _ = await make_user("installer")
    await _meter(app_client, token, "DRY-01")

    text = "Time,kWh\n2026-05-07 06:00:00,0.5\n2026-05-07 06:15:00,0.6\n"
    assert (await _preview(app_client, token, text)).status_code == 200

    row = await database.database.fetch_one(
        "SELECT COUNT(*) AS c FROM generation_readings WHERE device_id = 'DRY-01'")
    assert row["c"] == 0
