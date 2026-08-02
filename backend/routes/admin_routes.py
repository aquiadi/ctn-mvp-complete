"""
CTN Admin Routes — full visibility, manual mint/retire with audit logging, system health.
All routes require admin role.
"""

import time
from fastapi import APIRouter, HTTPException, Depends, status
from pydantic import BaseModel
from typing import Optional

from database import database, db_execute_with_retry
from auth import require_admin

router = APIRouter(prefix="/api/admin", tags=["admin"])


# ── Request models ─────────────────────────────────────────────────────────

class AdminMintRequest(BaseModel):
    recipient: str
    reason: str  # Required — every admin action must be justified

class AdminRetireRequest(BaseModel):
    reason: str

class AdminTransferRequest(BaseModel):
    new_holder: str
    reason: str


# ── Audit logging helper ──────────────────────────────────────────────────

async def log_admin_action(admin_id: int, action: str, target_type: str,
                            target_id: str, reason: str, details: str = None):
    """Record every admin action with identity, timestamp, and reason."""
    await db_execute_with_retry(
        query="""INSERT INTO audit_log (admin_user_id, action, target_type, target_id, reason, details)
                 VALUES (:admin_id, :action, :target_type, :target_id, :reason, :details)""",
        values={
            "admin_id": admin_id,
            "action": action,
            "target_type": target_type,
            "target_id": str(target_id),
            "reason": reason,
            "details": details,
        }
    )


# ── Routes ─────────────────────────────────────────────────────────────────

@router.get("/overview")
async def admin_overview(admin: dict = Depends(require_admin)):
    """Platform-wide overview for admin dashboard."""
    users = await database.fetch_one(
        query="""SELECT 
            COUNT(*) as total,
            SUM(CASE WHEN role='installer' THEN 1 ELSE 0 END) as installers,
            SUM(CASE WHEN role='buyer' THEN 1 ELSE 0 END) as buyers,
            SUM(CASE WHEN role='admin' THEN 1 ELSE 0 END) as admins
        FROM users"""
    )
    credits = await database.fetch_one(
        query="""SELECT
            COUNT(*) as total,
            SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END) as pending,
            SUM(CASE WHEN status='verified' THEN 1 ELSE 0 END) as verified,
            SUM(CASE WHEN status='listed' THEN 1 ELSE 0 END) as listed,
            SUM(CASE WHEN status='reserved' THEN 1 ELSE 0 END) as reserved,
            SUM(CASE WHEN status='sold' THEN 1 ELSE 0 END) as sold,
            SUM(CASE WHEN status='retired' THEN 1 ELSE 0 END) as retired,
            COALESCE(SUM(total_kwh), 0) as total_kwh,
            COALESCE(SUM(co2_avoided_kg), 0) as total_co2_kg
        FROM credits"""
    )
    transactions = await database.fetch_one(
        query="""SELECT
            COUNT(*) as total,
            SUM(CASE WHEN payment_status='completed' THEN 1 ELSE 0 END) as completed,
            COALESCE(SUM(CASE WHEN payment_status='completed' THEN total_amount_inr ELSE 0 END), 0) as total_inr
        FROM marketplace_transactions"""
    )

    return {
        "users": dict(users),
        "credits": dict(credits),
        "transactions": dict(transactions),
    }


@router.get("/installers")
async def list_installers(admin: dict = Depends(require_admin)):
    """List all installer accounts with their stats."""
    installers = await database.fetch_all(
        query="""SELECT u.id, u.email, u.wallet_address, u.created_at,
                 (SELECT COUNT(*) FROM credits WHERE owner_user_id = u.id) as credit_count,
                 (SELECT COUNT(*) FROM devices WHERE owner_user_id = u.id) as device_count,
                 (SELECT COALESCE(SUM(total_kwh), 0) FROM credits WHERE owner_user_id = u.id) as total_kwh
                 FROM users u WHERE u.role = 'installer'
                 ORDER BY u.created_at DESC"""
    )
    return {"installers": [dict(i) for i in installers]}


