"""
CTN Installer Routes — dashboard, credits, devices, sell, history.
All routes scoped to the authenticated installer's own data.
"""

import time
from fastapi import APIRouter, HTTPException, Depends, status
from pydantic import BaseModel
from typing import List, Optional

from database import database, db_execute_with_retry
from auth import require_installer

router = APIRouter(prefix="/api/installer", tags=["installer"])

# ── Config ─────────────────────────────────────────────────────────────────

CREDIT_VALUE_USD = 5.0
INR_RATE = 83
SELL_THRESHOLD = 1  # MVP threshold — 1 tonne. Revisit for production (e.g. 10+ tonnes).


# ── Request models ─────────────────────────────────────────────────────────

class SellRequest(BaseModel):
    credit_ids: List[int]  # DB ids of credits to list


# ── Routes ─────────────────────────────────────────────────────────────────

@router.get("/dashboard")
async def installer_dashboard(user: dict = Depends(require_installer)):
    """
    Installer's own dashboard stats — scoped to their credits only.
    Returns generation data, credit counts by status, and sell-threshold progress.
    """
    user_id = user["id"]

    # Aggregate stats for this installer
    stats = await database.fetch_one(
        query="""SELECT 
            COALESCE(SUM(total_kwh), 0) as total_kwh,
            COALESCE(SUM(co2_avoided_kg), 0) as total_co2_kg,
            COUNT(*) as total_credits,
            MIN(period_start) as period_start,
            MAX(period_end) as period_end
        FROM credits WHERE owner_user_id = :user_id""",
        values={"user_id": user_id}
    )

    # Credits by status
    status_counts = await database.fetch_all(
        query="""SELECT status, COUNT(*) as count 
                 FROM credits WHERE owner_user_id = :user_id 
                 GROUP BY status""",
        values={"user_id": user_id}
    )
    by_status = {row["status"]: row["count"] for row in status_counts}

    verified_count = by_status.get("verified", 0)
    total_credits = dict(stats)["total_credits"]
    total_kwh = dict(stats)["total_kwh"]
    total_co2 = dict(stats)["total_co2_kg"]

    # Devices
    devices = await database.fetch_all(
        query="SELECT device_id, location FROM devices WHERE owner_user_id = :user_id",
        values={"user_id": user_id}
    )

    return {
        "installer": {
            "id": user["id"],
            "email": user["email"],
            "wallet_address": user["wallet_address"],
            "wallet_linked": user["wallet_address"] is not None,
        },
        "stats": {
            "total_kwh": round(total_kwh, 2),
            "total_co2_kg": round(total_co2, 2),
            "total_co2_tonnes": round(total_co2 / 1000, 3),
            "total_credits": total_credits,
            "value_usd": round(total_credits * CREDIT_VALUE_USD, 2),
            "value_inr": round(total_credits * CREDIT_VALUE_USD * INR_RATE, 2),
            "period_start": dict(stats)["period_start"],
            "period_end": dict(stats)["period_end"],
        },
        "credits_by_status": {
            "pending": by_status.get("pending", 0),
            "verified": verified_count,
            "listed": by_status.get("listed", 0),
            "reserved": by_status.get("reserved", 0),
            "sold": by_status.get("sold", 0),
            "retired": by_status.get("retired", 0),
        },
        "sell_threshold": {
            "required": SELL_THRESHOLD,
            "current": verified_count,
            "eligible": verified_count >= SELL_THRESHOLD,
            "remaining": max(0, SELL_THRESHOLD - verified_count),
            "progress_pct": round(min(100, (verified_count / SELL_THRESHOLD) * 100), 1),
        },
        "devices": [dict(d) for d in devices],
    }


