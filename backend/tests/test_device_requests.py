"""
Installer device self-onboarding.

An installer submits a device; it only becomes real once an administrator
approves it. Self-registration is deliberately not possible, because a credit's
integrity rests on its device having been attested by someone other than the
party who profits from it.
"""

import database
from tests.conftest import auth


async def _submit(app_client, token, device_id, location="Pune", notes=""):
    return await app_client.post(
        "/api/installer/device-requests",
        headers=auth(token),
        json={"device_id": device_id, "location": location, "notes": notes},
    )


# ── Submission ─────────────────────────────────────────────────────────────

async def test_an_installer_can_submit_a_device(app_client, make_user):
    token, _ = await make_user("installer")
    response = await _submit(app_client, token, "REQ-1")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "pending_review"
    assert body["device_id"] == "REQ-1"


async def test_submitting_does_not_create_the_device(app_client, make_user):
    """The whole point: a request must not take effect on its own."""
    token, _ = await make_user("installer")
    await _submit(app_client, token, "REQ-NOT-LIVE")

    row = await database.database.fetch_one(
        "SELECT id FROM devices WHERE device_id = 'REQ-NOT-LIVE'"
    )
    assert row is None


async def test_buyers_cannot_submit_devices(app_client, make_user):
    token, _ = await make_user("buyer")
    assert (await _submit(app_client, token, "REQ-BUYER")).status_code == 403


async def test_submission_requires_authentication(app_client):
    response = await app_client.post(
        "/api/installer/device-requests", json={"device_id": "REQ-ANON"}
    )
    assert response.status_code == 401


async def test_a_malformed_device_id_is_rejected(app_client, make_user):
    token, _ = await make_user("installer")
    assert (await _submit(app_client, token, "bad id!")).status_code == 422
    assert (await _submit(app_client, token, "ab")).status_code == 422


async def test_an_already_registered_device_cannot_be_claimed(
    app_client, admin_token, make_user
):
    """Otherwise an installer could request a device another account owns."""
    _, owner = await make_user("installer")
    await app_client.post(
        "/api/admin/devices",
        headers=auth(admin_token),
        json={"device_id": "REQ-TAKEN", "owner_email": owner["email"], "location": "X"},
    )

    other_token, _ = await make_user("installer")
    assert (await _submit(app_client, other_token, "REQ-TAKEN")).status_code == 409


async def test_the_same_device_cannot_be_queued_twice(app_client, make_user):
    token, _ = await make_user("installer")
    assert (await _submit(app_client, token, "REQ-DUP")).status_code == 200
    assert (await _submit(app_client, token, "REQ-DUP")).status_code == 409


async def test_an_installer_sees_only_their_own_submissions(app_client, make_user):
    first_token, _ = await make_user("installer")
    second_token, _ = await make_user("installer")
    await _submit(app_client, first_token, "REQ-MINE")
    await _submit(app_client, second_token, "REQ-THEIRS")

    body = (await app_client.get(
        "/api/installer/device-requests", headers=auth(first_token))).json()
    ids = {r["device_id"] for r in body["requests"]}
    assert "REQ-MINE" in ids
    assert "REQ-THEIRS" not in ids


# ── Review ─────────────────────────────────────────────────────────────────

async def test_approval_registers_the_device_to_the_requester(
    app_client, admin_token, make_user
):
    token, installer = await make_user("installer")
    request_id = (await _submit(app_client, token, "REQ-OK", "Nashik")).json()["request_id"]

    approval = await app_client.post(
        f"/api/admin/device-requests/{request_id}/approve",
        headers=auth(admin_token), json={"note": "Panels verified on site"},
    )
    assert approval.status_code == 200, approval.text
    assert approval.json()["owner_email"] == installer["email"]

    # The installer can now see it among their devices.
    devices = (await app_client.get(
        "/api/installer/devices", headers=auth(token))).json()["devices"]
    entry = next(d for d in devices if d["device_id"] == "REQ-OK")
    assert entry["location"] == "Nashik"


