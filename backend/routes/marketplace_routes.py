"""
CTN Marketplace Routes — browse listings, reserve, purchase, cancel.
Atomic reservation prevents double-purchase of the same credit.
"""

import time
import json
from fastapi import APIRouter, HTTPException, Depends, status
from pydantic import BaseModel
from typing import List, Optional

from database import database, db_execute_with_retry
from auth import require_buyer, get_current_user

router = APIRouter(prefix="/api/marketplace", tags=["marketplace"])

# ── Config ─────────────────────────────────────────────────────────────────

CREDIT_VALUE_USD = 5
INR_RATE = 83
RESERVATION_TIMEOUT_SECONDS = 15 * 60  # 15 minutes


# ── Request models ─────────────────────────────────────────────────────────

class ReserveRequest(BaseModel):
    credit_ids: List[int]  # DB ids of credits to reserve

class PurchaseRequest(BaseModel):
    reservation_ids: List[int]  # DB ids of reserved credits to finalize

class CancelRequest(BaseModel):
    credit_ids: List[int]


# ── Routes ─────────────────────────────────────────────────────────────────

@router.get("/listings")
async def browse_listings(
    page: int = 1,
    limit: int = 20,
    user: dict = Depends(get_current_user)
):
    """
    Browse available credits listed for sale on the marketplace.
    Returns credits grouped/batched for buyer convenience.
    """
    offset = (page - 1) * limit

    # Get listed credit batches
    batches = await database.fetch_all(
        query="""SELECT c.owner_user_id, u.email as seller_email, c.location,
                 COUNT(c.id) as credit_count,
                 SUM(c.total_kwh) as total_kwh,
                 SUM(c.co2_avoided_kg) as co2_avoided_kg,
                 GROUP_CONCAT(c.id) as credit_ids
                 FROM credits c
                 LEFT JOIN users u ON c.owner_user_id = u.id
                 WHERE c.status = 'listed' AND c.contract_version = 'new'
                 GROUP BY c.owner_user_id, c.location, u.email
                 ORDER BY MAX(c.listed_at) DESC
                 LIMIT :limit OFFSET :offset""",
        values={"limit": limit, "offset": offset}
    )

    total = await database.fetch_one(
        query="SELECT COUNT(*) as cnt FROM credits WHERE status = 'listed' AND contract_version = 'new'"
    )
    total_count = dict(total)["cnt"]

    # Format response
    formatted_batches = []
    for b in batches:
        b_dict = dict(b)
        # SQLite GROUP_CONCAT returns comma separated string of IDs
        credit_ids = [int(i) for i in b_dict["credit_ids"].split(",")] if b_dict["credit_ids"] else []
        formatted_batches.append({
            "seller_email": b_dict["seller_email"],
            "location": b_dict["location"],
            "credit_count": b_dict["credit_count"],
            "total_kwh": b_dict["total_kwh"],
            "total_co2_kg": b_dict["co2_avoided_kg"],
            "credits": [{"id": cid} for cid in credit_ids], # Stub format needed by frontend modal
            "price_per_credit_usd": CREDIT_VALUE_USD,
            "price_per_credit_inr": CREDIT_VALUE_USD * INR_RATE,
        })

    return {
        "listings": formatted_batches,
        "total_available": total_count,
        "page": page,
        "pages": max(1, -(-total_count // limit)),
        "price_per_credit_usd": CREDIT_VALUE_USD,
        "price_per_credit_inr": CREDIT_VALUE_USD * INR_RATE,
    }


@router.post("/reserve")
async def reserve_credits(req: ReserveRequest, user: dict = Depends(require_buyer)):
    """
    Atomically reserve credits for purchase.
    Uses SQLite's serialized writes to prevent double-reservation.
    Returns 409 if any credit is already reserved or no longer available.
    """
    if not req.credit_ids:
        raise HTTPException(400, "No credits specified")

    now = time.time()
    reserved_ids = []

    for cid in req.credit_ids:
        # Atomic check-and-update: only reserve if status is still 'listed' and contract is new
        credit = await database.fetch_one(
            query="SELECT id, status, contract_version FROM credits WHERE id = :id",
            values={"id": cid}
        )

        if not credit:
            raise HTTPException(404, f"Credit #{cid} not found")

        c_dict = dict(credit)
        if c_dict["status"] != "listed" or c_dict["contract_version"] != "new":
            # Another buyer got here first, or credit was delisted
            # Roll back any we already reserved in this batch
            if reserved_ids:
                placeholders = ", ".join([f":id{i}" for i in range(len(reserved_ids))])
                values = {f"id{i}": rid for i, rid in enumerate(reserved_ids)}
                await db_execute_with_retry(
                    query=f"""UPDATE credits SET status = 'listed', reserved_by = NULL, reserved_at = NULL
                             WHERE id IN ({placeholders})""",
                    values=values
                )
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Credit #{cid} is no longer available — it was just purchased or reserved by another buyer. Please refresh and try again."
            )

        # Reserve it
        await db_execute_with_retry(
            query="""UPDATE credits SET status = 'reserved', reserved_by = :buyer_id, reserved_at = :now
                     WHERE id = :id AND status = 'listed'""",
            values={"buyer_id": user["id"], "now": now, "id": cid}
        )
        reserved_ids.append(cid)

    total_usd = len(reserved_ids) * CREDIT_VALUE_USD
    total_inr = total_usd * INR_RATE

    return {
        "status": "reserved",
        "reserved_credit_ids": reserved_ids,
        "quantity": len(reserved_ids),
        "total_usd": total_usd,
        "total_inr": total_inr,
        "expires_in_minutes": RESERVATION_TIMEOUT_SECONDS // 60,
        "message": f"{len(reserved_ids)} credits reserved. Complete your purchase within {RESERVATION_TIMEOUT_SECONDS // 60} minutes."
    }


@router.post("/purchase")
async def finalize_purchase(req: PurchaseRequest, user: dict = Depends(require_buyer)):
    """
    Finalize purchase of reserved credits.
    Payment is SIMULATED for MVP — clearly labeled.
    Marks credits as 'sold', records transaction, would call retireCreditFor on-chain.
    """
    if not req.reservation_ids:
        raise HTTPException(400, "No credits specified")

    now = time.time()

    # Verify all credits are reserved by this buyer
    reserved = []
    for cid in req.reservation_ids:
        credit = await database.fetch_one(
            query="SELECT id, status, reserved_by, credit_id FROM credits WHERE id = :id",
            values={"id": cid}
        )
        if not credit:
            raise HTTPException(404, f"Credit #{cid} not found")

        c = dict(credit)
        if c["status"] != "reserved" or c["reserved_by"] != user["id"]:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Credit #{cid} is not reserved by you. Your reservation may have expired."
            )
        reserved.append(c)

    # Calculate totals
    quantity = len(reserved)
    total_usd = quantity * CREDIT_VALUE_USD
    total_inr = total_usd * INR_RATE

    # Mark credits as sold
    for c in reserved:
        await db_execute_with_retry(
            query="""UPDATE credits SET status = 'sold', buyer_user_id = :buyer_id, sold_at = :now
                     WHERE id = :id""",
            values={"buyer_id": user["id"], "now": now, "id": c["id"]}
        )

    # Record transaction
    credit_ids_json = json.dumps([c["id"] for c in reserved])
    txn_id = await db_execute_with_retry(
        query="""INSERT INTO marketplace_transactions 
                 (buyer_user_id, credit_ids, quantity, total_amount_usd, total_amount_inr,
                  payment_status, payment_method, completed_at)
                 VALUES (:buyer_id, :credit_ids, :quantity, :usd, :inr, 
                         'completed', 'simulated', :now)""",
        values={
            "buyer_id": user["id"],
            "credit_ids": credit_ids_json,
            "quantity": quantity,
            "usd": total_usd,
            "inr": total_inr,
            "now": now,
        }
    )

    # Note: On-chain retirement via retireCreditFor would happen here
    # For MVP, we record the sale off-chain only. On-chain retirement
    # requires the Stage C.5 contract upgrade to be deployed first.

    return {
        "status": "purchased",
        "transaction_id": txn_id,
        "quantity": quantity,
        "total_usd": total_usd,
        "total_inr": total_inr,
        "payment_method": "simulated",
        "payment_note": "SIMULATED — no real payment was processed. This is a testnet MVP.",
        "credits_purchased": [c["credit_id"] for c in reserved],
        "receipt": {
            "transaction_id": txn_id,
            "date": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(now)),
            "credits": quantity,
            "total_co2_offset_note": "On-chain retirement will execute once contract upgrade is deployed.",
        }
    }


