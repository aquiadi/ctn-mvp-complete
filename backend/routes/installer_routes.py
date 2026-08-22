"""
CTN Installer routes — dashboard, credits, readings, devices, listing, history.

Every query is scoped to the authenticated installer's own records.
"""

import re
import secrets
import time
from typing import List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from pydantic import BaseModel, Field, field_validator

import config
from auth import require_installer
import csv_schema
from database import database, db_execute_with_retry, process_raw_readings

router = APIRouter(prefix="/api/installer", tags=["installer"])

VALID_STATUSES = {"pending", "verified", "listed", "reserved", "sold", "retired"}


class SellRequest(BaseModel):
    credit_ids: List[int] = Field(..., min_length=1, max_length=1000)


class DeviceRequestSubmission(BaseModel):
    """An installer asking for a generation device to be added to the platform."""

    device_id: str = Field(..., min_length=3, max_length=64)
    location: str = Field(default="India", max_length=120)
    notes: str = Field(default="", max_length=500)
    # The address derived from the device's signing key. Optional so existing
    # meters can still be onboarded, but without it the device's readings can
    # only ever be imported, never attested.
    public_key: str = Field(default="", max_length=64)

    @field_validator("device_id", "location", "notes")
    @classmethod
    def strip(cls, value: str) -> str:
        return value.strip()

    @field_validator("public_key")
    @classmethod
    def validate_public_key(cls, value: str) -> str:
        if not value:
            return ""
        import attestation
        return attestation.normalise_public_key(value)

    @field_validator("device_id")
    @classmethod
    def validate_device_id(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9._-]+", value):
            raise ValueError(
                "Device ID may contain only letters, numbers, dots, dashes, and underscores."
            )
        return value


class ManualDeviceRequest(BaseModel):
    """A device whose readings will be uploaded rather than signed."""

    device_id: str = Field(..., min_length=3, max_length=64)
    location: str = Field(default="India", max_length=120)

    @field_validator("device_id", "location")
    @classmethod
    def strip(cls, value: str) -> str:
        return value.strip()

    @field_validator("device_id")
    @classmethod
    def validate_device_id(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9._-]+", value):
            raise ValueError(
                "Device ID may contain only letters, numbers, dots, dashes, and underscores."
            )
        return value


class EnrollmentCodeRequest(BaseModel):
    """A seller asking for a pairing code to flash into a new device."""

    label: str = Field(default="", max_length=64)
    location: str = Field(default="India", max_length=120)

    @field_validator("label", "location")
    @classmethod
    def strip(cls, value: str) -> str:
        return value.strip()


def _paginate(page: int, limit: int, cap: int = 200) -> tuple[int, int, int]:
    page, limit = max(1, page), max(1, min(limit, cap))
    return page, limit, (page - 1) * limit


