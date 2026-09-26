"""
CTN Admin routes — platform visibility, data ingestion, audit trail, health.

Every administrative action is recorded in `audit_log` with the acting admin,
a timestamp, and a stated reason.
"""

import json
import time
from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from pydantic import BaseModel, Field, field_validator

import chain
import config
from auth import require_admin
from data_utils import CsvError, parse_reading_csv
from database import database, db_execute_with_retry, process_raw_readings
from routes.site import SiteSpec

router = APIRouter(prefix="/api/admin", tags=["admin"])

# ── Request models ─────────────────────────────────────────────────────────

class DeviceReview(BaseModel):
    """An administrator's decision on a submitted device."""

    note: str = Field(default="", max_length=500)

    @field_validator("note")
    @classmethod
    def strip(cls, value: str) -> str:
        return value.strip()


class DeviceRegistration(SiteSpec):
    device_id: str = Field(..., min_length=1, max_length=64)
    owner_email: str
    location: str = Field(default="India", max_length=120)
    public_key: str = Field(default="", max_length=64)

    @field_validator("public_key")
    @classmethod
    def validate_public_key(cls, value: str) -> str:
        if not value:
            return ""
        import attestation
        return attestation.normalise_public_key(value)

    @field_validator("device_id", "owner_email", "location")
    @classmethod
    def strip(cls, value: str) -> str:
        return value.strip()


# ── Audit logging ──────────────────────────────────────────────────────────

async def log_admin_action(
    admin_id: int,
    action: str,
    target_type: str,
    target_id: str,
    reason: str,
    details: Optional[str] = None,
):
    """Record an administrative action against the audit trail."""
    await db_execute_with_retry(
        query="""INSERT INTO audit_log
                 (admin_user_id, action, target_type, target_id, reason, details)
                 VALUES (:admin_id, :action, :target_type, :target_id, :reason, :details)""",
        values={
            "admin_id": admin_id,
            "action": action,
            "target_type": target_type,
            "target_id": str(target_id),
            "reason": reason,
            "details": details,
        },
    )


def _paginate(page: int, limit: int, cap: int = 200) -> tuple[int, int, int]:
    page, limit = max(1, page), max(1, min(limit, cap))
    return page, limit, (page - 1) * limit


# ── Overview ───────────────────────────────────────────────────────────────

@router.get("/overview")
async def admin_overview(admin: dict = Depends(require_admin)):
    """Platform-wide counts across users, credits, and transactions."""
    users = await database.fetch_one(
        """SELECT COUNT(*) AS total,
                  COALESCE(SUM(role = 'installer'), 0) AS installers,
                  COALESCE(SUM(role = 'buyer'), 0)     AS buyers,
                  COALESCE(SUM(role = 'admin'), 0)     AS admins
           FROM users"""
    )
    credits = await database.fetch_one(
        """SELECT COUNT(*) AS total,
                  COALESCE(SUM(status = 'pending'), 0)  AS pending,
                  COALESCE(SUM(status = 'verified'), 0) AS verified,
                  COALESCE(SUM(status = 'listed'), 0)   AS listed,
                  COALESCE(SUM(status = 'reserved'), 0) AS reserved,
                  COALESCE(SUM(status = 'sold'), 0)     AS sold,
                  COALESCE(SUM(status = 'retired'), 0)  AS retired,
                  COALESCE(SUM(on_chain_id IS NOT NULL), 0) AS minted,
                  COALESCE(SUM(total_kwh), 0)           AS total_kwh,
                  COALESCE(SUM(co2_avoided_kg), 0)      AS total_co2_kg
           FROM credits"""
    )
    transactions = await database.fetch_one(
        """SELECT COUNT(*) AS total,
                  COALESCE(SUM(payment_status = 'completed'), 0) AS completed,
                  COALESCE(SUM(CASE WHEN payment_status = 'completed'
                                    THEN total_amount_inr ELSE 0 END), 0) AS total_inr
           FROM marketplace_transactions"""
    )

    return {
        "users": dict(users),
        "credits": dict(credits),
        "transactions": dict(transactions),
    }


