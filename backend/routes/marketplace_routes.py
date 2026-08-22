"""
CTN Marketplace — browse listings, reserve, purchase, cancel.

Reservation is the concurrency-sensitive step: two buyers must never both be
told they hold the same credit.
"""

import json
import time
from typing import List

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

import config
from auth import get_current_user, require_buyer
from database import database

router = APIRouter(prefix="/api/marketplace", tags=["marketplace"])


# ── Request models ─────────────────────────────────────────────────────────

class CreditSelection(BaseModel):
    credit_ids: List[int] = Field(..., min_length=1, max_length=500)


class PurchaseRequest(BaseModel):
    reservation_ids: List[int] = Field(..., min_length=1, max_length=500)


# ── Helpers ────────────────────────────────────────────────────────────────

def _id_placeholders(prefix: str, ids: List[int]) -> tuple[str, dict]:
    """
    Build a parameterised `IN (...)` clause.

    SQLite has no array binding, so placeholders are generated per id rather
    than interpolating values into the statement.
    """
    keys = [f"{prefix}{i}" for i in range(len(ids))]
    return ", ".join(f":{k}" for k in keys), dict(zip(keys, ids))


def _pricing(quantity: int) -> dict:
    return {
        "quantity": quantity,
        "total_usd": round(quantity * config.CREDIT_VALUE_USD, 2),
        "total_inr": round(quantity * config.CREDIT_VALUE_INR, 2),
    }


# ── Routes ─────────────────────────────────────────────────────────────────

@router.get("/listings")
async def browse_listings(
    page: int = 1,
    limit: int = 20,
    user: dict = Depends(get_current_user),
):
    """Credits currently for sale, grouped into one batch per seller and location."""
    page, limit = max(1, page), max(1, min(limit, 100))

    batches = await database.fetch_all(
        query="""SELECT c.owner_user_id, u.email AS seller_email, c.location,
                        COUNT(c.id)             AS credit_count,
                        SUM(c.total_kwh)        AS total_kwh,
                        SUM(c.co2_avoided_kg)   AS total_co2_kg,
                        GROUP_CONCAT(c.id)      AS credit_ids
                 FROM credits c
                 LEFT JOIN users u ON c.owner_user_id = u.id
                 WHERE c.status = 'listed'
                 GROUP BY c.owner_user_id, c.location, u.email
                 ORDER BY MAX(c.listed_at) DESC
                 LIMIT :limit OFFSET :offset""",
        values={"limit": limit, "offset": (page - 1) * limit},
    )

    row = await database.fetch_one(
        "SELECT COUNT(*) AS cnt FROM credits WHERE status = 'listed'"
    )
    total_available = row["cnt"] if row else 0

    listings = []
    for batch in batches:
        batch = dict(batch)
        credit_ids = [int(i) for i in (batch["credit_ids"] or "").split(",") if i]
        listings.append(
            {
                "seller_email": batch["seller_email"],
                "location": batch["location"],
                "credit_count": batch["credit_count"],
                "total_kwh": round(batch["total_kwh"] or 0, 2),
                "total_co2_kg": round(batch["total_co2_kg"] or 0, 2),
                "credit_ids": credit_ids,
                "price_per_credit_usd": config.CREDIT_VALUE_USD,
                "price_per_credit_inr": config.CREDIT_VALUE_INR,
                "total_price_inr": round(batch["credit_count"] * config.CREDIT_VALUE_INR, 2),
            }
        )

    return {
        "listings": listings,
        "total_available": total_available,
        "page": page,
        "pages": max(1, -(-total_available // limit)),
        "price_per_credit_usd": config.CREDIT_VALUE_USD,
        "price_per_credit_inr": config.CREDIT_VALUE_INR,
        "buyer_can_purchase": user["role"] == "buyer",
    }


@router.post("/reserve")
async def reserve_credits(req: CreditSelection, user: dict = Depends(require_buyer)):
    """
    Hold credits for this buyer while they complete checkout.

    The claim is a single conditional UPDATE, so concurrent buyers contend
    inside SQLite rather than in application code. The rows are then read back
    to confirm how many were actually won — checking availability with a
    separate SELECT first would let two buyers both observe 'listed' and both
    be told they succeeded.
    """
    requested = list(dict.fromkeys(req.credit_ids))
    placeholders, id_values = _id_placeholders("id", requested)
    claim_token = time.time()

    async with database.transaction():
        await database.execute(
            query=f"""UPDATE credits
                      SET status = 'reserved', reserved_by = :buyer_id, reserved_at = :claim
                      WHERE id IN ({placeholders}) AND status = 'listed'""",
            values={"buyer_id": user["id"], "claim": claim_token, **id_values},
        )

        claimed = await database.fetch_all(
            query=f"""SELECT id FROM credits
                      WHERE id IN ({placeholders})
                        AND status = 'reserved'
                        AND reserved_by = :buyer_id
                        AND reserved_at = :claim""",
            values={"buyer_id": user["id"], "claim": claim_token, **id_values},
        )
        claimed_ids = [row["id"] for row in claimed]

        # All-or-nothing: a partial batch would leave the buyer holding credits
        # they never agreed to buy on their own.
        if len(claimed_ids) != len(requested):
            if claimed_ids:
                release_placeholders, release_values = _id_placeholders("rid", claimed_ids)
                await database.execute(
                    query=f"""UPDATE credits
                              SET status = 'listed', reserved_by = NULL, reserved_at = NULL
                              WHERE id IN ({release_placeholders})""",
                    values=release_values,
                )
            unavailable = sorted(set(requested) - set(claimed_ids))
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"{len(unavailable)} of {len(requested)} credits are no longer "
                    "available — another buyer reserved them first. Please refresh and try again."
                ),
            )

    return {
        "status": "reserved",
        "reserved_credit_ids": claimed_ids,
        **_pricing(len(claimed_ids)),
        "expires_in_minutes": config.RESERVATION_TIMEOUT_MINUTES,
        "message": (
            f"{len(claimed_ids)} credit(s) reserved. Complete your purchase within "
            f"{config.RESERVATION_TIMEOUT_MINUTES} minutes."
        ),
    }


