"""
CTN Ingestion API — the device-facing surface.

Sensors post readings they have signed with their own key. Authentication is the
signature itself: there is no session and no shared secret to leak, because the
platform holds only the public half. A reading is accepted when it verifies
against the key registered for that device, and rejected otherwise.

Everything here is versioned under /api/v1 because firmware, once deployed to a
roof, cannot be redeployed as easily as this server.
"""

import re
import time
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field, field_validator

import attestation
import config
from database import database, process_raw_readings
from rate_limit import limiter

router = APIRouter(prefix="/api/v1", tags=["ingestion"])


# ── Request models ─────────────────────────────────────────────────────────

class SignedReading(BaseModel):
    """One interval of generation, signed by the device that measured it."""

    device_id: str = Field(..., min_length=3, max_length=64)
    sequence: int = Field(..., ge=1)
    timestamp: str = Field(..., min_length=4, max_length=64)
    delta_kwh: float = Field(..., ge=0)
    signature: str = Field(..., min_length=64, max_length=200)

    @field_validator("device_id", "timestamp", "signature")
    @classmethod
    def strip(cls, value: str) -> str:
        return value.strip()


class ReadingBatch(BaseModel):
    """
    Several readings from one device.

    Devices buffer while offline, so a batch is the normal case rather than an
    optimisation. Sequences must ascend within the batch.
    """

    readings: List[SignedReading] = Field(..., min_length=1, max_length=500)


class DeviceEnrollment(BaseModel):
    """A device claiming a pairing code and registering the key it generated."""

    enrollment_code: str = Field(..., min_length=8, max_length=64)
    device_id: str = Field(..., min_length=3, max_length=64)
    public_key: str = Field(..., min_length=40, max_length=64)

    @field_validator("enrollment_code", "device_id", "public_key")
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

    @field_validator("public_key")
    @classmethod
    def validate_public_key(cls, value: str) -> str:
        return attestation.normalise_public_key(value)


# ── Enrollment ─────────────────────────────────────────────────────────────

@router.post("/devices/enroll")
@limiter.limit(config.ENROLL_RATE_LIMIT)
async def enroll_device(request: Request, req: DeviceEnrollment):
    """
    Register a device against a seller's pairing code.

    Called by the device itself on first boot, after it has generated a keypair
    it will never disclose. The code proves the seller authorised this hardware;
    the key it submits is what every later reading is checked against.

    Unauthenticated by design — a sensor cannot hold a login, and the code is
    single use and short lived precisely so it can be embedded in firmware.
    """
    now = time.time()

    async with database.transaction():
        enrollment = await database.fetch_one(
            query="""SELECT id, owner_user_id, label, location, expires_at, used_at
                     FROM device_enrollments WHERE code = :code""",
            values={"code": req.enrollment_code},
        )
        if not enrollment:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, "That pairing code does not exist."
            )

        enrollment = dict(enrollment)
        if enrollment["used_at"]:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "That pairing code has already been used. Generate a new one to add another device.",
            )
        if enrollment["expires_at"] < now:
            raise HTTPException(
                status.HTTP_410_GONE,
                "That pairing code has expired. Generate a new one from your dashboard.",
            )

        taken = await database.fetch_one(
            query="SELECT id FROM devices WHERE device_id = :device_id",
            values={"device_id": req.device_id},
        )
        if taken:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"Device '{req.device_id}' is already registered. Choose a different id.",
            )

        await database.execute(
            query="""INSERT INTO devices
                     (device_id, owner_user_id, location, public_key, enrolled_via, verified)
                     VALUES (:device_id, :owner_id, :location, :public_key, 'pairing_code', 0)""",
            values={
                "device_id": req.device_id,
                "owner_id": enrollment["owner_user_id"],
                "location": enrollment["location"] or "India",
                "public_key": req.public_key,
            },
        )
        await database.execute(
            query="""UPDATE device_enrollments
                     SET used_at = :now, device_id = :device_id WHERE id = :id""",
            values={"now": now, "device_id": req.device_id, "id": enrollment["id"]},
        )

    return {
        "status": "enrolled",
        "device_id": req.device_id,
        "public_key": req.public_key,
        "verified": False,
        "next_sequence": 1,
        "message": (
            "Device enrolled. Start posting signed readings to /api/v1/readings. "
            "Credits accrue immediately but cannot be sold until an operator has "
            "confirmed the installation."
        ),
    }


# ── Ingestion ──────────────────────────────────────────────────────────────

