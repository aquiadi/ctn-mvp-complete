"""Admin visibility, device registration, CSV ingestion, and the audit trail."""

import config
from tests.conftest import auth


def _csv(rows: str) -> dict:
    return {"file": ("readings.csv", rows, "text/csv")}


async def test_overview_reports_platform_totals(app_client, admin_token):
    body = (await app_client.get("/api/admin/overview", headers=auth(admin_token))).json()
    assert body["users"]["admins"] >= 1
    # SUM over an empty table yields NULL; these must be numbers for the UI.
    assert isinstance(body["transactions"]["completed"], int)
    assert isinstance(body["transactions"]["total_inr"], (int, float))


async def test_device_registration_requires_admin(app_client, make_user):
    token, _ = await make_user("installer")
    response = await app_client.post(
        "/api/admin/devices",
        headers=auth(token),
        json={"device_id": "NOPE", "owner_email": "x@test.local", "location": "X"},
    )
    assert response.status_code == 403


async def test_a_device_can_be_registered_to_an_installer(app_client, admin_token, make_user):
    _, installer = await make_user("installer")
    response = await app_client.post(
        "/api/admin/devices",
        headers=auth(admin_token),
        json={"device_id": "REG-1", "owner_email": installer["email"], "location": "Pune"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["device_id"] == "REG-1"


async def test_registered_devices_are_listed(app_client, admin_token, make_user):
    """The admin panel needs to show what is already connected, not just add more."""
    _, installer = await make_user("installer")
    await app_client.post(
        "/api/admin/devices",
        headers=auth(admin_token),
        json={"device_id": "LIST-1", "owner_email": installer["email"], "location": "Chennai"},
    )

    body = (await app_client.get("/api/admin/devices", headers=auth(admin_token))).json()
    entry = next(d for d in body["devices"] if d["device_id"] == "LIST-1")
    assert entry["owner_email"] == installer["email"]
    assert entry["location"] == "Chennai"
    assert entry["reading_count"] == 0
    assert body["total"] >= 1


async def test_the_device_list_is_admin_only(app_client, make_user):
    token, _ = await make_user("installer")
    assert (await app_client.get("/api/admin/devices", headers=auth(token))).status_code == 403


async def test_health_separates_contract_total_from_platform_mints(app_client, admin_token):
    """
    The contract counter is cumulative across every deployment that has used it,
    so a non-zero value there does not mean this platform minted anything.
    """
    import database

    body = (await app_client.get("/api/admin/system-health", headers=auth(admin_token))).json()
    minted = await database.database.fetch_one(
        "SELECT COUNT(*) AS n FROM credits WHERE on_chain_id IS NOT NULL"
    )
    assert body["minted_by_this_platform"] == minted["n"]
    assert "total_on_chain_credits" not in body


async def test_a_duplicate_device_is_rejected(app_client, admin_token, make_user):
    _, installer = await make_user("installer")
    payload = {"device_id": "REG-DUP", "owner_email": installer["email"], "location": "Pune"}

    assert (await app_client.post("/api/admin/devices", headers=auth(admin_token), json=payload)).status_code == 200
    repeat = await app_client.post("/api/admin/devices", headers=auth(admin_token), json=payload)
    assert repeat.status_code == 409


async def test_a_device_cannot_be_registered_to_a_buyer(app_client, admin_token, make_user):
    _, buyer = await make_user("buyer")
    response = await app_client.post(
        "/api/admin/devices",
        headers=auth(admin_token),
        json={"device_id": "REG-BUYER", "owner_email": buyer["email"], "location": "Pune"},
    )
    assert response.status_code == 400


async def test_a_device_for_an_unknown_email_is_rejected(app_client, admin_token):
    response = await app_client.post(
        "/api/admin/devices",
        headers=auth(admin_token),
        json={"device_id": "REG-GHOST", "owner_email": "ghost@test.local", "location": "Pune"},
    )
    assert response.status_code == 404


# ── CSV ingestion ──────────────────────────────────────────────────────────

async def test_csv_ingestion_issues_credits(app_client, admin_token, make_user):
    _, installer = await make_user("installer")
    await app_client.post(
        "/api/admin/devices",
        headers=auth(admin_token),
        json={"device_id": "CSV-1", "owner_email": installer["email"], "location": "Pune"},
    )

    response = await app_client.post(
        "/api/admin/ingest-csv",
        headers=auth(admin_token),
        files=_csv(
            "device_id,timestamp,delta_kwh\n"
            "CSV-1,2025-06-01 06:00:00,610\n"
            "CSV-1,2025-06-01 06:15:00,610\n"
        ),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["readings_added"] == 2
    assert body["credits_issued"] == 1


async def test_reuploading_a_csv_changes_nothing(app_client, admin_token, make_user):
    _, installer = await make_user("installer")
    await app_client.post(
        "/api/admin/devices",
        headers=auth(admin_token),
        json={"device_id": "CSV-2", "owner_email": installer["email"], "location": "Pune"},
    )
    rows = "device_id,timestamp,delta_kwh\nCSV-2,2025-06-02 06:00:00,610\nCSV-2,2025-06-02 06:15:00,610\n"

    await app_client.post("/api/admin/ingest-csv", headers=auth(admin_token), files=_csv(rows))
    repeat = await app_client.post(
        "/api/admin/ingest-csv", headers=auth(admin_token), files=_csv(rows)
    )
    assert repeat.json() == {
        "status": "ingested",
        "readings_added": 0,
        "rows_processed": 2,
        "credits_issued": 0,
        "message": "Added 0 reading(s) and issued 0 new credit(s).",
    }


async def test_missing_columns_are_reported(app_client, admin_token):
    response = await app_client.post(
        "/api/admin/ingest-csv",
        headers=auth(admin_token),
        files=_csv("device_id,timestamp\nX,2025-01-01 00:00:00\n"),
    )
    assert response.status_code == 400
    assert "delta_kwh" in response.text


async def test_an_invalid_row_rejects_the_whole_file(app_client, admin_token, make_user):
    """A partially applied import would leave the ledger in an unclear state."""
    _, installer = await make_user("installer")
    await app_client.post(
        "/api/admin/devices",
        headers=auth(admin_token),
        json={"device_id": "CSV-3", "owner_email": installer["email"], "location": "Pune"},
    )

    response = await app_client.post(
        "/api/admin/ingest-csv",
        headers=auth(admin_token),
        files=_csv(
            "device_id,timestamp,delta_kwh\n"
            "CSV-3,2025-06-03 06:00:00,610\n"
            "CSV-3,2025-06-03 06:15:00,not-a-number\n"
        ),
    )
    assert response.status_code == 400

    # The valid row must not have been stored.
    import database
    row = await database.database.fetch_one(
        "SELECT COUNT(*) AS cnt FROM generation_readings WHERE device_id = 'CSV-3'"
    )
    assert row["cnt"] == 0


async def test_an_unknown_device_is_rejected(app_client, admin_token):
    response = await app_client.post(
        "/api/admin/ingest-csv",
        headers=auth(admin_token),
        files=_csv("device_id,timestamp,delta_kwh\nGHOST,2025-06-04 06:00:00,610\n"),
    )
    assert response.status_code == 400
    assert "GHOST" in response.text


async def test_negative_generation_is_rejected(app_client, admin_token, make_user):
    _, installer = await make_user("installer")
    await app_client.post(
        "/api/admin/devices",
        headers=auth(admin_token),
        json={"device_id": "CSV-4", "owner_email": installer["email"], "location": "Pune"},
    )
    response = await app_client.post(
        "/api/admin/ingest-csv",
        headers=auth(admin_token),
        files=_csv("device_id,timestamp,delta_kwh\nCSV-4,2025-06-05 06:00:00,-100\n"),
    )
    assert response.status_code == 400


# ── Audit trail ────────────────────────────────────────────────────────────

async def test_admin_actions_are_audited(app_client, admin_token, make_user):
    _, installer = await make_user("installer")
    await app_client.post(
        "/api/admin/devices",
        headers=auth(admin_token),
        json={"device_id": "AUDIT-1", "owner_email": installer["email"], "location": "Pune"},
    )

    logs = (await app_client.get("/api/admin/audit-log", headers=auth(admin_token))).json()["logs"]
    entry = next(e for e in logs if e["target_id"] == "AUDIT-1")
    assert entry["action"] == "register_device"
    assert entry["admin_email"] == config.ADMIN_EMAIL
    assert entry["reason"]


async def test_the_audit_log_is_admin_only(app_client, make_user):
    token, _ = await make_user("buyer")
    assert (await app_client.get("/api/admin/audit-log", headers=auth(token))).status_code == 403


# ── On-chain guards ────────────────────────────────────────────────────────

async def test_minting_requires_admin(app_client, make_user):
    token, _ = await make_user("installer")
    response = await app_client.post(
        f"/mint/1?recipient=0x{'a' * 40}", headers=auth(token)
    )
    assert response.status_code == 403


async def test_minting_without_a_signing_key_returns_503(app_client, admin_token):
    """The API must refuse cleanly rather than fail partway through a transaction."""
    response = await app_client.post(
        f"/mint/1?recipient=0x{'a' * 40}", headers=auth(admin_token)
    )
    assert response.status_code == 503
    assert "signing key" in response.json()["detail"]


async def test_retiring_requires_admin(app_client, make_user):
    token, _ = await make_user("buyer")
    assert (await app_client.post("/retire/1", headers=auth(token))).status_code == 403
