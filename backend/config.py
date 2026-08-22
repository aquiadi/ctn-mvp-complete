"""
CTN configuration — single source of truth for every tunable value.

Everything here is overridable via environment variables so the same image can
run locally, on staging, and in production without code changes. Import from
this module rather than redeclaring constants in route files.
"""

import os


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _csv(name: str, default: str) -> list[str]:
    return [item.strip() for item in os.getenv(name, default).split(",") if item.strip()]


# ── Environment ────────────────────────────────────────────────────────────

ENVIRONMENT = os.getenv("ENVIRONMENT", "development").lower()
IS_PRODUCTION = ENVIRONMENT == "production"


# ── Carbon accounting ──────────────────────────────────────────────────────

# Central Electricity Authority grid emission factor for India.
EMISSION_FACTOR_KG_PER_KWH = _float("EMISSION_FACTOR", 0.82)
METHODOLOGY = f"CEA Grid Emission Factor {EMISSION_FACTOR_KG_PER_KWH} kg CO2/kWh"
STANDARD = os.getenv("CREDIT_STANDARD", "CTN-SOLAR-V1")

# One credit represents one tonne of CO2 avoided. Readings accumulate until
# this threshold is crossed, at which point a discrete credit is issued.
KG_CO2_PER_CREDIT = _float("KG_CO2_PER_CREDIT", 1000.0)


# ── Pricing ────────────────────────────────────────────────────────────────

CREDIT_VALUE_USD = _float("CREDIT_VALUE_USD", 5.0)
USD_TO_INR = _float("USD_TO_INR", 83.0)
CREDIT_VALUE_INR = CREDIT_VALUE_USD * USD_TO_INR


# ── Marketplace ────────────────────────────────────────────────────────────

# Minimum credits an installer must list in a single batch.
SELL_THRESHOLD = _int("SELL_THRESHOLD", 1)

# How long a buyer's reservation is held before the cleanup task releases it.
RESERVATION_TIMEOUT_MINUTES = _int("RESERVATION_TIMEOUT_MINUTES", 15)
RESERVATION_TIMEOUT_SECONDS = RESERVATION_TIMEOUT_MINUTES * 60
RESERVATION_CLEANUP_INTERVAL_SECONDS = _int("RESERVATION_CLEANUP_INTERVAL_SECONDS", 300)


# ── Blockchain ─────────────────────────────────────────────────────────────

CONTRACT_ADDRESS = os.getenv("CONTRACT_ADDRESS", "0x1b4F5A7CEf1c2CFb914A5642CC82F887AB0C7Cf6")
AMOY_RPC = os.getenv("AMOY_RPC", "https://polygon-amoy-bor-rpc.publicnode.com")
EXPLORER = os.getenv("EXPLORER", "https://amoy.polygonscan.com")

MINT_GAS_LIMIT = _int("MINT_GAS_LIMIT", 300_000)
RETIRE_GAS_LIMIT = _int("RETIRE_GAS_LIMIT", 100_000)

# The contract stores energy and CO2 as integers. Values are multiplied by this
# factor before being written on-chain so three decimal places survive.
ON_CHAIN_SCALE = 1000

# Signing key for the platform wallet. Absent in local dev, which disables the
# on-chain endpoints rather than crashing at import time.
PRIVATE_KEY = os.getenv("PRIVATE_KEY", "").strip().strip('"').strip("'")
if PRIVATE_KEY[:2].lower() == "0x":
    PRIVATE_KEY = PRIVATE_KEY[2:]


# ── IPFS / Pinata ──────────────────────────────────────────────────────────

PINATA_API_KEY = os.getenv("PINATA_API_KEY")
PINATA_SECRET = os.getenv("PINATA_SECRET")
PINATA_PIN_URL = "https://api.pinata.cloud/pinning/pinJSONToIPFS"
IPFS_GATEWAY = os.getenv("IPFS_GATEWAY", "https://gateway.pinata.cloud/ipfs")

# Seed dataset of raw solar readings, loaded once at startup.
IPFS_SEED_URL = os.getenv(
    "IPFS_SEED_URL",
    "https://ivory-geographical-lungfish-400.mypinata.cloud/ipfs/"
    "bafybeifpn7y2r2rsjtvm4hun3dy63jkp5ah7qxfwhk5u6bemkapmis2qku",
)
IPFS_SEED_TIMEOUT_SECONDS = _int("IPFS_SEED_TIMEOUT_SECONDS", 30)


# ── Database ───────────────────────────────────────────────────────────────

# ctn.db and ctn_v2.db are stale snapshots from earlier credit models and are
# not read by the application; ctn_v3.db holds the current 1-tonne dataset.
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./ctn_v3.db")


# ── Auth ───────────────────────────────────────────────────────────────────

JWT_SECRET = os.getenv("JWT_SECRET", "")
JWT_ALGORITHM = "HS256"
TOKEN_EXPIRE_DAYS = _int("TOKEN_EXPIRE_DAYS", 7)

COOKIE_NAME = "ctn_session"
COOKIE_DOMAIN = os.getenv("COOKIE_DOMAIN") or None