@router.post("/readings")
@limiter.limit(config.INGEST_RATE_LIMIT)
async def ingest_signed_readings(request: Request, batch: ReadingBatch):
    """
    Accept signed readings from a device.

    Verification order matters: the device must be known and provisioned with a
    key, every signature must verify, and every sequence must advance past what
    has already been accepted. Only then is anything written — a batch is
    all-or-nothing so a partially applied upload cannot leave a gap that looks
    like missing generation.
    """
    device_ids = {r.device_id for r in batch.readings}
    if len(device_ids) != 1:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "A batch must come from a single device; sequences are per-device.",
        )

    device_id = next(iter(device_ids))
    device = await database.fetch_one(
        query="""SELECT device_id, owner_user_id, location, public_key, last_sequence
                 FROM devices WHERE device_id = :device_id""",
        values={"device_id": device_id},
    )
    if not device:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"Device '{device_id}' is not registered. It must be approved before it can report.",
        )

    device = dict(device)
    if not device["public_key"]:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Device '{device_id}' has no signing key registered, so its readings "
            "cannot be attested. Re-register it with a public key.",
        )

    # Sequences must ascend, both against history and within the batch itself.
    ordered = sorted(batch.readings, key=lambda r: r.sequence)
    if [r.sequence for r in ordered] != [r.sequence for r in batch.readings]:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "Readings must be ordered by ascending sequence."
        )

    last_sequence = device["last_sequence"] or 0
    seen: set[int] = set()
    prepared = []

    for reading in ordered:
        if reading.sequence in seen:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"Sequence {reading.sequence} appears twice in this batch.",
            )
        seen.add(reading.sequence)

        if reading.sequence <= last_sequence:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"Sequence {reading.sequence} was already accepted for '{device_id}' "
                f"(currently at {last_sequence}). This packet is a replay.",
            )

        try:
            signed_message = attestation.verify_reading(
                device_public_key=device["public_key"],
                device_id=reading.device_id,
                sequence=reading.sequence,
                timestamp=reading.timestamp,
                delta_kwh=reading.delta_kwh,
                signature=attestation.signature_hex(reading.signature),
            )
        except attestation.AttestationError as exc:
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED,
                f"Reading at sequence {reading.sequence} failed attestation: {exc}",
            )

        prepared.append(
            {
                "device_id": reading.device_id,
                "timestamp": reading.timestamp,
                "total_kwh": reading.delta_kwh,
                "co2_avoided_kg": reading.delta_kwh * config.EMISSION_FACTOR_KG_PER_KWH,
                "location": device["location"],
                "owner_user_id": device["owner_user_id"],
                "device_signature": attestation.signature_hex(reading.signature),
                "signed_message": signed_message,
                "sequence": reading.sequence,
            }
        )

    highest = prepared[-1]["sequence"]

    async with database.transaction():
        # Guarded so two concurrent uploads cannot both advance the counter;
        # the loser sees no rows changed and is rejected as a replay.
        await database.execute(
            query="""UPDATE devices SET last_sequence = :highest
                     WHERE device_id = :device_id AND last_sequence = :expected""",
            values={
                "highest": highest,
                "device_id": device_id,
                "expected": last_sequence,
            },
        )
        confirmed = await database.fetch_one(
            query="SELECT last_sequence FROM devices WHERE device_id = :device_id",
            values={"device_id": device_id},
        )
        if not confirmed or confirmed["last_sequence"] != highest:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "Another upload for this device landed first. Retry with fresh sequences.",
            )

        inserted, issued = await process_raw_readings(prepared, device["owner_user_id"])

    return {
        "status": "accepted",
        "device_id": device_id,
        "readings_accepted": inserted,
        "readings_submitted": len(prepared),
        "credits_issued": issued,
        "sequence": highest,
        "attested": True,
        "message": f"{inserted} attested reading(s) recorded; {issued} credit(s) issued.",
    }


# ── Public verification ────────────────────────────────────────────────────

@router.get("/devices/{device_id}")
async def device_public_record(device_id: str):
    """
    A device's public identity.

    Deliberately unauthenticated: verifying someone else's credits requires the
    key their readings were signed with, and a key that only the issuer can see
    proves nothing to anyone else.
    """
    device = await database.fetch_one(
        query="""SELECT device_id, location, public_key, last_sequence, created_at,
                        verified, enrolled_via
                 FROM devices WHERE device_id = :device_id""",
        values={"device_id": device_id},
    )
    if not device:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Device '{device_id}' not found")

    device = dict(device)
    counts = await database.fetch_one(
        query="""SELECT COUNT(*) AS total,
                        COALESCE(SUM(device_signature IS NOT NULL), 0) AS attested
                 FROM generation_readings WHERE device_id = :device_id""",
        values={"device_id": device_id},
    )

    return {
        "device_id": device["device_id"],
        "location": device["location"],
        "public_key": device["public_key"],
        "attestation_enabled": bool(device["public_key"]),
        "verified": bool(device["verified"]),
        "enrolled_via": device["enrolled_via"] or "operator",
        "last_sequence": device["last_sequence"],
        "readings_total": counts["total"],
        "readings_attested": counts["attested"],
        "registered_at": device["created_at"],
        "message_version": attestation.MESSAGE_VERSION,
    }