@router.get("/dashboard")
async def installer_dashboard(user: dict = Depends(require_installer)):
    """Generation totals, credit counts by status, and progress toward listing."""
    user_id = user["id"]

    totals = dict(
        await database.fetch_one(
            query="""SELECT COALESCE(SUM(total_kwh), 0)      AS total_kwh,
                            COALESCE(SUM(co2_avoided_kg), 0) AS total_co2_kg,
                            COUNT(*)                         AS total_credits,
                            MIN(period_start)                AS period_start,
                            MAX(period_end)                  AS period_end
                     FROM credits WHERE owner_user_id = :user_id""",
            values={"user_id": user_id},
        )
    )

    status_rows = await database.fetch_all(
        query="""SELECT status, COUNT(*) AS count FROM credits
                 WHERE owner_user_id = :user_id GROUP BY status""",
        values={"user_id": user_id},
    )
    by_status = {row["status"]: row["count"] for row in status_rows}
    verified = by_status.get("verified", 0)

    device_rows = await database.fetch_all(
        query="""SELECT d.device_id, d.location, d.verified, d.enrolled_via,
                        d.public_key, d.last_sequence,
                        (SELECT COUNT(*) FROM generation_readings r
                         WHERE r.device_id = d.device_id) AS reading_count
                 FROM devices d WHERE d.owner_user_id = :user_id
                 ORDER BY d.created_at DESC""",
        values={"user_id": user_id},
    )

    devices = []
    for row in device_rows:
        row = dict(row)
        devices.append(
            {
                "device_id": row["device_id"],
                "location": row["location"],
                # Whether an operator has confirmed the installation. This is
                # what decides if its credits can be sold, so it is reported
                # rather than left for the dashboard to assume.
                "verified": bool(row["verified"]),
                "attests": bool(row["public_key"]),
                "reading_count": row["reading_count"],
                "receiving_data": row["reading_count"] > 0,
                "source": "sensor" if row["public_key"] else "uploads",
            }
        )

    total_credits = totals["total_credits"]

    return {
        "installer": {
            "id": user["id"],
            "email": user["email"],
            "wallet_address": user["wallet_address"],
            "wallet_linked": bool(user["wallet_address"]),
        },
        "stats": {
            "total_kwh": round(totals["total_kwh"], 2),
            "total_co2_kg": round(totals["total_co2_kg"], 2),
            "total_co2_tonnes": round(totals["total_co2_kg"] / 1000, 3),
            "total_credits": total_credits,
            "value_usd": round(total_credits * config.CREDIT_VALUE_USD, 2),
            "value_inr": round(total_credits * config.CREDIT_VALUE_INR, 2),
            "period_start": totals["period_start"],
            "period_end": totals["period_end"],
        },
        "credits_by_status": {name: by_status.get(name, 0) for name in sorted(VALID_STATUSES)},
        "sell_threshold": {
            "required": config.SELL_THRESHOLD,
            "current": verified,
            "eligible": verified >= config.SELL_THRESHOLD,
            "remaining": max(0, config.SELL_THRESHOLD - verified),
            "progress_pct": round(min(100, verified / config.SELL_THRESHOLD * 100), 1),
        },
        "pricing": {
            "price_per_credit_usd": config.CREDIT_VALUE_USD,
            "price_per_credit_inr": config.CREDIT_VALUE_INR,
        },
        "pending_reason": (
            "Credits stay pending until an operator confirms the installation "
            "behind them. Nothing is lost — they are released once that happens."
            if by_status.get("pending") else None
        ),
        "devices": [dict(d) for d in devices],
    }