# The frontend may be served from a different origin than the API (Vercel in
# front of Railway), which requires SameSite=None — and browsers only accept
# that on secure cookies. Over plain http://localhost that combination is
# silently dropped, so default to a same-site cookie outside production.
COOKIE_SECURE = _bool("COOKIE_SECURE", IS_PRODUCTION)
COOKIE_SAMESITE = os.getenv("COOKIE_SAMESITE", "none" if COOKIE_SECURE else "lax")

# Brute-force protection on the credential endpoints.
LOGIN_RATE_LIMIT = os.getenv("LOGIN_RATE_LIMIT", "10/minute")
SIGNUP_RATE_LIMIT = os.getenv("SIGNUP_RATE_LIMIT", "5/minute")
CHAIN_WRITE_RATE_LIMIT = os.getenv("CHAIN_WRITE_RATE_LIMIT", "2/minute")

# Devices report continuously and batch while offline, so this is far more
# permissive than the human-facing limits. Replay protection, not throttling,
# is what stops a device flooding the ledger.
INGEST_RATE_LIMIT = os.getenv("INGEST_RATE_LIMIT", "120/minute")
ENROLL_RATE_LIMIT = os.getenv("ENROLL_RATE_LIMIT", "10/minute")

# A pairing code is a bearer credential typed into firmware, so it is short
# lived and single use. Long enough to flash a device, short enough that a
# leaked code is not a standing liability.
ENROLLMENT_CODE_TTL_MINUTES = _int("ENROLLMENT_CODE_TTL_MINUTES", 60)

# Whether a self-enrolled device's credits can be sold before an operator has
# confirmed the installation. Off by default: attestation proves origin, not
# that the meter is measuring real generation.
TRUST_SELF_ENROLLED_DEVICES = _bool("TRUST_SELF_ENROLLED_DEVICES", False)


# ── Seeding ────────────────────────────────────────────────────────────────

ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "admin@ctn.org")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "ctn-admin-2024")
DEMO_INSTALLER_EMAIL = os.getenv("DEMO_INSTALLER_EMAIL", "demo@installer.ctn")
DEMO_INSTALLER_PASSWORD = os.getenv("DEMO_INSTALLER_PASSWORD", "demo-installer-2024")
DEMO_DEVICE_ID = os.getenv("DEMO_DEVICE_ID", "1BY6WEcLGh8j5v7")
DEMO_DEVICE_LOCATION = os.getenv("DEMO_DEVICE_LOCATION", "Patna, Bihar, India")

# Credits earned by the demo installer sit in platform custody, so the demo can
# be minted end to end without a reviewer connecting a wallet. Real installers
# link their own address by signing a challenge. Must be a valid address:
# mintCredit checksums the recipient, so a placeholder string would revert.
DEMO_INSTALLER_WALLET = os.getenv("DEMO_INSTALLER_WALLET", CONTRACT_ADDRESS)
SEED_DEMO_DATA = _bool("SEED_DEMO_DATA", not IS_PRODUCTION)


# ── CORS ───────────────────────────────────────────────────────────────────

# Explicit allow-list. Credentialed CORS plus a wildcard-suffix pattern would
# let any subdomain of a shared hosting provider call the API with the user's
# cookies attached, so hosts are named individually.
CORS_ORIGINS = _csv(
    "CORS_ORIGINS",
    "http://localhost:8000,http://127.0.0.1:8000,"
    "http://localhost:3000,http://127.0.0.1:3000,"
    "http://localhost:5500,http://127.0.0.1:5500,"
    "https://ctn-mvp-complete-j52y.vercel.app",
)


# ── Startup validation ─────────────────────────────────────────────────────

class ConfigError(RuntimeError):
    """Raised when the process is not safely configured for its environment."""


def _is_loopback(origin: str) -> bool:
    return "localhost" in origin or "127.0.0.1" in origin or "[::1]" in origin


def validate() -> list[str]:
    """
    Check configuration coherence. Returns warnings for development, but raises
    on anything that would be a security defect in production.
    """
    problems: list[str] = []

    if not JWT_SECRET:
        problems.append("JWT_SECRET is not set — sessions would be signed with a known key.")
    elif len(JWT_SECRET) < 32:
        problems.append("JWT_SECRET is shorter than 32 characters.")

    if ADMIN_PASSWORD == "ctn-admin-2024":
        problems.append("ADMIN_PASSWORD is still the documented demo password.")

    if not COOKIE_SECURE:
        problems.append("COOKIE_SECURE is off — session cookies would be sent over plain HTTP.")

    # A plain-HTTP loopback origin is harmless (it can only be reached from the
    # same machine); a plain-HTTP public origin exposes credentialed requests
    # on the wire, so only the latter blocks a production start.
    insecure_origins = [
        o for o in CORS_ORIGINS
        if o.startswith("http://") and not _is_loopback(o)
    ]
    if insecure_origins:
        problems.append(
            "CORS_ORIGINS contains plain-HTTP public origins: " + ", ".join(insecure_origins)
        )

    if IS_PRODUCTION and problems:
        raise ConfigError(
            "Refusing to start in production:\n  - " + "\n  - ".join(problems)
        )

    return problems
