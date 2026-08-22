"""
CTN Auth routes — signup, login, logout, session, wallet linking.

Both signup and login return the token in the body as well as setting the
session cookie: when the frontend is served from a different origin, browsers
that block third-party cookies drop the cookie and the client falls back to
sending the token as a bearer header.
"""

import re
import secrets
import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field, field_validator

import config
from auth import (
    clear_auth_cookie,
    create_access_token,
    generate_nonce,
    get_current_user,
    hash_password,
    set_auth_cookie,
    verify_password,
    verify_wallet_signature,
)
from database import database, db_execute_with_retry
from rate_limit import limiter

router = APIRouter(prefix="/api/auth", tags=["auth"])

EMAIL_PATTERN = re.compile(r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$")
ADDRESS_PATTERN = re.compile(r"^0x[a-fA-F0-9]{40}$")

# Signed nonces are short-lived; an old challenge should not stay redeemable.
NONCE_TTL_SECONDS = 10 * 60


# ── Request models ─────────────────────────────────────────────────────────

class EmailField(BaseModel):
    email: str

    @field_validator("email")
    @classmethod
    def normalise_email(cls, value: str) -> str:
        value = value.strip().lower()
        if not EMAIL_PATTERN.match(value):
            raise ValueError("Invalid email address")
        return value


class SignupRequest(EmailField):
    password: str = Field(..., min_length=8, max_length=128)
    role: str

    @field_validator("role")
    @classmethod
    def validate_role(cls, value: str) -> str:
        if value not in ("installer", "buyer"):
            raise ValueError(
                "Role must be 'installer' or 'buyer'. Admin accounts cannot be self-registered."
            )
        return value


class LoginRequest(EmailField):
    password: str


class ChangeEmailRequest(EmailField):
    """Move the account to a new address. Confirmed with the current password."""

    password: str


class DeleteAccountRequest(BaseModel):
    """Close the account. Confirmed with the current password."""

    password: str
    confirm: str

    @field_validator("confirm")
    @classmethod
    def validate_confirm(cls, value: str) -> str:
        if value.strip().upper() != "DELETE":
            raise ValueError("Type DELETE to confirm closing the account.")
        return value


class LinkWalletRequest(BaseModel):
    wallet_address: str
    nonce: str
    signature: str

    @field_validator("wallet_address")
    @classmethod
    def validate_address(cls, value: str) -> str:
        value = value.strip()
        if not ADDRESS_PATTERN.match(value):
            raise ValueError("Invalid Ethereum wallet address")
        return value


# ── Helpers ────────────────────────────────────────────────────────────────

def _session_response(response: Response, user_id: int, email: str, role: str,
                      wallet_address: Optional[str], status_label: str) -> dict:
    token = create_access_token(user_id, email, role)
    set_auth_cookie(response, token)
    return {
        "status": status_label,
        "token": token,
        "user": {
            "id": user_id,
            "email": email,
            "role": role,
            "wallet_address": wallet_address,
        },
    }


# ── Routes ─────────────────────────────────────────────────────────────────

@router.post("/signup")
@limiter.limit(config.SIGNUP_RATE_LIMIT)
async def signup(request: Request, req: SignupRequest, response: Response):
    """Register an installer or buyer account and start a session."""
    existing = await database.fetch_one(
        query="SELECT id FROM users WHERE email = :email AND deleted_at IS NULL",
        values={"email": req.email},
    )
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An account with this email already exists",
        )

    user_id = await db_execute_with_retry(
        query="""INSERT INTO users (email, password_hash, role)
                 VALUES (:email, :password_hash, :role)""",
        values={
            "email": req.email,
            "password_hash": hash_password(req.password),
            "role": req.role,
        },
    )

    return _session_response(response, user_id, req.email, req.role, None, "created")