@router.get("/credits")
async def installer_credits(
    page: int = 1,
    limit: int = 20,
    status_filter: Optional[str] = None,
    user: dict = Depends(require_installer),
):
    """This installer's credits, optionally filtered by lifecycle status."""
    page, limit, offset = _paginate(page, limit, cap=1000)

    if status_filter and status_filter not in VALID_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown status '{status_filter}'. Expected one of: "
                   f"{', '.join(sorted(VALID_STATUSES))}.",
        )

    where = "owner_user_id = :user_id"
    values = {"user_id": user["id"]}
    if status_filter:
        where += " AND status = :status"
        values["status"] = status_filter

    credits = await database.fetch_all(
        query=f"""SELECT id, credit_id, device_id, total_kwh, co2_avoided_kg,
                         period_start, period_end, status, on_chain_id,
                         ipfs_hash, tx_hash, listed_at, sold_at, retired_at
                  FROM credits WHERE {where}
                  ORDER BY credit_id DESC LIMIT :limit OFFSET :offset""",
        values={**values, "limit": limit, "offset": offset},
    )
    row = await database.fetch_one(
        query=f"SELECT COUNT(*) AS cnt FROM credits WHERE {where}", values=values
    )
    total = row["cnt"] if row else 0

    return {
        "credits": [dict(c) for c in credits],
        "total": total,
        "page": page,
        "pages": max(1, -(-total // limit)),
    }


@router.get("/readings")
async def installer_readings(
    page: int = 1, limit: int = 20, user: dict = Depends(require_installer)
):
    """Raw generation readings recorded for this installer's devices."""
    page, limit, offset = _paginate(page, limit)

    readings = await database.fetch_all(
        query="""SELECT reading_id, device_id, total_kwh, co2_avoided_kg,
                        timestamp, signature, device_signature, sequence,
                        consumed_by_credit_id
                 FROM generation_readings WHERE owner_user_id = :user_id
                 ORDER BY timestamp DESC LIMIT :limit OFFSET :offset""",
        values={"user_id": user["id"], "limit": limit, "offset": offset},
    )
    row = await database.fetch_one(
        "SELECT COUNT(*) AS cnt FROM generation_readings WHERE owner_user_id = :user_id",
        {"user_id": user["id"]},
    )
    total = row["cnt"] if row else 0

    return {
        "readings": [
            {**dict(r), "attested": bool(r["device_signature"])} for r in readings
        ],
        "total": total,
        "page": page,
        "pages": max(1, -(-total // limit)),
    }


@router.get("/devices")
async def installer_devices(user: dict = Depends(require_installer)):
    """Registered devices with their contribution to date."""
    devices = await database.fetch_all(
        query="""SELECT d.id, d.device_id, d.location, d.created_at, d.public_key,
                        (SELECT COUNT(*) FROM credits
                         WHERE device_id = d.device_id AND owner_user_id = :user_id) AS credit_count,
                        (SELECT COALESCE(SUM(total_kwh), 0) FROM credits
                         WHERE device_id = d.device_id AND owner_user_id = :user_id) AS total_kwh
                 FROM devices d WHERE d.owner_user_id = :user_id
                 ORDER BY d.created_at DESC""",
        values={"user_id": user["id"]},
    )
    return {"devices": [dict(d) for d in devices]}


@router.post("/device-requests")
async def submit_device_request(
    req: DeviceRequestSubmission, user: dict = Depends(require_installer)
):
    """
    Ask for a device to be added under this installer's account.

    Requests are reviewed by an administrator rather than taking effect
    immediately: a credit's integrity depends on its device being attested, so
    self-registering a device would amount to self-issuing carbon credits.
    """
    taken = await database.fetch_one(
        "SELECT id FROM devices WHERE device_id = :device_id",
        {"device_id": req.device_id},
    )
    if taken:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Device '{req.device_id}' is already registered on the platform.",
        )

    pending = await database.fetch_one(
        """SELECT id FROM device_requests
           WHERE device_id = :device_id AND status = 'pending'""",
        {"device_id": req.device_id},
    )
    if pending:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"A request for '{req.device_id}' is already awaiting review.",
        )

    request_id = await db_execute_with_retry(
        query="""INSERT INTO device_requests
                 (device_id, requested_by, location, public_key, notes)
                 VALUES (:device_id, :requested_by, :location, :public_key, :notes)""",
        values={
            "device_id": req.device_id,
            "requested_by": user["id"],
            "location": req.location or "India",
            "public_key": req.public_key or None,
            "notes": req.notes or None,
        },
    )

    return {
        "status": "pending_review",
        "request_id": request_id,
        "device_id": req.device_id,
        "attestation_enabled": bool(req.public_key),
        "message": "Your device has been submitted for review. "
                   "It starts recording generation once an administrator approves it.",
    }


@router.get("/device-requests")
async def list_device_requests(user: dict = Depends(require_installer)):
    """This installer's submissions and where each one stands."""
    requests = await database.fetch_all(
        query="""SELECT id, device_id, location, public_key, notes, status,
                        review_note, reviewed_at, created_at
                 FROM device_requests WHERE requested_by = :user_id
                 ORDER BY created_at DESC""",
        values={"user_id": user["id"]},
    )
    return {"requests": [dict(r) for r in requests]}


@router.post("/enrollment-codes")
async def create_enrollment_code(
    req: EnrollmentCodeRequest, user: dict = Depends(require_installer)
):
    """
    Issue a single-use pairing code for a new device.

    The seller flashes this into firmware alongside their WiFi details. On first
    boot the device generates its own keypair and redeems the code, which binds
    it to this account without the private key ever existing anywhere else.

    The code is the only thing proving the device belongs to this seller, so it
    is short lived and cannot be reused.
    """
    code = f"CTN-{secrets.token_hex(4).upper()}-{secrets.token_hex(4).upper()}"
    expires_at = time.time() + config.ENROLLMENT_CODE_TTL_MINUTES * 60

    await db_execute_with_retry(
        query="""INSERT INTO device_enrollments
                 (code, owner_user_id, label, location, expires_at)
                 VALUES (:code, :owner_id, :label, :location, :expires_at)""",
        values={
            "code": code,
            "owner_id": user["id"],
            "label": req.label or None,
            "location": req.location or "India",
            "expires_at": expires_at,
        },
    )

    return {
        "status": "created",
        "enrollment_code": code,
        "expires_at": expires_at,
        "expires_in_minutes": config.ENROLLMENT_CODE_TTL_MINUTES,
        "message": (
            "Flash this code into your device along with your WiFi credentials. "
            "It enrolls itself on first boot and starts reporting immediately."
        ),
    }


@router.get("/enrollment-codes")
async def list_enrollment_codes(user: dict = Depends(require_installer)):
    """Pairing codes issued by this seller and whether each has been redeemed."""
    now = time.time()
    codes = await database.fetch_all(
        query="""SELECT code, label, location, created_at, expires_at, used_at, device_id
                 FROM device_enrollments WHERE owner_user_id = :user_id
                 ORDER BY created_at DESC LIMIT 50""",
        values={"user_id": user["id"]},
    )

    return {
        "codes": [
            {
                **dict(c),
                "status": (
                    "redeemed" if c["used_at"]
                    else "expired" if c["expires_at"] < now
                    else "waiting"
                ),
            }
            for c in codes
        ]
    }


@router.post("/devices")
async def add_manual_device(
    req: ManualDeviceRequest, user: dict = Depends(require_installer)
):
    """
    Register a meter whose readings will be uploaded rather than signed.

    For hardware that cannot sign, and for loading history that predates a
    sensor. No signing key is registered, so nothing it reports can be attested;
    its credits stay unsellable until an operator confirms the installation,
    exactly as for a self-enrolled device.
    """
    taken = await database.fetch_one(
        "SELECT id FROM devices WHERE device_id = :device_id",
        {"device_id": req.device_id},
    )
    if taken:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Device '{req.device_id}' is already registered.",
        )

    await db_execute_with_retry(
        query="""INSERT INTO devices (device_id, owner_user_id, location, enrolled_via)
                 VALUES (:device_id, :owner_id, :location, 'manual')""",
        values={
            "device_id": req.device_id,
            "owner_id": user["id"],
            "location": req.location or "India",
        },
    )

    return {
        "status": "added",
        "device_id": req.device_id,
        "attestation_enabled": False,
        "message": (
            f"{req.device_id} added. Upload readings for it as a CSV. "
            "Readings will be recorded as imported rather than attested."
        ),
    }


@router.post("/upload-readings/preview")
async def preview_readings(
    file: UploadFile = File(...), user: dict = Depends(require_installer)
):
    """
    Work out what an uploaded file contains, without storing anything.

    Meter exports have no shared schema, so the columns are identified by what
    they look like. The interpretation is returned for the uploader to check
    first: a wrong guess written silently would put invented generation into the
    ledger, and the ledger is the whole product.
    """
    devices = await _owned_devices(user)
    if not devices:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Add a meter before uploading readings for it.",
        )

    try:
        found = csv_schema.detect(await file.read())
    except csv_schema.SchemaError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))

    summary = found.as_dict()
    summary.update({
        "description": found.describe(),
        "your_devices": sorted(devices),
        "device_required": found.device_column is None,
        "estimated_credits": int(
            found.total_kwh * config.EMISSION_FACTOR_KG_PER_KWH // config.KG_CO2_PER_CREDIT
        ),
    })
    return summary


