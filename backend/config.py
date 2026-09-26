"""
CTN configuration — single source of truth for every tunable value.

Everything here is overridable via environment variables so the same image can
run locally, on staging, and in production without code changes. Import from
this module rather than redeclaring constants in route files.
"""

import os
import re


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

# Grid emission factor used to convert exported solar energy into avoided CO2.
# The factor is a versioned methodology parameter, not a constant of nature:
# CEA republishes its baseline database every year, and a calculation is only
# auditable if the factor's source and vintage travel with it. The default is
# the CEA v19 weighted average (0.817 t/MWh, FY2022-23) rounded to two places.
EMISSION_FACTOR_KG_PER_KWH = _float("EMISSION_FACTOR", 0.82)
EMISSION_FACTOR_SOURCE = os.getenv(
    "EMISSION_FACTOR_SOURCE",
    "CEA CO2 Baseline Database for the Indian Power Sector, v19.0",
)
EMISSION_FACTOR_VINTAGE = os.getenv("EMISSION_FACTOR_VINTAGE", "FY2022-23")
STANDARD = os.getenv("CREDIT_STANDARD", "CTN-SOLAR-V1")
METHODOLOGY = f"CEA Grid Emission Factor {EMISSION_FACTOR_KG_PER_KWH} kg CO2/kWh"


def methodology_record() -> dict:
    """The methodology parameters every certificate carries, in structured form."""
    return {
        "methodology_id": STANDARD,
        "calculation": "co2_avoided_kg = energy_kwh * factor_value",
        "factor_value": EMISSION_FACTOR_KG_PER_KWH,
        "factor_unit": "kg CO2 / kWh",
        "factor_source": EMISSION_FACTOR_SOURCE,
        "factor_vintage": EMISSION_FACTOR_VINTAGE,
    }

# One credit represents one tonne of CO2 avoided. Readings accumulate until
# this threshold is crossed, at which point a discrete credit is issued.
KG_CO2_PER_CREDIT = _float("KG_CO2_PER_CREDIT", 1000.0)


# ── Physical plausibility ──────────────────────────────────────────────────

# A signature proves who produced a reading, not that it is physically possible.
# These bounds reject readings no real installation could have produced.

# How far ahead of server time a device clock may run. Matches the +/-300 s
# window in the protocol description; NTP-disciplined clocks sit well inside it.
MAX_CLOCK_SKEW_SECONDS = _int("MAX_CLOCK_SKEW_SECONDS", 300)

# How old a reading may be when it arrives. Devices buffer through outages, so
# this is a backfill window rather than a freshness requirement. Anything older
# has to come in through the reviewed import path instead.
MAX_READING_AGE_HOURS = _int("MAX_READING_AGE_HOURS", 72)

# Nameplate AC capacity assumed for a device that did not declare one. Sized for
# a large commercial rooftop so honest devices are never refused; declaring the
# real capacity tightens the bound considerably.
DEFAULT_RATED_CAPACITY_KW = _float("DEFAULT_RATED_CAPACITY_KW", 100.0)

# Headroom over nameplate: metering class tolerance plus brief cloud-edge
# over-irradiance. Anything past this is not a solar array.
CAPACITY_TOLERANCE = _float("CAPACITY_TOLERANCE", 1.10)

# Shortest interval the capacity bound is evaluated over. A device reporting
# every few seconds would otherwise be held to an energy bound far tighter
# than its meter's resolution.
MIN_PLAUSIBILITY_INTERVAL_SECONDS = _int("MIN_PLAUSIBILITY_INTERVAL_SECONDS", 900)

# A V2 reading carries the device's lifetime energy counter. The interval's
# energy must equal the counter's advance to within one meter tick.
METER_CONTINUITY_TOLERANCE_WH = _int("METER_CONTINUITY_TOLERANCE_WH", 1)


# ── Anomaly screening ──────────────────────────────────────────────────────

# Statistical screening runs beside the deterministic rules above. It never
# rejects a reading — it holds the credit for a person to look at.
ANOMALY_SCREENING = _bool("ANOMALY_SCREENING", True)
ANOMALY_MIN_HISTORY = _int("ANOMALY_MIN_HISTORY", 96)          # one day at 15 min
ANOMALY_HISTORY_WINDOW = _int("ANOMALY_HISTORY_WINDOW", 2880)  # thirty days
ANOMALY_ROBUST_Z = _float("ANOMALY_ROBUST_Z", 6.0)
ANOMALY_FLATLINE_RUN = _int("ANOMALY_FLATLINE_RUN", 12)
# Sun this far below the horizon for the whole interval means generation is
# impossible, allowing for refraction and timestamp rounding.
ANOMALY_NIGHT_ELEVATION_DEG = _float("ANOMALY_NIGHT_ELEVATION_DEG", -2.0)


# ── Pricing ────────────────────────────────────────────────────────────────

CREDIT_VALUE_USD = _float("CREDIT_VALUE_USD", 5.0)
USD_TO_INR = _float("USD_TO_INR", 83.0)
CREDIT_VALUE_INR = CREDIT_VALUE_USD * USD_TO_INR