@router.post("/login")
@limiter.limit(config.LOGIN_RATE_LIMIT)
async def login(request: Request, req: LoginRequest, response: Response):
    """Exchange email and password for a session."""
    user = await database.fetch_one(
        query="""SELECT id, email, password_hash, role, wallet_address
                 FROM users WHERE email = :email AND deleted_at IS NULL""",
        values={"email": req.email},
    )

    # The same message and code for both failure modes, so the response cannot
    # be used to enumerate which addresses have accounts.
    if not user or not verify_password(req.password, user["password_hash"]):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
        )

    return _session_response(
        response, user["id"], user["email"], user["role"],
        user["wallet_address"], "authenticated",
    )


@router.post("/logout")
async def logout(response: Response):
    """Clear the session cookie."""
    clear_auth_cookie(response)
    return {"status": "logged_out"}


@router.get("/me")
async def get_me(user: dict = Depends(get_current_user)):
    """The current session's user."""
    return {
        "user": {
            "id": user["id"],
            "email": user["email"],
            "role": user["role"],
            "wallet_address": user["wallet_address"],
        }
    }


@router.post("/refresh")
async def refresh_token(response: Response, user: dict = Depends(get_current_user)):
    """Extend a still-valid session."""
    set_auth_cookie(response, create_access_token(user["id"], user["email"], user["role"]))
    return {"status": "refreshed"}


@router.get("/profile")
async def get_profile(user: dict = Depends(get_current_user)):
    """Account details plus whatever would block closing it."""
    blockers = await _closure_blockers(user)
    return {
        "user": {
            "id": user["id"],
            "email": user["email"],
            "role": user["role"],
            "wallet_address": user["wallet_address"],
            "created_at": user["created_at"],
        },
        "can_delete": not blockers,
        "delete_blockers": blockers,
    }


@router.post("/change-email")
async def change_email(
    req: ChangeEmailRequest, response: Response, user: dict = Depends(get_current_user)
):
    """
    Move the account to a different address, transferring who controls it.

    The current password is required, so someone with a borrowed session cannot
    quietly take the account over.
    """
    record = await database.fetch_one(
        query="SELECT password_hash FROM users WHERE id = :id",
        values={"id": user["id"]},
    )
    if not record or not verify_password(req.password, record["password_hash"]):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Password is incorrect.",
        )

    if req.email == user["email"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="That is already the address on this account.",
        )

    taken = await database.fetch_one(
        query="SELECT id FROM users WHERE email = :email AND deleted_at IS NULL",
        values={"email": req.email},
    )
    if taken:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Another account already uses that email address.",
        )

    await db_execute_with_retry(
        query="UPDATE users SET email = :email, updated_at = :now WHERE id = :id",
        values={"email": req.email, "now": time.time(), "id": user["id"]},
    )

    # The address is part of the token's claims, so reissue the session.
    set_auth_cookie(response, create_access_token(user["id"], req.email, user["role"]))
    token = create_access_token(user["id"], req.email, user["role"])

    return {
        "status": "email_changed",
        "email": req.email,
        "token": token,
        "message": f"This account is now controlled by {req.email}.",
    }


async def _closure_blockers(user: dict) -> list[str]:
    """
    Reasons this account cannot be closed yet.

    Anything mid-transaction is held back so closing an account cannot strand a
    counterparty's purchase or leave a credit unsellable.
    """
    blockers: list[str] = []

    if user["role"] == "installer":
        row = await database.fetch_one(
            query="""SELECT COUNT(*) AS cnt FROM credits
                     WHERE owner_user_id = :id AND status IN ('listed', 'reserved')""",
            values={"id": user["id"]},
        )
        if row and row["cnt"]:
            blockers.append(
                f"{row['cnt']} credit(s) are listed or reserved on the marketplace. "
                "Remove them from sale first."
            )

    if user["role"] == "buyer":
        row = await database.fetch_one(
            query="""SELECT COUNT(*) AS cnt FROM credits
                     WHERE reserved_by = :id AND status = 'reserved'""",
            values={"id": user["id"]},
        )
        if row and row["cnt"]:
            blockers.append(
                f"{row['cnt']} credit(s) are still reserved by you. "
                "Complete or cancel the purchase first."
            )

    if user["role"] == "admin":
        row = await database.fetch_one(
            "SELECT COUNT(*) AS cnt FROM users WHERE role = 'admin' AND deleted_at IS NULL"
        )
        if row and row["cnt"] <= 1:
            blockers.append(
                "This is the only administrator account. Promote another before closing it."
            )

    return blockers