async def _owned_devices(user: dict) -> dict:
    return {
        row["device_id"]: dict(row)
        for row in await database.fetch_all(
            query="""SELECT device_id, owner_user_id, location
                     FROM devices WHERE owner_user_id = :user_id""",
            values={"user_id": user["id"]},
        )
    }


@router.post("/upload-readings")
async def upload_readings(
    file: UploadFile = File(...),
    device_id: str = Form(default=""),
    user: dict = Depends(require_installer),
):
    """
    Import readings for this seller's own meters.

    The file is interpreted the same way the preview described it. Rows naming a
    meter this account does not own are refused rather than silently crediting
    someone else.
    """
    devices = await _owned_devices(user)
    if not devices:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Add a meter before uploading readings for it.",
        )

    content = await file.read()

    try:
        found = csv_schema.detect(content)
    except csv_schema.SchemaError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))

    chosen = (device_id or "").strip()
    if found.device_column is None:
        # Nothing in the file says which meter this is, so it cannot be guessed.
        if not chosen:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="This file has no device column, so choose which meter it belongs to.",
            )
        if chosen not in devices:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"'{chosen}' is not one of your meters.",
            )

    parsed = csv_schema.to_readings(content, found, chosen or next(iter(devices)))

    unknown = sorted({r["device_id"] for r in parsed} - set(devices))
    if unknown:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"The file names meters that are not yours: {', '.join(unknown[:5])}.",
        )

    readings = [
        {
            "device_id": r["device_id"],
            "timestamp": r["timestamp"],
            "total_kwh": r["delta_kwh"],
            "co2_avoided_kg": r["delta_kwh"] * config.EMISSION_FACTOR_KG_PER_KWH,
            "location": devices[r["device_id"]]["location"],
            "owner_user_id": user["id"],
        }
        for r in parsed
        if r["delta_kwh"] > 0
    ]
    if not readings:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No generation was found in that file — every interval came to zero.",
        )

    inserted, issued = await process_raw_readings(readings, user["id"])

    return {
        "status": "imported",
        "readings_added": inserted,
        "rows_processed": len(readings),
        "credits_issued": issued,
        "attested": False,
        "interpreted_as": found.describe(),
        "message": (
            f"Imported {inserted} reading(s) and issued {issued} credit(s). "
            "Uploaded readings are recorded as imported, not attested."
        ),
    }


