"""
CTN Auth — JWT sessions, password hashing, role dependencies, wallet verification.

Sessions are carried in an httpOnly cookie. When the frontend is served from a
different origin than the API, browsers that block third-party cookies drop it,
so an `Authorization: Bearer` header is accepted as a fallback.
"""

import os
import secrets
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from eth_account.messages import encode_defunct
from fastapi import Depends, HTTPException, Request, Response, status
from jose import JWTError, jwt
from web3 import Web3

import config
from database import database, pwd_context

# ── Signing key ────────────────────────────────────────────────────────────

_DEV_SECRET_FILE = Path(__file__).parent / ".ctn-dev-secret"


def _resolve_secret() -> str:
    """
    Use the configured secret when present. Outside production, fall back to a
    locally generated key persisted next to the source so restarts don't log
    developers out — deliberately not a constant baked into the repository.
    """
    if config.JWT_SECRET:
        return config.JWT_SECRET

    if config.IS_PRODUCTION:
        raise config.ConfigError("JWT_SECRET must be set in production.")

    if _DEV_SECRET_FILE.exists():
        return _DEV_SECRET_FILE.read_text().strip()

    generated = secrets.token_urlsafe(48)
    _DEV_SECRET_FILE.write_text(generated)
    os.chmod(_DEV_SECRET_FILE, 0o600)
    return generated


SECRET_KEY = _resolve_secret()


# ── JWT helpers ────────────────────────────────────────────────────────────

def create_access_token(user_id: int, email: str, role: str) -> str:
    """Issue a signed session token for the given user."""
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "email": email,
        "role": role,
        "exp": now + timedelta(days=config.TOKEN_EXPIRE_DAYS),
        "iat": now,
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=config.JWT_ALGORITHM)


def decode_token(token: str) -> dict:
    """Decode and validate a session token. Raises JWTError on failure."""
    return jwt.decode(token, SECRET_KEY, algorithms=[config.JWT_ALGORITHM])


def set_auth_cookie(response: Response, token: str):
    """Attach the session token as an httpOnly cookie."""
    response.set_cookie(
        key=config.COOKIE_NAME,
        value=token,
        httponly=True,
        secure=config.COOKIE_SECURE,
        samesite=config.COOKIE_SAMESITE,
        max_age=config.TOKEN_EXPIRE_DAYS * 24 * 3600,
        path="/",
        domain=config.COOKIE_DOMAIN,
    )


def clear_auth_cookie(response: Response):
    """
    Remove the session cookie. The attributes must match those used when it was
    set, or the browser treats it as a different cookie and leaves it in place.
    """
    response.delete_cookie(
        key=config.COOKIE_NAME,
        path="/",
        domain=config.COOKIE_DOMAIN,
        secure=config.COOKIE_SECURE,
        httponly=True,
        samesite=config.COOKIE_SAMESITE,
    )


# ── FastAPI dependencies ───────────────────────────────────────────────────

def _extract_token(request: Request) -> Optional[str]:
    token = request.cookies.get(config.COOKIE_NAME)
    if token:
        return token

    auth_header = request.headers.get("authorization", "")
    if auth_header.lower().startswith("bearer "):
        return auth_header[7:].strip() or None

    return None


async def get_current_user(request: Request) -> dict:
    """Resolve the caller's user record from their session, or raise 401."""
    token = _extract_token(request)
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated — no session cookie or token found",
        )

    try:
        payload = decode_token(token)
    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session expired or invalid — please log in again",
        )

    try:
        user_id = int(payload.get("sub", ""))
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid session token",
        )

    user = await database.fetch_one(
        query="""SELECT id, email, role, wallet_address, created_at
                 FROM users WHERE id = :id AND deleted_at IS NULL""",
        values={"id": user_id},
    )
    if not user:
        # Covers both a deleted row and a closed account whose tokens are still
        # within their expiry window.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="This account is no longer active",
        )

    return dict(user)


async def get_optional_user(request: Request) -> Optional[dict]:
    """Like get_current_user, but returns None instead of raising."""
    try:
        return await get_current_user(request)
    except HTTPException:
        return None


def _require_role(role: str, message: str):
    """Build a dependency that admits only users holding the given role."""

    async def dependency(user: dict = Depends(get_current_user)) -> dict:
        if user["role"] != role:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=message)
        return user

    return dependency


require_installer = _require_role("installer", "This action requires an installer account")
require_buyer = _require_role("buyer", "This action requires a buyer account")
require_admin = _require_role("admin", "This action requires admin access")


# ── Password helpers ───────────────────────────────────────────────────────

def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)


# ── Wallet signature verification ─────────────────────────────────────────

def generate_nonce() -> str:
    """Produce a single-use challenge for a wallet-ownership proof."""
    return f"CTN-AUTH-{secrets.token_hex(16)}-{int(time.time())}"


def verify_wallet_signature(wallet_address: str, nonce: str, signature: str) -> bool:
    """
    Confirm the signature over `nonce` was produced by the key controlling
    `wallet_address`, using the EIP-191 personal_sign format.
    """
    try:
        message = encode_defunct(text=nonce)
        recovered = Web3().eth.account.recover_message(message, signature=signature)
        return recovered.lower() == wallet_address.lower()
    except Exception:
        return False