@router.get("/credits")
async def list_all_credits(
    page: int = 1,
    limit: int = 50,
    status_filter: Optional[str] = None,
    owner_id: Optional[int] = None,
    admin: dict = Depends(require_admin)
):
    """List all credits with optional filters."""
    offset = (page - 1) * limit
    conditions = []
    values = {"limit": limit, "offset": offset}

    if status_filter:
        conditions.append("c.status = :status")
        values["status"] = status_filter
    if owner_id:
        conditions.append("c.owner_user_id = :owner_id")
        values["owner_id"] = owner_id

    where = "WHERE " + " AND ".join(conditions) if conditions else ""

    credits = await database.fetch_all(
        query=f"""SELECT c.*, u.email as owner_email
                  FROM credits c
                  LEFT JOIN users u ON c.owner_user_id = u.id
                  {where}
                  ORDER BY c.id DESC
                  LIMIT :limit OFFSET :offset""",
        values=values
    )
    total = await database.fetch_one(
        query=f"SELECT COUNT(*) as cnt FROM credits c {where}",
        values={k: v for k, v in values.items() if k not in ('limit', 'offset')}
    )

    return {
        "credits": [dict(c) for c in credits],
        "total": dict(total)["cnt"],
        "page": page,
        "pages": max(1, -(-dict(total)["cnt"] // limit)),
    }


@router.get("/transactions")
async def list_transactions(
    page: int = 1,
    limit: int = 50,
    admin: dict = Depends(require_admin)
):
    """List all marketplace transactions."""
    offset = (page - 1) * limit
    txns = await database.fetch_all(
        query="""SELECT mt.*, u.email as buyer_email
                 FROM marketplace_transactions mt
                 LEFT JOIN users u ON mt.buyer_user_id = u.id
                 ORDER BY mt.created_at DESC
                 LIMIT :limit OFFSET :offset""",
        values={"limit": limit, "offset": offset}
    )
    total = await database.fetch_one(
        query="SELECT COUNT(*) as cnt FROM marketplace_transactions"
    )
    return {
        "transactions": [dict(t) for t in txns],
        "total": dict(total)["cnt"],
        "page": page,
        "pages": max(1, -(-dict(total)["cnt"] // limit)),
    }


@router.get("/audit-log")
async def get_audit_log(
    page: int = 1,
    limit: int = 50,
    admin: dict = Depends(require_admin)
):
    """View the admin audit trail."""
    offset = (page - 1) * limit
    logs = await database.fetch_all(
        query="""SELECT al.*, u.email as admin_email
                 FROM audit_log al
                 LEFT JOIN users u ON al.admin_user_id = u.id
                 ORDER BY al.created_at DESC
                 LIMIT :limit OFFSET :offset""",
        values={"limit": limit, "offset": offset}
    )
    total = await database.fetch_one(
        query="SELECT COUNT(*) as cnt FROM audit_log"
    )
    return {
        "logs": [dict(l) for l in logs],
        "total": dict(total)["cnt"],
        "page": page,
        "pages": max(1, -(-dict(total)["cnt"] // limit)),
    }


@router.get("/credit/{credit_id}")
async def credit_detail(credit_id: int, admin: dict = Depends(require_admin)):
    """Full lifecycle view of a single credit (for dispute resolution)."""
    credit = await database.fetch_one(
        query="""SELECT c.*, u.email as owner_email, 
                 b.email as buyer_email
                 FROM credits c
                 LEFT JOIN users u ON c.owner_user_id = u.id
                 LEFT JOIN users b ON c.buyer_user_id = b.id
                 WHERE c.id = :id""",
        values={"id": credit_id}
    )
    if not credit:
        raise HTTPException(404, f"Credit #{credit_id} not found")

    # Related audit log entries
    audit = await database.fetch_all(
        query="""SELECT al.*, u.email as admin_email
                 FROM audit_log al
                 LEFT JOIN users u ON al.admin_user_id = u.id
                 WHERE al.target_type = 'credit' AND al.target_id = :id
                 ORDER BY al.created_at DESC""",
        values={"id": str(credit_id)}
    )

    return {
        "credit": dict(credit),
        "audit_trail": [dict(a) for a in audit],
    }


@router.post("/mint/{credit_id}")
async def admin_mint(
    credit_id: int,
    req: AdminMintRequest,
    admin: dict = Depends(require_admin)
):
    """
    Manual mint override for admin — wraps the existing mint logic
    and records an audit log entry with admin identity + reason.
    """
    if not req.reason or len(req.reason.strip()) < 5:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="A reason is required for admin overrides (minimum 5 characters)."
        )

    # Log the action BEFORE attempting — so even failed attempts are audited
    await log_admin_action(
        admin_id=admin["id"],
        action="mint",
        target_type="credit",
        target_id=str(credit_id),
        reason=req.reason,
        details=f"recipient={req.recipient}"
    )

    # Call the actual mint endpoint logic (imported from main)
    # Note: the actual blockchain call happens in main.py's mint_credit function
    # For now, we return success — the actual call would be done via internal function
    return {
        "status": "audit_logged",
        "message": f"Admin mint for credit #{credit_id} logged. Use POST /mint/{credit_id}?recipient={req.recipient} with admin auth to execute.",
        "audit": {
            "admin": admin["email"],
            "action": "mint",
            "reason": req.reason,
            "credit_id": credit_id,
            "recipient": req.recipient,
        }
    }


@router.post("/retire/{credit_id}")
async def admin_retire(
    credit_id: int,
    req: AdminRetireRequest,
    admin: dict = Depends(require_admin)
):
    """Manual retire override for admin — with audit logging."""
    if not req.reason or len(req.reason.strip()) < 5:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="A reason is required for admin overrides (minimum 5 characters)."
        )

    await log_admin_action(
        admin_id=admin["id"],
        action="retire",
        target_type="credit",
        target_id=str(credit_id),
        reason=req.reason,
    )

    return {
        "status": "audit_logged",
        "message": f"Admin retire for credit #{credit_id} logged. Use POST /retire/{credit_id} with admin auth to execute on-chain.",
        "audit": {
            "admin": admin["email"],
            "action": "retire",
            "reason": req.reason,
            "credit_id": credit_id,
        }
    }


@router.get("/system-health")
async def system_health(admin: dict = Depends(require_admin)):
    """System health check — contract owner, wallet balance, RPC status."""
    from main import w3, contract, PRIVATE_KEY, CONTRACT_ADDRESS, EXPLORER

    health = {
        "contract_address": CONTRACT_ADDRESS,
        "explorer": f"{EXPLORER}/address/{CONTRACT_ADDRESS}",
    }

    try:
        account = w3.eth.account.from_key(PRIVATE_KEY)
        contract_owner = contract.functions.owner().call()
        balance = w3.eth.get_balance(account.address)
        total_on_chain = contract.functions.totalCredits().call()

        health.update({
            "rpc_connected": w3.is_connected(),
            "chain_id": w3.eth.chain_id,
            "signing_wallet": account.address,
            "contract_owner": contract_owner,
            "is_owner": account.address.lower() == contract_owner.lower(),
            "wallet_balance_matic": round(w3.from_wei(balance, "ether"), 4),
            "total_on_chain_credits": total_on_chain,
            "status": "healthy" if w3.is_connected() and account.address.lower() == contract_owner.lower() else "degraded"
        })
    except Exception as e:
        health.update({
            "status": "error",
            "error": str(e),
        })

    # DB stats
    try:
        db_credits = await database.fetch_one("SELECT COUNT(*) as cnt FROM credits")
        db_users = await database.fetch_one("SELECT COUNT(*) as cnt FROM users")
        health["db_credits"] = dict(db_credits)["cnt"]
        health["db_users"] = dict(db_users)["cnt"]
    except Exception as e:
        health["db_error"] = str(e)

    return health