@router.get("/credits")
async def installer_credits(
    page: int = 1,
    limit: int = 20,
    status_filter: Optional[str] = None,
    user: dict = Depends(require_installer)
):
    """Paginated list of this installer's credits with optional status filter."""
    user_id = user["id"]
    offset = (page - 1) * limit

    if status_filter:
        credits = await database.fetch_all(
            query="""SELECT id, credit_id, device_id, total_kwh, co2_avoided_kg,
                     period_start, period_end, status, on_chain_id,
                     ipfs_hash, tx_hash, listed_at, sold_at
                     FROM credits 
                     WHERE owner_user_id = :user_id AND status = :status
                     ORDER BY credit_id DESC
                     LIMIT :limit OFFSET :offset""",
            values={"user_id": user_id, "status": status_filter, "limit": limit, "offset": offset}
        )
        total = await database.fetch_one(
            query="SELECT COUNT(*) as cnt FROM credits WHERE owner_user_id = :user_id AND status = :status",
            values={"user_id": user_id, "status": status_filter}
        )
    else:
        credits = await database.fetch_all(
            query="""SELECT id, credit_id, device_id, total_kwh, co2_avoided_kg,
                     period_start, period_end, status, on_chain_id,
                     ipfs_hash, tx_hash, listed_at, sold_at
                     FROM credits 
                     WHERE owner_user_id = :user_id
                     ORDER BY credit_id DESC
                     LIMIT :limit OFFSET :offset""",
            values={"user_id": user_id, "limit": limit, "offset": offset}
        )
        total = await database.fetch_one(
            query="SELECT COUNT(*) as cnt FROM credits WHERE owner_user_id = :user_id",
            values={"user_id": user_id}
        )

    total_count = dict(total)["cnt"]

    return {
        "credits": [dict(c) for c in credits],
        "total": total_count,
        "page": page,
        "pages": max(1, -(-total_count // limit)),
    }


@router.get("/readings")
async def installer_readings(
    page: int = 1,
    limit: int = 20,
    user: dict = Depends(require_installer)
):
    """Paginated list of raw generation readings (15-min intervals)."""
    user_id = user["id"]
    offset = (page - 1) * limit

    readings = await database.fetch_all(
        query="""SELECT reading_id, device_id, total_kwh, co2_avoided_kg,
                 timestamp, period_start, period_end
                 FROM generation_readings 
                 WHERE owner_user_id = :user_id
                 ORDER BY timestamp DESC
                 LIMIT :limit OFFSET :offset""",
        values={"user_id": user_id, "limit": limit, "offset": offset}
    )
    total = await database.fetch_one(
        query="SELECT COUNT(*) as cnt FROM generation_readings WHERE owner_user_id = :user_id",
        values={"user_id": user_id}
    )

    total_count = dict(total)["cnt"]

    return {
        "readings": [dict(r) for r in readings],
        "total": total_count,
        "page": page,
        "pages": max(1, -(-total_count // limit)),
    }

@router.get("/devices")
async def installer_devices(user: dict = Depends(require_installer)):
    """List this installer's registered devices."""
    devices = await database.fetch_all(
        query="""SELECT d.id, d.device_id, d.location, d.created_at,
                 (SELECT COUNT(*) FROM credits WHERE device_id = d.device_id AND owner_user_id = :user_id) as credit_count,
                 (SELECT COALESCE(SUM(total_kwh), 0) FROM credits WHERE device_id = d.device_id AND owner_user_id = :user_id) as total_kwh
                 FROM devices d WHERE d.owner_user_id = :user_id""",
        values={"user_id": user["id"]}
    )
    return {"devices": [dict(d) for d in devices]}


@router.post("/sell")
async def list_credits_for_sale(req: SellRequest, user: dict = Depends(require_installer)):
    """
    List credits for sale on the marketplace.
    Requirements: wallet must be linked, credits must be verified, count >= threshold.
    """
    # Check wallet linked
    if not user["wallet_address"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="You must link a wallet address before selling credits. Go to Settings → Link Wallet."
        )

    if len(req.credit_ids) < SELL_THRESHOLD:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Minimum {SELL_THRESHOLD} credits required to list for sale. You selected {len(req.credit_ids)}."
        )

    # Verify all credits belong to this installer and are in 'verified' status
    placeholders = ", ".join([f":id{i}" for i in range(len(req.credit_ids))])
    values = {f"id{i}": cid for i, cid in enumerate(req.credit_ids)}
    values["user_id"] = user["id"]

    owned_credits = await database.fetch_all(
        query=f"""SELECT id, status FROM credits 
                  WHERE id IN ({placeholders}) AND owner_user_id = :user_id""",
        values=values
    )

    if len(owned_credits) != len(req.credit_ids):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Some of the selected credits don't belong to your account."
        )

    non_verified = [dict(c) for c in owned_credits if dict(c)["status"] != "verified"]
    if non_verified:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{len(non_verified)} credit(s) are not in 'verified' status and cannot be listed."
        )

    # Update status to 'listed'
    now = time.time()
    for credit_id in req.credit_ids:
        await db_execute_with_retry(
            query="UPDATE credits SET status = 'listed', listed_at = :now WHERE id = :id",
            values={"now": now, "id": credit_id}
        )

    return {
        "status": "listed",
        "credits_listed": len(req.credit_ids),
        "price_per_credit_usd": CREDIT_VALUE_USD,
        "price_per_credit_inr": CREDIT_VALUE_USD * INR_RATE,
        "total_value_inr": len(req.credit_ids) * CREDIT_VALUE_USD * INR_RATE,
        "message": f"{len(req.credit_ids)} credits listed for sale on the marketplace."
    }


@router.get("/history")
async def installer_history(
    page: int = 1,
    limit: int = 20,
    user: dict = Depends(require_installer)
):
    """Transaction/payout history for this installer."""
    offset = (page - 1) * limit

    # Credits that have been sold
    sold = await database.fetch_all(
        query="""SELECT id, credit_id, total_kwh, co2_avoided_kg, status,
                 sold_at, buyer_user_id, tx_hash
                 FROM credits 
                 WHERE owner_user_id = :user_id AND status IN ('sold', 'retired')
                 ORDER BY sold_at DESC
                 LIMIT :limit OFFSET :offset""",
        values={"user_id": user["id"], "limit": limit, "offset": offset}
    )

    total = await database.fetch_one(
        query="""SELECT COUNT(*) as cnt FROM credits 
                 WHERE owner_user_id = :user_id AND status IN ('sold', 'retired')""",
        values={"user_id": user["id"]}
    )

    total_count = dict(total)["cnt"]
    total_earned = total_count * CREDIT_VALUE_USD * INR_RATE

    return {
        "transactions": [dict(s) for s in sold],
        "total": total_count,
        "page": page,
        "pages": max(1, -(-total_count // limit)),
        "total_earned_inr": total_earned,
        "total_earned_usd": total_count * CREDIT_VALUE_USD,
    }