@router.post("/delete-account")
async def delete_account(
    req: DeleteAccountRequest, response: Response, user: dict = Depends(get_current_user)
):
    """
    Close the account and remove its personal details.

    The row itself is retained and anonymised rather than deleted: credits,
    marketplace transactions, and audit entries reference it, and destroying it
    would leave the ledger unreadable and break the audit trail that makes the
    credits verifiable. Nothing identifying the person survives.
    """
    record = await database.fetch_one(
        query="SELECT password_hash FROM users WHERE id = :id",
        values={"id": user["id"]},
    )
    if not record or not verify_password(req.password, record["password_hash"]):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Password is incorrect.",
        )

    blockers = await _closure_blockers(user)
    if blockers:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=" ".join(blockers),
        )

    now = time.time()
    # The tombstone keeps the unique index satisfied and frees the real address
    # and wallet for reuse. The password hash is replaced with an unusable value.
    tombstone = f"deleted-{user['id']}-{secrets.token_hex(4)}@ctn.invalid"

    await db_execute_with_retry(
        query="""UPDATE users
                 SET email = :email, password_hash = :password_hash,
                     wallet_address = NULL, deleted_at = :now, updated_at = :now
                 WHERE id = :id""",
        values={
            "email": tombstone,
            "password_hash": "!closed",
            "now": now,
            "id": user["id"],
        },
    )

    clear_auth_cookie(response)
    return {
        "status": "account_closed",
        "message": "Your account is closed and your details have been removed. "
                   "Credit and transaction records are retained without them.",
    }


@router.post("/nonce")
async def get_nonce(user: dict = Depends(get_current_user)):
    """
    Issue a challenge for wallet linking. Signing it proves control of the
    address without ever exposing the private key.
    """
    nonce = generate_nonce()
    await db_execute_with_retry(
        query="INSERT INTO wallet_nonces (user_id, nonce) VALUES (:user_id, :nonce)",
        values={"user_id": user["id"], "nonce": nonce},
    )
    return {
        "nonce": nonce,
        "expires_in_seconds": NONCE_TTL_SECONDS,
        "message": f"Sign this message to link your wallet to CTN:\n\n{nonce}",
    }


@router.post("/link-wallet")
async def link_wallet(req: LinkWalletRequest, user: dict = Depends(get_current_user)):
    """Attach a wallet address to this account, proven by a signed nonce."""
    nonce_record = await database.fetch_one(
        query="""SELECT id, created_at FROM wallet_nonces
                 WHERE user_id = :user_id AND nonce = :nonce AND used = 0""",
        values={"user_id": user["id"], "nonce": req.nonce},
    )
    if not nonce_record or nonce_record["created_at"] < time.time() - NONCE_TTL_SECONDS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or expired challenge. Please request a new one.",
        )

    if not verify_wallet_signature(req.wallet_address, req.nonce, req.signature):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Signature verification failed — make sure you are signing with "
                   "the wallet you are linking.",
        )

    taken = await database.fetch_one(
        query="SELECT id FROM users WHERE wallet_address = :addr AND id != :user_id",
        values={"addr": req.wallet_address, "user_id": user["id"]},
    )
    if taken:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This wallet is already linked to another CTN account.",
        )

    # Burn the challenge before linking, so a replay of the same signature
    # cannot succeed even if the update below fails.
    await db_execute_with_retry(
        query="UPDATE wallet_nonces SET used = 1 WHERE id = :id",
        values={"id": nonce_record["id"]},
    )
    await db_execute_with_retry(
        query="UPDATE users SET wallet_address = :addr, updated_at = :now WHERE id = :id",
        values={"addr": req.wallet_address, "now": time.time(), "id": user["id"]},
    )

    return {
        "status": "wallet_linked",
        "wallet_address": req.wallet_address,
        "message": "Wallet successfully linked to your account.",
    }