@router.post("/purchase")
async def finalize_purchase(req: PurchaseRequest, user: dict = Depends(require_buyer)):
    """
    Complete the purchase of reserved credits.

    Payment is simulated for the MVP; nothing is charged.
    """
    requested = list(dict.fromkeys(req.reservation_ids))
    placeholders, id_values = _id_placeholders("id", requested)
    now = time.time()
    cutoff = now - config.RESERVATION_TIMEOUT_SECONDS

    async with database.transaction():
        held = await database.fetch_all(
            query=f"""SELECT id, credit_id FROM credits
                      WHERE id IN ({placeholders})
                        AND status = 'reserved'
                        AND reserved_by = :buyer_id
                        AND reserved_at >= :cutoff""",
            values={"buyer_id": user["id"], "cutoff": cutoff, **id_values},
        )

        if len(held) != len(requested):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "Some of these credits are no longer reserved by you — your "
                    "reservation may have expired. Please start the purchase again."
                ),
            )

        await database.execute(
            query=f"""UPDATE credits
                      SET status = 'sold', buyer_user_id = :buyer_id, sold_at = :now
                      WHERE id IN ({placeholders})""",
            values={"buyer_id": user["id"], "now": now, **id_values},
        )

        pricing = _pricing(len(held))
        transaction_id = await database.execute(
            query="""INSERT INTO marketplace_transactions
                     (buyer_user_id, credit_ids, quantity, total_amount_usd,
                      total_amount_inr, payment_status, payment_method, completed_at)
                     VALUES (:buyer_id, :credit_ids, :quantity, :usd, :inr,
                             'completed', 'simulated', :now)""",
            values={
                "buyer_id": user["id"],
                "credit_ids": json.dumps([row["id"] for row in held]),
                "quantity": pricing["quantity"],
                "usd": pricing["total_usd"],
                "inr": pricing["total_inr"],
                "now": now,
            },
        )

    return {
        "status": "purchased",
        "transaction_id": transaction_id,
        **pricing,
        "payment_method": "simulated",
        "payment_note": "SIMULATED — no real payment was processed. This is a testnet MVP.",
        "credits_purchased": [row["credit_id"] for row in held],
        "receipt": {
            "transaction_id": transaction_id,
            "date": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(now)),
            "credits": pricing["quantity"],
            "co2_offset_kg": pricing["quantity"] * config.KG_CO2_PER_CREDIT,
            "retirement_note": (
                "An administrator retires these credits on-chain to finalise the offset."
            ),
        },
    }


@router.post("/cancel-reservation")
async def cancel_reservation(req: CreditSelection, user: dict = Depends(require_buyer)):
    """Return this buyer's reserved credits to the marketplace."""
    placeholders, id_values = _id_placeholders("id", list(dict.fromkeys(req.credit_ids)))

    async with database.transaction():
        released = await database.fetch_all(
            query=f"""SELECT id FROM credits
                      WHERE id IN ({placeholders})
                        AND status = 'reserved' AND reserved_by = :buyer_id""",
            values={"buyer_id": user["id"], **id_values},
        )
        if released:
            await database.execute(
                query=f"""UPDATE credits
                          SET status = 'listed', reserved_by = NULL, reserved_at = NULL
                          WHERE id IN ({placeholders})
                            AND status = 'reserved' AND reserved_by = :buyer_id""",
                values={"buyer_id": user["id"], **id_values},
            )

    return {
        "status": "released",
        "credits_released": len(released),
        "message": f"{len(released)} credit(s) returned to the marketplace.",
    }


@router.get("/my-purchases")
async def my_purchases(page: int = 1, limit: int = 20, user: dict = Depends(require_buyer)):
    """This buyer's completed transactions."""
    page, limit = max(1, page), max(1, min(limit, 100))

    transactions = await database.fetch_all(
        query="""SELECT * FROM marketplace_transactions
                 WHERE buyer_user_id = :user_id
                 ORDER BY created_at DESC LIMIT :limit OFFSET :offset""",
        values={"user_id": user["id"], "limit": limit, "offset": (page - 1) * limit},
    )
    row = await database.fetch_one(
        "SELECT COUNT(*) AS cnt FROM marketplace_transactions WHERE buyer_user_id = :user_id",
        {"user_id": user["id"]},
    )
    total = row["cnt"] if row else 0

    return {
        "purchases": [dict(t) for t in transactions],
        "total": total,
        "page": page,
        "pages": max(1, -(-total // limit)),
    }