async def test_an_approved_device_can_receive_readings(app_client, admin_token, make_user):
    """End to end: submit, approve, ingest, and the credit lands with the installer."""
    token, _ = await make_user("installer")
    request_id = (await _submit(app_client, token, "REQ-FLOW")).json()["request_id"]
    await app_client.post(
        f"/api/admin/device-requests/{request_id}/approve",
        headers=auth(admin_token), json={"note": "ok"},
    )

    ingest = await app_client.post(
        "/api/admin/ingest-csv",
        headers=auth(admin_token),
        files={"file": ("r.csv",
                        "device_id,timestamp,delta_kwh\n"
                        "REQ-FLOW,2025-07-01 06:00:00,610\n"
                        "REQ-FLOW,2025-07-01 06:15:00,610\n", "text/csv")},
    )
    assert ingest.status_code == 200, ingest.text
    assert ingest.json()["credits_issued"] == 1

    dashboard = (await app_client.get(
        "/api/installer/dashboard", headers=auth(token))).json()
    assert dashboard["stats"]["total_credits"] == 1


async def test_rejection_records_a_reason_and_creates_nothing(
    app_client, admin_token, make_user
):
    token, _ = await make_user("installer")
    request_id = (await _submit(app_client, token, "REQ-NO")).json()["request_id"]

    rejection = await app_client.post(
        f"/api/admin/device-requests/{request_id}/reject",
        headers=auth(admin_token), json={"note": "Serial number does not match the meter"},
    )
    assert rejection.status_code == 200

    assert await database.database.fetch_one(
        "SELECT id FROM devices WHERE device_id = 'REQ-NO'") is None

    body = (await app_client.get(
        "/api/installer/device-requests", headers=auth(token))).json()
    entry = next(r for r in body["requests"] if r["device_id"] == "REQ-NO")
    assert entry["status"] == "rejected"
    assert "Serial number" in entry["review_note"]


async def test_rejection_requires_a_reason(app_client, admin_token, make_user):
    token, _ = await make_user("installer")
    request_id = (await _submit(app_client, token, "REQ-NOREASON")).json()["request_id"]

    response = await app_client.post(
        f"/api/admin/device-requests/{request_id}/reject",
        headers=auth(admin_token), json={"note": ""},
    )
    assert response.status_code == 400


async def test_a_request_cannot_be_reviewed_twice(app_client, admin_token, make_user):
    token, _ = await make_user("installer")
    request_id = (await _submit(app_client, token, "REQ-ONCE")).json()["request_id"]

    first = await app_client.post(
        f"/api/admin/device-requests/{request_id}/approve",
        headers=auth(admin_token), json={"note": "ok"})
    assert first.status_code == 200

    second = await app_client.post(
        f"/api/admin/device-requests/{request_id}/approve",
        headers=auth(admin_token), json={"note": "again"})
    assert second.status_code == 409


async def test_installers_cannot_approve_their_own_request(app_client, make_user):
    """The approval gate is worthless if the requester can pass it themselves."""
    token, _ = await make_user("installer")
    request_id = (await _submit(app_client, token, "REQ-SELF")).json()["request_id"]

    response = await app_client.post(
        f"/api/admin/device-requests/{request_id}/approve",
        headers=auth(token), json={"note": "approving myself"},
    )
    assert response.status_code == 403

    assert await database.database.fetch_one(
        "SELECT id FROM devices WHERE device_id = 'REQ-SELF'") is None


async def test_reviews_are_audited(app_client, admin_token, make_user):
    token, _ = await make_user("installer")
    request_id = (await _submit(app_client, token, "REQ-AUDIT")).json()["request_id"]
    await app_client.post(
        f"/api/admin/device-requests/{request_id}/approve",
        headers=auth(admin_token), json={"note": "Verified against invoice"})

    logs = (await app_client.get(
        "/api/admin/audit-log", headers=auth(admin_token))).json()["logs"]
    entry = next(e for e in logs if e["target_id"] == "REQ-AUDIT")
    assert entry["action"] == "approve_device_request"
    assert "invoice" in entry["reason"]


async def test_the_admin_queue_surfaces_pending_requests(
    app_client, admin_token, make_user
):
    token, _ = await make_user("installer")
    await _submit(app_client, token, "REQ-QUEUE")

    body = (await app_client.get(
        "/api/admin/device-requests?status_filter=pending",
        headers=auth(admin_token))).json()
    assert body["pending_count"] >= 1
    assert all(r["status"] == "pending" for r in body["requests"])
    assert any(r["device_id"] == "REQ-QUEUE" for r in body["requests"])


async def test_the_queue_is_admin_only(app_client, make_user):
    token, _ = await make_user("installer")
    assert (await app_client.get(
        "/api/admin/device-requests", headers=auth(token))).status_code == 403
