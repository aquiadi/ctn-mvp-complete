"""
CTN Auth Routes — signup, login, logout, wallet linking, session management.
"""

import time
import re
from fastapi import APIRouter, HTTPException, Depends, Response, Request, status
from pydantic import BaseModel, EmailStr, field_validator
from typing import Optional

from database import database, db_execute_with_retry
from auth import (
    hash_password, verify_password, create_access_token,
    set_auth_cookie, clear_auth_cookie, get_current_user,
    generate_nonce, verify_wallet_signature,
)

router = APIRouter(prefix="/api/auth", tags=["auth"])


# ── Request models ─────────────────────────────────────────────────────────

class SignupRequest(BaseModel):
    email: str
    password: str
    role: str  # "installer" or "buyer"

    @field_validator("role")
    @classmethod
    def validate_role(cls, v):
        if v not in ("installer", "buyer"):
            raise ValueError("Role must be 'installer' or 'buyer'. Admin accounts cannot be self-registered.")
        return v

    @field_validator("password")
    @classmethod
    def validate_password(cls, v):
        if len(v) < 8:
            raise ValueError("Password must be at least 8 characters")
        return v

    @field_validator("email")
    @classmethod
    def validate_email(cls, v):
        # Basic email validation
        if not re.match(r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$', v):
            raise ValueError("Invalid email address")
        return v.lower().strip()


class LoginRequest(BaseModel):
    email: str
    password: str

    @field_validator("email")
    @classmethod
    def validate_email(cls, v):
        return v.lower().strip()


class LinkWalletRequest(BaseModel):
    wallet_address: str
    nonce: str
    signature: str

    @field_validator("wallet_address")
    @classmethod
    def validate_address(cls, v):
        if not re.match(r'^0x[a-fA-F0-9]{40}$', v):
            raise ValueError("Invalid Ethereum wallet address")
        return v


# ── Routes ─────────────────────────────────────────────────────────────────

@router.post("/signup")
async def signup(req: SignupRequest, response: Response):
    """
    Create a new installer or buyer account.
    Sets an httpOnly session cookie. Admin accounts cannot be self-registered.
    """
    # Check if email already exists
    existing = await database.fetch_one(
        query="SELECT id FROM users WHERE email = :email",
        values={"email": req.email}
    )
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An account with this email already exists"
        )

    # Create user
    user_id = await db_execute_with_retry(
        query="""INSERT INTO users (email, password_hash, role)
                 VALUES (:email, :password_hash, :role)""",
        values={
            "email": req.email,
            "password_hash": hash_password(req.password),
            "role": req.role,
        }
    )

    # Set session cookie
    token = create_access_token(user_id, req.email, req.role)
    set_auth_cookie(response, token)

    return {
        "status": "created",
        "user": {
            "id": user_id,
            "email": req.email,
            "role": req.role,
            "wallet_address": None,
        }
    }


@router.post("/login")
async def login(req: LoginRequest, response: Response):
    """
    Authenticate with email + password.
    Sets an httpOnly session cookie.
    """
    user = await database.fetch_one(
        query="SELECT id, email, password_hash, role, wallet_address FROM users WHERE email = :email",
        values={"email": req.email}
    )
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password"
        )

    if not verify_password(req.password, user["password_hash"]):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password"
        )

    # Set session cookie
    token = create_access_token(user["id"], user["email"], user["role"])
    set_auth_cookie(response, token)

    return {
        "status": "authenticated",
        "user": {
            "id": user["id"],
            "email": user["email"],
            "role": user["role"],
            "wallet_address": user["wallet_address"],
        }
    }


@router.post("/logout")
async def logout(response: Response):
    """Clear the session cookie."""
    clear_auth_cookie(response)
    return {"status": "logged_out"}


@router.get("/me")
async def get_me(user: dict = Depends(get_current_user)):
    """Return the current authenticated user's info."""
    return {
        "user": {
            "id": user["id"],
            "email": user["email"],
            "role": user["role"],
            "wallet_address": user["wallet_address"],
        }
    }


@router.post("/nonce")
async def get_nonce(user: dict = Depends(get_current_user)):
    """
    Generate a random nonce for the wallet signature challenge.
    The user signs this nonce with their wallet to prove ownership.
    """
    nonce = generate_nonce()

    await db_execute_with_retry(
        query="""INSERT INTO wallet_nonces (user_id, nonce)
                 VALUES (:user_id, :nonce)""",
        values={"user_id": user["id"], "nonce": nonce}
    )

    return {"nonce": nonce, "message": f"Sign this message to link your wallet to CTN:\n\n{nonce}"}


@router.post("/link-wallet")
async def link_wallet(req: LinkWalletRequest, user: dict = Depends(get_current_user)):
    """
    Link a wallet address to the current user's account.
    Requires a valid signature of a previously-issued nonce to prove wallet ownership.
    Rejects if the address is already linked to another account.
    """
    # Verify the nonce was issued to this user and hasn't been used
    nonce_record = await database.fetch_one(
        query="""SELECT id FROM wallet_nonces 
                 WHERE user_id = :user_id AND nonce = :nonce AND used = 0""",
        values={"user_id": user["id"], "nonce": req.nonce}
    )
    if not nonce_record:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or expired nonce. Please request a new one."
        )

    # Verify signature
    if not verify_wallet_signature(req.wallet_address, req.nonce, req.signature):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Wallet signature verification failed. Please try again — make sure you're signing with the correct wallet."
        )

    # Check if wallet is already linked to another account
    existing = await database.fetch_one(
        query="SELECT id, email FROM users WHERE wallet_address = :addr AND id != :user_id",
        values={"addr": req.wallet_address, "user_id": user["id"]}
    )
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This wallet address is already linked to another CTN account. Each wallet can only be linked to one account."
        )

    # Mark nonce as used
    await db_execute_with_retry(
        query="UPDATE wallet_nonces SET used = 1 WHERE id = :id",
        values={"id": nonce_record["id"]}
    )

    # Link wallet
    await db_execute_with_retry(
        query="UPDATE users SET wallet_address = :addr, updated_at = :now WHERE id = :id",
        values={
            "addr": req.wallet_address,
            "now": time.time(),
            "id": user["id"],
        }
    )

    return {
        "status": "wallet_linked",
        "wallet_address": req.wallet_address,
        "message": "Wallet successfully linked to your account."
    }


@router.post("/refresh")
async def refresh_token(response: Response, user: dict = Depends(get_current_user)):
    """Issue a fresh session cookie if the current one is still valid."""
    token = create_access_token(user["id"], user["email"], user["role"])
    set_auth_cookie(response, token)
    return {"status": "refreshed"}