@router.get("/installers")
async def list_installers(admin: dict = Depends(require_admin)):
    """Installer accounts with their device and credit counts."""
    installers = await database.fetch_all(
        """SELECT u.id, u.email, u.wallet_address, u.created_at,
                  (SELECT COUNT(*) FROM credits WHERE owner_user_id = u.id) AS credit_count,
                  (SELECT COUNT(*) FROM devices WHERE owner_user_id = u.id) AS device_count,
                  (SELECT COALESCE(SUM(total_kwh), 0) FROM credits
                   WHERE owner_user_id = u.id) AS total_kwh
           FROM users u WHERE u.role = 'installer' AND u.deleted_at IS NULL
           ORDER BY u.created_at DESC"""
    )
    return {"installers": [dict(i) for i in installers]}


@router.get("/buyers")
async def list_buyers(admin: dict = Depends(require_admin)):
    """Buyer accounts with what they have purchased and spent."""
    buyers = await database.fetch_all(
        """SELECT u.id, u.email, u.wallet_address, u.created_at,
                  (SELECT COUNT(*) FROM marketplace_transactions
                   WHERE buyer_user_id = u.id AND payment_status = 'completed')
                      AS purchase_count,
                  (SELECT COALESCE(SUM(quantity), 0) FROM marketplace_transactions
                   WHERE buyer_user_id = u.id AND payment_status = 'completed')
                      AS credits_bought,
                  (SELECT COALESCE(SUM(total_amount_inr), 0) FROM marketplace_transactions
                   WHERE buyer_user_id = u.id AND payment_status = 'completed')
                      AS total_spent_inr
           FROM users u WHERE u.role = 'buyer' AND u.deleted_at IS NULL
           ORDER BY u.created_at DESC"""
    )
    return {"buyers": [dict(b) for b in buyers]}