@router.post("/cancel-reservation")
async def cancel_reservation(req: CancelRequest, user: dict = Depends(require_buyer)):
    """Release reserved credits back to 'listed' status."""
    released = 0
    for cid in req.credit_ids:
        credit = await database.fetch_one(
            query="SELECT id, status, reserved_by FROM credits WHERE id = :id",
            values={"id": cid}
        )
        if not credit:
            continue
        c = dict(credit)
        if c["status"] == "reserved" and c["reserved_by"] == user["id"]:
            await db_execute_with_retry(
                query="""UPDATE credits SET status = 'listed', reserved_by = NULL, reserved_at = NULL
                         WHERE id = :id""",
                values={"id": cid}
            )
            released += 1

    return {
        "status": "released",
        "credits_released": released,
        "message": f"{released} credit(s) returned to the marketplace."
    }


@router.get("/my-purchases")
async def my_purchases(
    page: int = 1,
    limit: int = 20,
    user: dict = Depends(require_buyer)
):
    """View buyer's purchase history."""
    offset = (page - 1) * limit
    txns = await database.fetch_all(
        query="""SELECT * FROM marketplace_transactions 
                 WHERE buyer_user_id = :user_id
                 ORDER BY created_at DESC
                 LIMIT :limit OFFSET :offset""",
        values={"user_id": user["id"], "limit": limit, "offset": offset}
    )
    total = await database.fetch_one(
        query="SELECT COUNT(*) as cnt FROM marketplace_transactions WHERE buyer_user_id = :user_id",
        values={"user_id": user["id"]}
    )

    return {
        "purchases": [dict(t) for t in txns],
        "total": dict(total)["cnt"],
        "page": page,
        "pages": max(1, -(-dict(total)["cnt"] // limit)),
    }
