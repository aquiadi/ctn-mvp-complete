"""
Settlement: how each sale's proceeds divide.

Every sold credit's price is split between the generator who produced it, the
CTN treasury, and an operational reserve, at the basis points in config
(70/20/10 by default). The CTNSettlement contract applies the same split
on-chain.

Amounts are integer minor units: US cents and Indian paise. Shares are rounded
down and the rounding remainder goes to the reserve, so the three parts of
every sale add back to its price exactly, with no fractional money created or
lost however many credits are sold.

Payments are simulated today, so payouts are recorded with status
'simulated'. When a payment rail is connected, the same rows become 'owed'
and then 'paid', and nothing about the split changes.
"""

from typing import Iterable

import config
from database import database

PAYEES = ("seller", "treasury", "reserve")


def split_bps() -> dict:
    return {
        "seller": config.SPLIT_SELLER_BPS,
        "treasury": config.SPLIT_TREASURY_BPS,
        "reserve": config.SPLIT_RESERVE_BPS,
    }


def split_amount(amount_minor: int) -> dict:
    """Split an integer amount by the configured shares; the parts sum exactly."""
    bps = split_bps()
    seller = amount_minor * bps["seller"] // 10_000
    treasury = amount_minor * bps["treasury"] // 10_000
    return {"seller": seller, "treasury": treasury, "reserve": amount_minor - seller - treasury}


def price_minor() -> tuple[int, int]:
    """One credit's price as (US cents, Indian paise)."""
    return round(config.CREDIT_VALUE_USD * 100), round(config.CREDIT_VALUE_INR * 100)


async def record_sale(transaction_id: int, credits: Iterable[dict], status: str = "simulated") -> dict:
    """
    Record the payouts for one purchase. Call inside the purchase transaction.

    Each credit's seller share goes to that credit's owner, so a basket bought
    from several generators pays each of them for their own credits. Returns
    the totals per payee.
    """
    usd, inr = price_minor()
    usd_parts, inr_parts = split_amount(usd), split_amount(inr)
    bps = split_bps()
    totals = {payee: {"usd_cents": 0, "inr_paise": 0} for payee in PAYEES}

    for credit in credits:
        for payee in PAYEES:
            await database.execute(
                query="""INSERT INTO settlement_payouts
                         (transaction_id, credit_row_id, payee, payee_user_id, share_bps,
                          amount_usd_cents, amount_inr_paise, status)
                         VALUES (:tx, :credit, :payee, :user, :bps, :usd, :inr, :status)""",
                values={
                    "tx": transaction_id,
                    "credit": credit["id"],
                    "payee": payee,
                    "user": credit["owner_user_id"] if payee == "seller" else None,
                    "bps": bps[payee],
                    "usd": usd_parts[payee],
                    "inr": inr_parts[payee],
                    "status": status,
                },
            )
            totals[payee]["usd_cents"] += usd_parts[payee]
            totals[payee]["inr_paise"] += inr_parts[payee]

    return {
        payee: {
            "share_bps": bps[payee],
            "usd": amounts["usd_cents"] / 100,
            "inr": amounts["inr_paise"] / 100,
        }
        for payee, amounts in totals.items()
    }


async def summary() -> dict:
    """Platform-wide totals per payee, and each seller's balance."""
    by_payee = await database.fetch_all(
        """SELECT payee, status, COUNT(DISTINCT credit_row_id) AS credits,
                  SUM(amount_usd_cents) AS usd, SUM(amount_inr_paise) AS inr
           FROM settlement_payouts GROUP BY payee, status"""
    )
    sellers = await database.fetch_all(
        """SELECT p.payee_user_id AS user_id, u.email, COUNT(*) AS credits_sold,
                  SUM(p.amount_usd_cents) AS usd, SUM(p.amount_inr_paise) AS inr
           FROM settlement_payouts p LEFT JOIN users u ON u.id = p.payee_user_id
           WHERE p.payee = 'seller'
           GROUP BY p.payee_user_id, u.email ORDER BY usd DESC"""
    )
    return {
        "split_bps": split_bps(),
        "totals": [
            {"payee": r["payee"], "status": r["status"], "credits": r["credits"],
             "usd": (r["usd"] or 0) / 100, "inr": (r["inr"] or 0) / 100}
            for r in by_payee
        ],
        "sellers": [
            {"user_id": r["user_id"], "email": r["email"], "credits_sold": r["credits_sold"],
             "usd": (r["usd"] or 0) / 100, "inr": (r["inr"] or 0) / 100}
            for r in sellers
        ],
    }


async def seller_earnings(user_id: int) -> dict:
    row = await database.fetch_one(
        query="""SELECT COUNT(*) AS credits_sold, COALESCE(SUM(amount_usd_cents), 0) AS usd,
                        COALESCE(SUM(amount_inr_paise), 0) AS inr
                 FROM settlement_payouts WHERE payee = 'seller' AND payee_user_id = :user""",
        values={"user": user_id},
    )
    return {
        "share_bps": config.SPLIT_SELLER_BPS,
        "credits_sold": row["credits_sold"],
        "usd": row["usd"] / 100,
        "inr": row["inr"] / 100,
        "status": "simulated",
    }