@router.get("/credits")
async def list_all_credits(
    page: int = 1,
    limit: int = 50,
    status_filter: Optional[str] = None,
    owner_id: Optional[int] = None,
    admin: dict = Depends(require_admin),
):
    """The full credit ledger, with optional status and owner filters."""
    page, limit, offset = _paginate(page, limit, cap=500)

    conditions, values = [], {}
    if status_filter:
        conditions.append("c.status = :status")
        values["status"] = status_filter
    if owner_id:
        conditions.append("c.owner_user_id = :owner_id")
        values["owner_id"] = owner_id

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""

    credits = await database.fetch_all(
        query=f"""SELECT c.*, u.email AS owner_email, u.wallet_address AS owner_wallet
                  FROM credits c LEFT JOIN users u ON c.owner_user_id = u.id
                  {where} ORDER BY c.credit_id DESC LIMIT :limit OFFSET :offset""",
        values={**values, "limit": limit, "offset": offset},
    )
    row = await database.fetch_one(
        query=f"SELECT COUNT(*) AS cnt FROM credits c {where}", values=values
    )
    total = row["cnt"] if row else 0

    return {
        "credits": [dict(c) for c in credits],
        "total": total,
        "page": page,
        "pages": max(1, -(-total // limit)),
        "explorer": config.EXPLORER,
    }


@router.get("/credit/{credit_id}")
async def credit_detail(credit_id: int, admin: dict = Depends(require_admin)):
    """A single credit with its full audit history, for dispute resolution."""
    credit = await database.fetch_one(
        query="""SELECT c.*, u.email AS owner_email, b.email AS buyer_email
                 FROM credits c
                 LEFT JOIN users u ON c.owner_user_id = u.id
                 LEFT JOIN users b ON c.buyer_user_id = b.id
                 WHERE c.credit_id = :credit_id""",
        values={"credit_id": credit_id},
    )
    if not credit:
        raise HTTPException(404, f"Credit #{credit_id} not found")

    audit = await database.fetch_all(
        query="""SELECT al.*, u.email AS admin_email
                 FROM audit_log al LEFT JOIN users u ON al.admin_user_id = u.id
                 WHERE al.target_type = 'credit' AND al.target_id = :id
                 ORDER BY al.created_at DESC""",
        values={"id": str(credit_id)},
    )

    return {"credit": dict(credit), "audit_trail": [dict(a) for a in audit]}


@router.get("/transactions")
async def list_transactions(page: int = 1, limit: int = 50, admin: dict = Depends(require_admin)):
    """All marketplace transactions."""
    page, limit, offset = _paginate(page, limit, cap=500)

    transactions = await database.fetch_all(
        query="""SELECT mt.*, u.email AS buyer_email
                 FROM marketplace_transactions mt
                 LEFT JOIN users u ON mt.buyer_user_id = u.id
                 ORDER BY mt.created_at DESC LIMIT :limit OFFSET :offset""",
        values={"limit": limit, "offset": offset},
    )
    row = await database.fetch_one("SELECT COUNT(*) AS cnt FROM marketplace_transactions")
    total = row["cnt"] if row else 0

    return {
        "transactions": [dict(t) for t in transactions],
        "total": total,
        "page": page,
        "pages": max(1, -(-total // limit)),
    }


@router.get("/audit-log")
async def get_audit_log(page: int = 1, limit: int = 50, admin: dict = Depends(require_admin)):
    """The administrative audit trail."""
    page, limit, offset = _paginate(page, limit, cap=500)

    logs = await database.fetch_all(
        query="""SELECT al.*, u.email AS admin_email
                 FROM audit_log al LEFT JOIN users u ON al.admin_user_id = u.id
                 ORDER BY al.created_at DESC LIMIT :limit OFFSET :offset""",
        values={"limit": limit, "offset": offset},
    )
    row = await database.fetch_one("SELECT COUNT(*) AS cnt FROM audit_log")
    total = row["cnt"] if row else 0

    return {
        "logs": [dict(entry) for entry in logs],
        "total": total,
        "page": page,
        "pages": max(1, -(-total // limit)),
    }


# ── Device registration ────────────────────────────────────────────────────

@router.get("/devices")
async def list_devices(admin: dict = Depends(require_admin)):
    """Every registered device with its owner and contribution to date."""
    devices = await database.fetch_all(
        """SELECT d.device_id, d.location, d.created_at,
                  d.public_key, d.last_sequence, d.verified, d.enrolled_via,
                  d.rated_capacity_kw, d.latitude, d.longitude, d.last_reading_at,
                  d.last_meter_wh, d.tamper_count,
                  u.email AS owner_email,
                  (SELECT COUNT(*) FROM generation_readings r
                   WHERE r.device_id = d.device_id) AS reading_count,
                  (SELECT COUNT(*) FROM generation_readings r
                   WHERE r.device_id = d.device_id
                     AND r.device_signature IS NOT NULL) AS attested_count,
                  (SELECT COUNT(*) FROM credits c
                   WHERE c.device_id = d.device_id) AS credit_count,
                  (SELECT COALESCE(SUM(r.total_kwh), 0) FROM generation_readings r
                   WHERE r.device_id = d.device_id) AS total_kwh
           FROM devices d
           LEFT JOIN users u ON d.owner_user_id = u.id
           ORDER BY d.created_at DESC"""
    )
    return {"devices": [dict(d) for d in devices], "total": len(devices)}


@router.post("/devices")
async def register_device(req: DeviceRegistration, admin: dict = Depends(require_admin)):
    """Register a generation device against an existing installer account."""
    owner = await database.fetch_one(
        "SELECT id, role FROM users WHERE email = :email", {"email": req.owner_email.lower()}
    )
    if not owner:
        raise HTTPException(404, f"No user found with email {req.owner_email}")
    if owner["role"] != "installer":
        raise HTTPException(400, "Devices can only be registered to installer accounts.")

    existing = await database.fetch_one(
        "SELECT id FROM devices WHERE device_id = :device_id", {"device_id": req.device_id}
    )
    if existing:
        raise HTTPException(409, f"Device '{req.device_id}' is already registered.")

    await db_execute_with_retry(
        query="""INSERT INTO devices
                     (device_id, owner_user_id, location, public_key,
                      rated_capacity_kw, latitude, longitude)
                 VALUES (:device_id, :owner_id, :location, :public_key,
                         :capacity, :latitude, :longitude)""",
        values={
            "device_id": req.device_id,
            "owner_id": owner["id"],
            "location": req.location,
            "public_key": req.public_key or None,
            **req.site_values(),
        },
    )
    await log_admin_action(
        admin["id"], "register_device", "device", req.device_id,
        f"Registered to {req.owner_email}",
    )

    return {
        "status": "registered",
        "device_id": req.device_id,
        "owner_email": req.owner_email,
        "location": req.location,
    }


# ── Device requests ────────────────────────────────────────────────────────

@router.get("/device-requests")
async def list_device_requests(
    status_filter: Optional[str] = None, admin: dict = Depends(require_admin)
):
    """Devices submitted by installers, newest first, pending ones first."""
    if status_filter and status_filter not in ("pending", "approved", "rejected"):
        raise HTTPException(400, "status_filter must be pending, approved, or rejected")

    where = "WHERE dr.status = :status" if status_filter else ""
    values = {"status": status_filter} if status_filter else {}

    requests = await database.fetch_all(
        query=f"""SELECT dr.*, u.email AS requester_email, r.email AS reviewer_email
                  FROM device_requests dr
                  LEFT JOIN users u ON dr.requested_by = u.id
                  LEFT JOIN users r ON dr.reviewed_by = r.id
                  {where}
                  ORDER BY dr.status = 'pending' DESC, dr.created_at DESC""",
        values=values,
    )
    pending = await database.fetch_one(
        "SELECT COUNT(*) AS cnt FROM device_requests WHERE status = 'pending'"
    )

    return {
        "requests": [dict(r) for r in requests],
        "pending_count": pending["cnt"] if pending else 0,
    }


async def _load_pending_request(request_id: int) -> dict:
    row = await database.fetch_one(
        query="""SELECT dr.*, u.email AS requester_email
                 FROM device_requests dr
                 LEFT JOIN users u ON dr.requested_by = u.id
                 WHERE dr.id = :id""",
        values={"id": request_id},
    )
    if not row:
        raise HTTPException(404, f"Device request #{request_id} not found")

    record = dict(row)
    if record["status"] != "pending":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Request #{request_id} was already {record['status']}.",
        )
    return record


@router.post("/device-requests/{request_id}/approve")
async def approve_device_request(
    request_id: int, req: DeviceReview, admin: dict = Depends(require_admin)
):
    """Approve a submission and register the device to the installer who asked."""
    record = await _load_pending_request(request_id)

    taken = await database.fetch_one(
        "SELECT id FROM devices WHERE device_id = :device_id",
        {"device_id": record["device_id"]},
    )
    if taken:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Device '{record['device_id']}' was registered by another route in the meantime.",
        )

    async with database.transaction():
        await database.execute(
            query="""INSERT INTO devices
                         (device_id, owner_user_id, location, public_key,
                          rated_capacity_kw, latitude, longitude)
                     VALUES (:device_id, :owner_id, :location, :public_key,
                             :capacity, :latitude, :longitude)""",
            values={
                "device_id": record["device_id"],
                "owner_id": record["requested_by"],
                "location": record["location"] or "India",
                "public_key": record.get("public_key"),
                "capacity": record.get("rated_capacity_kw"),
                "latitude": record.get("latitude"),
                "longitude": record.get("longitude"),
            },
        )
        await database.execute(
            query="""UPDATE device_requests
                     SET status = 'approved', reviewed_by = :admin_id,
                         reviewed_at = :now, review_note = :note
                     WHERE id = :id""",
            values={
                "admin_id": admin["id"],
                "now": time.time(),
                "note": req.note or None,
                "id": request_id,
            },
        )

    await log_admin_action(
        admin["id"], "approve_device_request", "device", record["device_id"],
        req.note or "Device approved",
        f"requested by {record['requester_email']}",
    )

    return {
        "status": "approved",
        "device_id": record["device_id"],
        "owner_email": record["requester_email"],
        "message": f"Device {record['device_id']} is now registered and can receive readings.",
    }


@router.post("/device-requests/{request_id}/reject")
async def reject_device_request(
    request_id: int, req: DeviceReview, admin: dict = Depends(require_admin)
):
    """Decline a submission. A reason is required so the installer can respond."""
    if len(req.note) < 5:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "A reason of at least 5 characters is required when rejecting a device.",
        )

    record = await _load_pending_request(request_id)

    await db_execute_with_retry(
        query="""UPDATE device_requests
                 SET status = 'rejected', reviewed_by = :admin_id,
                     reviewed_at = :now, review_note = :note
                 WHERE id = :id""",
        values={
            "admin_id": admin["id"],
            "now": time.time(),
            "note": req.note,
            "id": request_id,
        },
    )
    await log_admin_action(
        admin["id"], "reject_device_request", "device", record["device_id"], req.note,
        f"requested by {record['requester_email']}",
    )

    return {
        "status": "rejected",
        "device_id": record["device_id"],
        "message": f"Request for {record['device_id']} was declined.",
    }


@router.post("/devices/{device_id}/verify")
async def verify_device(
    device_id: str, req: DeviceReview, admin: dict = Depends(require_admin)
):
    """
    Confirm a device's installation is real, releasing its credits for sale.

    Attestation already proves each reading came from this device unaltered.
    What it cannot show is that the device is measuring a genuine solar array
    rather than a bench supply, so that judgement stays with a person. Credits
    already accrued are promoted here rather than being reissued, since they
    were always valid measurements — only their salability was in question.
    """
    device = await database.fetch_one(
        query="SELECT device_id, verified, public_key FROM devices WHERE device_id = :id",
        values={"id": device_id},
    )
    if not device:
        raise HTTPException(404, f"Device '{device_id}' not found")
    if device["verified"]:
        raise HTTPException(409, f"Device '{device_id}' is already verified.")
    if len(req.note) < 5:
        raise HTTPException(
            400, "Record how the installation was confirmed (at least 5 characters)."
        )

    now = time.time()
    async with database.transaction():
        await database.execute(
            query="""UPDATE devices SET verified = 1, verified_at = :now, verified_by = :admin
                     WHERE device_id = :id""",
            values={"now": now, "admin": admin["id"], "id": device_id},
        )
        promoted = await database.fetch_all(
            query="""SELECT id FROM credits
                     WHERE device_id = :id AND status = 'pending' AND review_hold IS NULL""",
            values={"id": device_id},
        )
        if promoted:
            await database.execute(
                query="""UPDATE credits SET status = 'verified'
                         WHERE device_id = :id AND status = 'pending' AND review_hold IS NULL""",
                values={"id": device_id},
            )

    await log_admin_action(
        admin["id"], "verify_device", "device", device_id, req.note,
        f"released {len(promoted)} pending credit(s)",
    )

    return {
        "status": "verified",
        "device_id": device_id,
        "credits_released": len(promoted),
        "message": f"{device_id} verified; {len(promoted)} credit(s) are now sellable.",
    }


# ── Settlement ─────────────────────────────────────────────────────────────

@router.get("/settlements")
async def settlements(admin: dict = Depends(require_admin)):
    """Proceeds per payee (seller, treasury, reserve) and each seller's share."""
    import settlement

    return await settlement.summary()


# ── Review holds and device events ────────────────────────────────────────

@router.get("/review-queue")
async def review_queue(admin: dict = Depends(require_admin)):
    """
    Credits held because screening flagged a contributing reading.

    Each entry carries the flags themselves, so the reviewer sees what was
    measured and which threshold it crossed, not an opaque score.
    """
    held = await database.fetch_all(
        """SELECT c.credit_id, c.device_id, c.review_hold, c.period_start, c.period_end,
                  c.total_kwh, d.verified AS device_verified
           FROM credits c LEFT JOIN devices d ON d.device_id = c.device_id
           WHERE c.review_hold IS NOT NULL
           ORDER BY c.credit_id"""
    )

    queue = []
    for credit in held:
        flagged = await database.fetch_all(
            query="""SELECT r.reading_id, r.timestamp, r.total_kwh, r.anomaly_flags
                     FROM credit_allocations a
                     JOIN credits c ON c.id = a.credit_row_id
                     JOIN generation_readings r ON r.id = a.reading_row_id
                     WHERE c.credit_id = :credit_id AND r.anomaly_flags IS NOT NULL""",
            values={"credit_id": credit["credit_id"]},
        )
        queue.append({
            **dict(credit),
            "flagged_readings": [
                {**dict(r), "anomaly_flags": json.loads(r["anomaly_flags"])} for r in flagged
            ],
        })
    return {"credits": queue, "total": len(queue)}


@router.post("/credits/{credit_id}/release")
async def release_held_credit(
    credit_id: int, req: DeviceReview, admin: dict = Depends(require_admin)
):
    """
    Clear a review hold after a person has looked at the flagged readings.

    The credit becomes sellable only if its device's installation is also
    confirmed; otherwise it stays pending and is released with the device.
    """
    if len(req.note) < 5:
        raise HTTPException(400, "Record what was checked (at least 5 characters).")

    credit = await database.fetch_one(
        query="""SELECT c.credit_id, c.status, c.review_hold, c.device_id,
                        d.verified AS device_verified
                 FROM credits c LEFT JOIN devices d ON d.device_id = c.device_id
                 WHERE c.credit_id = :credit_id""",
        values={"credit_id": credit_id},
    )
    if not credit:
        raise HTTPException(404, f"Credit #{credit_id} not found")
    if not credit["review_hold"]:
        raise HTTPException(409, f"Credit #{credit_id} is not held for review.")

    sellable = bool(credit["device_verified"]) or config.TRUST_SELF_ENROLLED_DEVICES
    new_status = "verified" if sellable and credit["status"] == "pending" else credit["status"]

    await log_admin_action(
        admin["id"], "release_credit", "credit", str(credit_id), req.note,
        f"hold cleared: {credit['review_hold']}",
    )
    await db_execute_with_retry(
        query="""UPDATE credits SET review_hold = NULL, status = :status
                 WHERE credit_id = :credit_id""",
        values={"status": new_status, "credit_id": credit_id},
    )

    return {
        "status": new_status,
        "credit_id": credit_id,
        "message": (
            f"Credit #{credit_id} released for sale." if new_status == "verified"
            else f"Hold cleared on credit #{credit_id}; it is released when "
                 f"{credit['device_id']} is confirmed."
        ),
    }


@router.get("/device-events")
async def device_events(
    device_id: Optional[str] = None, limit: int = 100, admin: dict = Depends(require_admin)
):
    """Tamper and other trust-changing events reported by devices, newest first."""
    where = "WHERE device_id = :device_id" if device_id else ""
    values = {"limit": max(1, min(limit, 500))}
    if device_id:
        values["device_id"] = device_id
    rows = await database.fetch_all(
        query=f"""SELECT device_id, kind, detail, created_at FROM device_events
                  {where} ORDER BY id DESC LIMIT :limit""",
        values=values,
    )
    return {"events": [dict(r) for r in rows]}


# ── CSV ingestion ──────────────────────────────────────────────────────────

@router.post("/ingest-csv")
async def ingest_csv(file: UploadFile = File(...), admin: dict = Depends(require_admin)):
    """
    Import raw readings from a CSV of `device_id, timestamp, delta_kwh`.

    An operator can import for any registered device. Imported rows carry no
    device signature, so they are recorded as unattested.
    """
    devices = {
        row["device_id"]: dict(row)
        for row in await database.fetch_all(
            "SELECT device_id, owner_user_id, location FROM devices"
        )
    }

    try:
        readings = parse_reading_csv(
            await file.read(), devices, config.EMISSION_FACTOR_KG_PER_KWH
        )
    except CsvError as exc:
        raise HTTPException(400, exc.as_detail() if exc.errors else exc.message)

    inserted, issued = await process_raw_readings(readings, readings[0]["owner_user_id"])
    await log_admin_action(
        admin["id"], "ingest_csv", "generation_readings", file.filename or "upload.csv",
        f"Ingested {inserted} readings", f"issued {issued} credits",
    )

    return {
        "status": "ingested",
        "readings_added": inserted,
        "rows_processed": len(readings),
        "credits_issued": issued,
        "message": f"Added {inserted} reading(s) and issued {issued} new credit(s).",
    }


# ── System health ──────────────────────────────────────────────────────────

@router.get("/system-health")
async def system_health(admin: dict = Depends(require_admin)):
    """Contract, RPC, wallet, and database status."""
    health = {
        "environment": config.ENVIRONMENT,
        "contract_address": config.CONTRACT_ADDRESS,
        "explorer": chain.contract_url(),
    }

    try:
        chain_info = await chain.health()
        health.update(chain_info)
        health["status"] = (
            "healthy"
            if chain_info.get("rpc_connected") and chain_info.get("is_owner")
            else "degraded"
        )
    except Exception as exc:
        health["status"] = "error"
        health["chain_error"] = str(exc)

    try:
        credits = await database.fetch_one("SELECT COUNT(*) AS cnt FROM credits")
        users = await database.fetch_one("SELECT COUNT(*) AS cnt FROM users")
        # The contract's counter is its lifetime total across every deployment
        # that has ever used it, including earlier testing. Only credits this
        # database has recorded an on-chain id for were minted by this platform.
        minted = await database.fetch_one(
            "SELECT COUNT(*) AS cnt FROM credits WHERE on_chain_id IS NOT NULL"
        )
        health["db_credits"] = credits["cnt"]
        health["db_users"] = users["cnt"]
        health["minted_by_this_platform"] = minted["cnt"]
    except Exception as exc:
        health["status"] = "error"
        health["db_error"] = str(exc)

    return health