@router.get("/readings/{reading_id}/proof")
async def reading_proof(reading_id: str):
    """
    Everything needed to verify a reading without trusting this server.

    Returns the exact signed text, the signature, and the device's public key.
    Recovering the signer from those and comparing is a check anyone can run
    offline, in any language. The server's own result is included for
    convenience, not as the authority.
    """
    reading = await database.fetch_one(
        query="""SELECT r.*, d.public_key, c.credit_id, c.on_chain_id, c.tx_hash
                 FROM generation_readings r
                 LEFT JOIN devices d ON r.device_id = d.device_id
                 LEFT JOIN credits c ON r.consumed_by_credit_id = c.id
                 WHERE r.reading_id = :reading_id""",
        values={"reading_id": reading_id},
    )
    if not reading:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Reading '{reading_id}' not found")

    reading = dict(reading)
    proof = {
        "reading_id": reading["reading_id"],
        "device_id": reading["device_id"],
        "timestamp": reading["timestamp"],
        "total_kwh": reading["total_kwh"],
        "co2_avoided_kg": reading["co2_avoided_kg"],
        "attested": bool(reading["device_signature"]),
        "content_hash": reading["signature"],
        "credit": _credit_link(reading),
    }

    if not reading["device_signature"]:
        proof["detail"] = (
            "This reading was imported rather than signed by a device, so its origin "
            "rests on the operator who uploaded it. Only the content hash applies."
        )
        return proof

    verified: Optional[bool]
    try:
        recovered = attestation.recover_signer(
            reading["signed_message"], reading["device_signature"]
        )
        verified = recovered.lower() == (reading["public_key"] or "").lower()
    except attestation.AttestationError:
        recovered, verified = None, False

    proof.update(
        {
            "signed_message": reading["signed_message"],
            "device_signature": reading["device_signature"],
            "device_public_key": reading["public_key"],
            "recovered_signer": recovered,
            "signature_valid": verified,
            "sequence": reading["sequence"],
            "message_version": attestation.MESSAGE_VERSION,
            "how_to_verify": (
                "Recover the EIP-191 signer of `signed_message` from "
                "`device_signature` and compare it to `device_public_key`."
            ),
        }
    )
    return proof


def _credit_link(reading: dict) -> Optional[dict]:
    """Where this reading ended up, if it has been consumed into a credit."""
    if not reading.get("credit_id"):
        return None
    return {
        "credit_id": reading["credit_id"],
        "on_chain_id": reading["on_chain_id"],
        "tx_hash": reading["tx_hash"],
        "explorer": f"{config.EXPLORER}/tx/{reading['tx_hash']}"
        if reading["tx_hash"] else None,
    }


@router.get("/spec")
def ingestion_spec():
    """
    The signing contract, served from the code that enforces it.

    Firmware authors need the exact byte layout; publishing it here keeps the
    documentation from drifting away from the implementation.
    """
    example = attestation.canonical_message("INV-2401-7788", 1042, "2026-04-01T06:00:00Z", 0.61)
    return {
        "message_version": attestation.MESSAGE_VERSION,
        "signature_scheme": "EIP-191 personal_sign over secp256k1",
        "signature_format": (
            "0x-prefixed r||s (64 bytes), with or without a trailing recovery id. "
            "Embedded libraries that cannot derive the recovery id may omit it; "
            "both possibilities are tried."
        ),
        "key_format": "0x-prefixed 20-byte address derived from the device signing key",
        "kwh_decimals": attestation.KWH_DECIMALS,
        "canonical_message_template": (
            f"{attestation.MESSAGE_VERSION}\\n"
            "device:{device_id}\\n"
            "sequence:{sequence}\\n"
            "timestamp:{timestamp}\\n"
            "delta_kwh:{delta_kwh with 6 decimal places}"
        ),
        "example_message": example,
        "rules": [
            "Sequence numbers start at 1 and must strictly increase per device.",
            "A sequence already accepted is rejected as a replay.",
            "Batches must contain one device and ascending sequences.",
            "delta_kwh is the generation for that interval, not a meter total.",
            "Readings are rejected whole; a batch never applies partially.",
        ],
        "endpoint": "POST /api/v1/readings",
    }