@router.post("/sell")
async def list_credits_for_sale(req: SellRequest, user: dict = Depends(require_installer)):
    """
    Put verified credits on the marketplace.

    Requires a linked wallet: the proceeds and eventual on-chain custody are
    tied to an address the installer has proven they control.
    """
    if not user["wallet_address"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Link a wallet before listing credits for sale.",
        )

    credit_ids = list(dict.fromkeys(req.credit_ids))
    if len(credit_ids) < config.SELL_THRESHOLD:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"At least {config.SELL_THRESHOLD} credit(s) must be listed at once. "
                   f"You selected {len(credit_ids)}.",
        )

    keys = [f"id{i}" for i in range(len(credit_ids))]
    placeholders = ", ".join(f":{k}" for k in keys)
    values = {**dict(zip(keys, credit_ids)), "user_id": user["id"]}

    async with database.transaction():
        owned = await database.fetch_all(
            query=f"""SELECT id, status FROM credits
                      WHERE id IN ({placeholders}) AND owner_user_id = :user_id""",
            values=values,
        )

        if len(owned) != len(credit_ids):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Some of the selected credits don't belong to your account.",
            )

        not_verified = [row["id"] for row in owned if row["status"] != "verified"]
        if not_verified:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"{len(not_verified)} credit(s) are not in 'verified' status "
                       "and cannot be listed.",
            )

        await database.execute(
            query=f"""UPDATE credits SET status = 'listed', listed_at = :now
                      WHERE id IN ({placeholders}) AND owner_user_id = :user_id
                        AND status = 'verified'""",
            values={**values, "now": time.time()},
        )

    return {
        "status": "listed",
        "credits_listed": len(credit_ids),
        "price_per_credit_usd": config.CREDIT_VALUE_USD,
        "price_per_credit_inr": config.CREDIT_VALUE_INR,
        "total_value_inr": round(len(credit_ids) * config.CREDIT_VALUE_INR, 2),
        "message": f"{len(credit_ids)} credit(s) listed on the marketplace.",
    }


@router.get("/history")
async def installer_history(
    page: int = 1, limit: int = 20, user: dict = Depends(require_installer)
):
    """Credits that have been sold or retired, with cumulative earnings."""
    page, limit, offset = _paginate(page, limit)

    sold = await database.fetch_all(
        query="""SELECT id, credit_id, total_kwh, co2_avoided_kg, status,
                        sold_at, buyer_user_id, tx_hash, on_chain_id
                 FROM credits
                 WHERE owner_user_id = :user_id AND status IN ('sold', 'retired')
                 ORDER BY sold_at DESC LIMIT :limit OFFSET :offset""",
        values={"user_id": user["id"], "limit": limit, "offset": offset},
    )
    row = await database.fetch_one(
        """SELECT COUNT(*) AS cnt FROM credits
           WHERE owner_user_id = :user_id AND status IN ('sold', 'retired')""",
        {"user_id": user["id"]},
    )
    total = row["cnt"] if row else 0

    return {
        "transactions": [dict(s) for s in sold],
        "total": total,
        "page": page,
        "pages": max(1, -(-total // limit)),
        "total_earned_usd": round(total * config.CREDIT_VALUE_USD, 2),
        "total_earned_inr": round(total * config.CREDIT_VALUE_INR, 2),
    }
