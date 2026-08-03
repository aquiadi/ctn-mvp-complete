"""
CTN Auth — JWT (httpOnly cookies), password hashing, role dependencies, wallet verification.
"""

import os
import time
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import Depends, HTTPException, Request, Response, status
from jose import JWTError, jwt
from passlib.context import CryptContext
from web3 import Web3
from eth_account.messages import encode_defunct

from database import database, pwd_context, db_execute_with_retry

# ── Config ─────────────────────────────────────────────────────────────────

SECRET_KEY = os.getenv("JWT_SECRET", "ctn-dev-secret-change-in-production-2024")
ALGORITHM = "HS256"
TOKEN_EXPIRE_DAYS = 7
COOKIE_NAME = "ctn_session"
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "true").lower() == "true"  # Must be True for SameSite=None
COOKIE_DOMAIN = os.getenv("COOKIE_DOMAIN", None)  # e.g. ".ctn.org" in production


# ── JWT helpers ────────────────────────────────────────────────────────────

def create_access_token(user_id: int, email: str, role: str) -> str:
    """Create a JWT token with user claims."""
    expire = datetime.now(timezone.utc) + timedelta(days=TOKEN_EXPIRE_DAYS)
    payload = {
        "sub": str(user_id),
        "email": email,
        "role": role,
        "exp": expire,
        "iat": datetime.now(timezone.utc),
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def decode_token(token: str) -> dict:
    """Decode and validate a JWT token. Raises JWTError on failure."""
    return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])


def set_auth_cookie(response: Response, token: str):
    """Set the JWT as an httpOnly secure cookie."""
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        httponly=True,
        secure=COOKIE_SECURE,
        samesite="none",
        max_age=TOKEN_EXPIRE_DAYS * 24 * 3600,
        path="/",
        domain=COOKIE_DOMAIN,
    )


def clear_auth_cookie(response: Response):
    """Remove the auth cookie."""
    response.delete_cookie(
        key=COOKIE_NAME,
        path="/",
        domain=COOKIE_DOMAIN,
        secure=COOKIE_SECURE,
        httponly=True,
        samesite="none",
    )


# ── FastAPI dependencies ───────────────────────────────────────────────────

async def get_current_user(request: Request) -> dict:
    """
    Extract and validate the JWT from the ctn_session httpOnly cookie.
    Returns the user record from the database.
    """
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated — no session cookie found"
        )

    try:
        payload = decode_token(token)
    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session expired or invalid — please log in again"
        )

    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid session token"
        )

    user = await database.fetch_one(
        query="SELECT id, email, role, wallet_address, created_at FROM users WHERE id = :id",
        values={"id": int(user_id)}
    )
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User no longer exists"
        )

    return dict(user)


async def get_optional_user(request: Request) -> Optional[dict]:
    """Like get_current_user, but returns None instead of raising if not authenticated."""
    try:
        return await get_current_user(request)
    except HTTPException:
        return None


async def require_installer(user: dict = Depends(get_current_user)) -> dict:
    """Require the current user to have the 'installer' role."""
    if user["role"] != "installer":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This action requires an installer account"
        )
    return user


async def require_buyer(user: dict = Depends(get_current_user)) -> dict:
    """Require the current user to have the 'buyer' role."""
    if user["role"] != "buyer":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This action requires a buyer account"
        )
    return user


async def require_admin(user: dict = Depends(get_current_user)) -> dict:
    """Require the current user to have the 'admin' role."""
    if user["role"] != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This action requires admin access"
        )
    return user


# ── Password helpers ───────────────────────────────────────────────────────

def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)


# ── Wallet signature verification ─────────────────────────────────────────

def generate_nonce() -> str:
    """Generate a random nonce for wallet signature challenge."""
    return f"CTN-AUTH-{secrets.token_hex(16)}-{int(time.time())}"


def verify_wallet_signature(wallet_address: str, nonce: str, signature: str) -> bool:
    """
    Verify that the given signature was produced by the private key
    controlling wallet_address, signing the given nonce message.
    Uses EIP-191 personal_sign standard.
    """
    try:
        w3 = Web3()
        message = encode_defunct(text=nonce)
        recovered_address = w3.eth.account.recover_message(message, signature=signature)
        return recovered_address.lower() == wallet_address.lower()
    except Exception:
        return False