# ── Settlement split ───────────────────────────────────────────────────────

# How each sale's proceeds divide, in basis points (1/100 of a percent): the
# generator who produced the credit, the CTN treasury, and an operational
# reserve. The same split is fixed into the CTNSettlement contract at deploy.
SPLIT_SELLER_BPS = _int("SPLIT_SELLER_BPS", 7000)
SPLIT_TREASURY_BPS = _int("SPLIT_TREASURY_BPS", 2000)
SPLIT_RESERVE_BPS = _int("SPLIT_RESERVE_BPS", 1000)


# ── Marketplace ────────────────────────────────────────────────────────────

# Minimum credits an installer must list in a single batch.
SELL_THRESHOLD = _int("SELL_THRESHOLD", 1)

# How long a buyer's reservation is held before the cleanup task releases it.
RESERVATION_TIMEOUT_MINUTES = _int("RESERVATION_TIMEOUT_MINUTES", 15)
RESERVATION_TIMEOUT_SECONDS = RESERVATION_TIMEOUT_MINUTES * 60
RESERVATION_CLEANUP_INTERVAL_SECONDS = _int("RESERVATION_CLEANUP_INTERVAL_SECONDS", 300)


# ── Blockchain ─────────────────────────────────────────────────────────────

# The original V1 contract. Every credit minted before the move to V2 lives
# there, and credits remember the contract they were minted on, so they stay
# readable and retirable after CONTRACT_ADDRESS moved on.
LEGACY_CONTRACT_ADDRESS = os.getenv(
    "LEGACY_CONTRACT_ADDRESS", "0x1b4F5A7CEf1c2CFb914A5642CC82F887AB0C7Cf6"
)
# Where new credits are minted: CarbonCreditV2 on Amoy.
CONTRACT_ADDRESS = os.getenv("CONTRACT_ADDRESS", "0x890b51626Cc77E41d83fCaa147CF57955d62fA1c")
AMOY_RPC = os.getenv("AMOY_RPC", "https://polygon-amoy-bor-rpc.publicnode.com")
EXPLORER = os.getenv("EXPLORER", "https://amoy.polygonscan.com")

MINT_GAS_LIMIT = _int("MINT_GAS_LIMIT", 300_000)
RETIRE_GAS_LIMIT = _int("RETIRE_GAS_LIMIT", 120_000)

# Interface of the contract at CONTRACT_ADDRESS.
# 2 = CarbonCreditV2 (ERC-721, duplicate-certificate guard, retirement
#     beneficiary). 1 = the original CarbonCredit interface, for pointing a
#     deployment back at a V1 contract.
CONTRACT_VERSION = _int("CONTRACT_VERSION", 2)

# A mint claim older than this with no broadcast transaction is presumed dead
# (process restarted before sending) and may be taken over. One that did
# broadcast is never taken over automatically; see /mint.
MINT_CLAIM_TTL_SECONDS = _int("MINT_CLAIM_TTL_SECONDS", 600)

# Blocks to wait for a receipt before giving up on a transaction.
TX_RECEIPT_TIMEOUT_SECONDS = _int("TX_RECEIPT_TIMEOUT_SECONDS", 180)

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
# link their own address by signing a challenge.
#
# Custody means the platform's own signing wallet. Earlier builds defaulted to
# the contract address, which minted credits to a contract that can neither
# hold nor move them. With no signing key there is no custody wallet, and the
# demo account is simply left without one.
def _custody_address() -> str | None:
    if not PRIVATE_KEY:
        return None
    try:
        from eth_account import Account

        return Account.from_key(PRIVATE_KEY).address
    except Exception:
        return None


# Without a signing key nothing can be minted, but the demo account still
# needs an address to list credits in the marketplace. This one is used only
# then, and startup replaces it with the custody wallet once a key is set, so no
# credit can ever be minted to it.
DEMO_UNCUSTODIED_WALLET = "0x000000000000000000000000000000000000C7a0"

DEMO_INSTALLER_WALLET = (
    os.getenv("DEMO_INSTALLER_WALLET") or _custody_address() or DEMO_UNCUSTODIED_WALLET
)
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
    # A split that does not add up would create or destroy money on every
    # sale, in any environment, so it is fatal everywhere.
    split = (SPLIT_SELLER_BPS, SPLIT_TREASURY_BPS, SPLIT_RESERVE_BPS)
    if sum(split) != 10_000 or min(split) < 0:
        raise ConfigError(
            f"Settlement split must be non-negative and total 10000 bps; got {split}."
        )
    for name, address in (("CONTRACT_ADDRESS", CONTRACT_ADDRESS),
                          ("LEGACY_CONTRACT_ADDRESS", LEGACY_CONTRACT_ADDRESS)):
        if not re.fullmatch(r"0x[0-9a-fA-F]{40}", address or ""):
            raise ConfigError(f"{name} is not a 0x-prefixed 20-byte address: {address!r}")

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
