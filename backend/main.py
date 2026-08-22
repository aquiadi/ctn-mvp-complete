"""
CTN API — application wiring, public read routes, and on-chain operations.

Role-scoped functionality lives in the routers under `routes/`.
"""

import asyncio
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

import chain
import config
from auth import require_admin
from database import database, db_execute_with_retry, init_db, shutdown_db
from ipfs_utils import gateway_url, upload_credit_to_ipfs
from rate_limit import limiter
from routes.admin_routes import log_admin_action
from routes.admin_routes import router as admin_router
from routes.auth_routes import router as auth_router
from routes.installer_routes import router as installer_router
from routes.marketplace_routes import router as marketplace_router

FRONTEND_DIR = os.path.join(os.path.dirname(__file__), "..", "frontend")


# ── Application lifecycle ──────────────────────────────────────────────────

async def release_stale_reservations():
    """
    Return credits to the marketplace when a buyer abandons a reservation.

    Without this, a buyer who closes the tab mid-checkout would hold the credit
    indefinitely and the seller could never sell it.
    """
    while True:
        try:
            await asyncio.sleep(config.RESERVATION_CLEANUP_INTERVAL_SECONDS)
            released = await database.execute(
                query="""UPDATE credits
                         SET status = 'listed', reserved_by = NULL, reserved_at = NULL
                         WHERE status = 'reserved' AND reserved_at < :cutoff""",
                values={"cutoff": time.time() - config.RESERVATION_TIMEOUT_SECONDS},
            )
            if released:
                print(f"✓ Released {released} stale reservation(s)")
        except asyncio.CancelledError:
            break
        except Exception as exc:
            print(f"⚠ Reservation cleanup error: {exc}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    for warning in config.validate():
        print(f"⚠ {warning}")

    await init_db()
    cleanup_task = asyncio.create_task(release_stale_reservations())
    try:
        yield
    finally:
        cleanup_task.cancel()
        await shutdown_db()


app = FastAPI(title="CTN API", version="2.1", lifespan=lifespan)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# Credentialed CORS against an explicit allow-list. A wildcard-suffix pattern
# would let any site on a shared hosting provider call the API with the user's
# session attached.
app.add_middleware(
    CORSMiddleware,
    allow_origins=config.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "Cache-Control", "Pragma"],
)

app.include_router(auth_router)
app.include_router(installer_router)
app.include_router(admin_router)
app.include_router(marketplace_router)


# ── Static pages ───────────────────────────────────────────────────────────

def _page(filename: str):
    """
    Serve a frontend page.

    The frontend is deployed separately (Vercel) in production, and a host that
    builds only the backend directory will not have it. Report that plainly
    instead of raising when the file is absent.
    """

    def handler():
        path = os.path.join(FRONTEND_DIR, filename)
        if not os.path.isfile(path):
            raise HTTPException(
                status_code=404,
                detail="This deployment serves the API only — the frontend is hosted separately.",
            )
        return FileResponse(path)

    return handler


for _route, _file in {
    "/": "index.html",
    "/login": "login.html",
    "/app": "app.html",
    "/app/history": "app-history.html",
    "/admin": "admin.html",
    "/marketplace": "marketplace.html",
}.items():
    app.get(_route, include_in_schema=False)(_page(_file))


@app.get("/static/{filename}", include_in_schema=False)
def static_asset(filename: str):
    """Serve the shared frontend modules."""
    if not filename.endswith((".js", ".css")) or "/" in filename or ".." in filename:
        raise HTTPException(404, "Not found")

    path = os.path.join(FRONTEND_DIR, "static", filename)
    if not os.path.isfile(path):
        raise HTTPException(404, "Not found")
    return FileResponse(path)


# ── Platform statistics ────────────────────────────────────────────────────

def _parse_timestamp(value: str):
    try:
        return datetime.fromisoformat(str(value).strip())
    except (TypeError, ValueError):
        return None


def _period_days(period_start: str, period_end: str) -> int:
    """
    Number of days the dataset spans, used to derive daily averages.

    Falls back to 1 so a single-day dataset — or one with unparseable
    timestamps — divides safely instead of producing a zero-division or a
    figure scaled against an unrelated constant.
    """
    start, end = _parse_timestamp(period_start), _parse_timestamp(period_end)
    if not start or not end:
        return 1
    return max(1, (end - start).days)


async def calculate_stats() -> dict:
    """Platform-wide generation, credit, and value totals."""
    row = await database.fetch_one(
        """SELECT COALESCE(SUM(total_kwh), 0)      AS total_kwh,
                  COALESCE(SUM(co2_avoided_kg), 0) AS total_co2,
                  COUNT(*)                         AS total_credits,
                  MIN(period_start)                AS period_start,
                  MAX(period_end)                  AS period_end,
                  MIN(device_id)                   AS device_id
           FROM credits"""
    )

    total_kwh = row["total_kwh"] or 0
    total_co2 = row["total_co2"] or 0
    total_credits = row["total_credits"] or 0
    period_start = row["period_start"] or ""
    period_end = row["period_end"] or ""
    days = _period_days(period_start, period_end)

    return {
        "total_kwh": round(total_kwh, 2),
        "total_co2_kg": round(total_co2, 2),
        "total_co2_tonnes": round(total_co2 / 1000, 2),
        "total_credits": total_credits,
        "period_days": days,
        "daily_avg_kwh": round(total_kwh / days, 2),
        "daily_avg_co2_kg": round(total_co2 / days, 2),
        "daily_avg_credits": round(total_credits / days, 2),
        "monthly_credits": round(total_credits / days * 30, 1),
        "yearly_credits": round(total_credits / days * 365, 1),
        "value_usd": round(total_credits * config.CREDIT_VALUE_USD, 2),
        "value_inr": round(total_credits * config.CREDIT_VALUE_INR, 2),
        "price_per_credit_usd": config.CREDIT_VALUE_USD,
        "price_per_credit_inr": config.CREDIT_VALUE_INR,
        "kg_co2_per_credit": config.KG_CO2_PER_CREDIT,
        "device_id": row["device_id"] or "unknown",
        "period_start": period_start,
        "period_end": period_end,
        "methodology": config.METHODOLOGY,
        "ipfs_master": config.IPFS_SEED_URL,
        "contract": config.CONTRACT_ADDRESS,
        "explorer": chain.contract_url(),
    }


# ── CO2 equivalence ────────────────────────────────────────────────────────

# Published averages used to translate a mass of CO2 into something tangible.
CO2_EQUIVALENTS_KG = {
    "trees_planted_10yr": 21.0,      # sequestration by one tree over 10 years
    "cars_off_road_1yr": 4600.0,     # average passenger car, annual emissions
    "flights_delhi_mumbai": 180.0,   # one economy seat, one way
    "flights_delhi_ny": 8700.0,      # one economy seat, one way
    "km_not_driven": 0.21,           # average passenger car, per km
    "smartphones_charged": 0.008,    # one full charge
}


def co2_equivalents(kg_co2: float) -> dict:
    """Express a mass of CO2 in everyday terms."""
    return {
        name: round(kg_co2 / factor, 3 if factor > 1000 else 1)
        for name, factor in CO2_EQUIVALENTS_KG.items()
    }


# ── Public read routes ─────────────────────────────────────────────────────

@app.get("/healthz", include_in_schema=False)
async def healthz():
    """
    Liveness probe for the hosting platform.

    Confirms the process is up and the database answers, without the heavier
    contract and wallet checks in /api/admin/system-health. Returns 503 so an
    orchestrator restarts the instance if the database is unreachable.
    """
    try:
        await database.fetch_one("SELECT 1")
    except Exception as exc:
        raise HTTPException(
            status_code=503, detail=f"database unavailable: {exc}"
        )
    return {"status": "ok", "version": app.version}


@app.get("/api")
async def api_root():
    row = await database.fetch_one("SELECT COUNT(*) AS cnt FROM credits")
    return {
        "name": "CTN API",
        "version": app.version,
        "credits_loaded": row["cnt"] if row else 0,
        "docs": "/docs",
    }


@app.get("/config")
def public_config():
    """Values the frontend needs so it never has to hardcode them."""
    return {
        "price_per_credit_usd": config.CREDIT_VALUE_USD,
        "price_per_credit_inr": config.CREDIT_VALUE_INR,
        "usd_to_inr": config.USD_TO_INR,
        "kg_co2_per_credit": config.KG_CO2_PER_CREDIT,
        "emission_factor_kg_per_kwh": config.EMISSION_FACTOR_KG_PER_KWH,
        "methodology": config.METHODOLOGY,
        "standard": config.STANDARD,
        "sell_threshold": config.SELL_THRESHOLD,
        "reservation_timeout_minutes": config.RESERVATION_TIMEOUT_MINUTES,
        "contract_address": config.CONTRACT_ADDRESS,
        "explorer": config.EXPLORER,
        "contract_explorer_url": chain.contract_url(),
        "chain_writes_enabled": chain.is_configured(),
    }


@app.get("/stats")
async def get_stats():
    """Dashboard totals — energy, CO2, credits, and value."""
    return await calculate_stats()


@app.get("/credits")
async def get_credits(page: int = 1, limit: int = 20):
    """Paginated public credit ledger."""
    page, limit = max(1, page), max(1, min(limit, 200))

    credits = await database.fetch_all(
        query="""SELECT credit_id, device_id, total_kwh, co2_avoided_kg,
                        period_start, period_end, status, on_chain_id,
                        ipfs_hash, tx_hash, location
                 FROM credits ORDER BY credit_id DESC LIMIT :limit OFFSET :offset""",
        values={"limit": limit, "offset": (page - 1) * limit},
    )
    row = await database.fetch_one("SELECT COUNT(*) AS cnt FROM credits")
    total = row["cnt"] if row else 0

    return {
        "credits": [dict(c) for c in credits],
        "total": total,
        "page": page,
        "pages": max(1, -(-total // limit)),
    }


@app.get("/credits/{credit_id}")
async def get_credit(credit_id: int):
    """A single credit by its public id."""
    credit = await database.fetch_one(
        "SELECT * FROM credits WHERE credit_id = :credit_id", {"credit_id": credit_id}
    )
    if not credit:
        raise HTTPException(404, f"Credit #{credit_id} not found")
    return dict(credit)


@app.get("/value/{credits}")
def credit_value(credits: int):
    """Monetary and CO2 value of a number of credits."""
    if credits < 0:
        raise HTTPException(400, "Credit count cannot be negative")

    kg_co2 = credits * config.KG_CO2_PER_CREDIT
    return {
        "credits": credits,
        "usd": round(credits * config.CREDIT_VALUE_USD, 2),
        "inr": round(credits * config.CREDIT_VALUE_INR, 2),
        "co2_kg": round(kg_co2, 2),
        "co2_equivalents": co2_equivalents(kg_co2),
    }


@app.get("/daily")
async def daily_breakdown():
    """Average generation per day across the dataset."""
    stats = await calculate_stats()
    return {
        "kwh_per_day": stats["daily_avg_kwh"],
        "co2_per_day_kg": stats["daily_avg_co2_kg"],
        "credits_per_day": stats["daily_avg_credits"],
        "inr_per_day": round(stats["daily_avg_credits"] * config.CREDIT_VALUE_INR, 2),
        "period_days": stats["period_days"],
    }


@app.get("/compare/{kg_co2}")
def compare_co2(kg_co2: float):
    """Translate a mass of CO2 into everyday equivalents."""
    if kg_co2 < 0:
        raise HTTPException(400, "CO2 mass cannot be negative")
    return {"kg_co2": kg_co2, "equivalent_to": co2_equivalents(kg_co2)}


# ── On-chain operations ────────────────────────────────────────────────────

@app.post("/mint/{credit_id}")
@limiter.limit(config.CHAIN_WRITE_RATE_LIMIT)
async def mint_credit(
    request: Request,
    credit_id: int,
    recipient: str,
    reason: str = "Admin mint",
    admin: dict = Depends(require_admin),
):
    """
    Mint a verified credit onto the blockchain and record the result.

    The on-chain id, transaction hash, and certificate CID are persisted so
    verification is a direct lookup rather than a scan of the whole contract.
    """
    chain.require_configured()
    recipient_address = chain.parse_address(recipient)

    row = await database.fetch_one(
        "SELECT * FROM credits WHERE credit_id = :credit_id", {"credit_id": credit_id}
    )
    if not row:
        raise HTTPException(404, f"Credit #{credit_id} not found")
    credit = dict(row)

    if credit.get("on_chain_id"):
        raise HTTPException(
            409,
            f"Credit #{credit_id} is already on-chain as #{credit['on_chain_id']} "
            f"(tx {credit.get('tx_hash')}).",
        )

    await log_admin_action(
        admin_id=admin["id"],
        action="mint",
        target_type="credit",
        target_id=str(credit_id),
        reason=reason,
        details=f"recipient={recipient_address}",
    )

    # Pin the certificate for this specific credit if issuance could not.
    ipfs_hash = credit.get("ipfs_hash")
    if not ipfs_hash or ipfs_hash.startswith("local-"):
        ipfs_hash = upload_credit_to_ipfs(credit)

    try:
        result = await chain.mint(
            recipient_address,
            ipfs_hash,
            credit.get("total_kwh") or 0,
            credit.get("co2_avoided_kg") or 0,
        )
    except chain.ChainError as exc:
        raise HTTPException(502, str(exc))

    await db_execute_with_retry(
        query="""UPDATE credits
                 SET on_chain_id = :on_chain_id, tx_hash = :tx_hash,
                     ipfs_hash = :ipfs_hash, minted_at = :now
                 WHERE credit_id = :credit_id""",
        values={
            "on_chain_id": result["on_chain_id"],
            "tx_hash": result["tx_hash"],
            "ipfs_hash": ipfs_hash,
            "now": time.time(),
            "credit_id": credit_id,
        },
    )

    return {
        "status": "minted",
        "credit_id": credit_id,
        "on_chain_id": result["on_chain_id"],
        "recipient": recipient_address,
        "ipfs_hash": ipfs_hash,
        "tx_hash": result["tx_hash"],
        "polygonscan": chain.tx_url(result["tx_hash"]),
        "verify_ipfs": gateway_url(ipfs_hash),
    }


@app.get("/verify/{credit_id}")
async def verify_on_chain(credit_id: int):
    """
    Verify a credit against the blockchain.

    Resolved through the on-chain id recorded at mint time. Matching by value
    is not possible now that every credit represents exactly one tonne — the
    stored quantities are identical across all of them.
    """
    row = await database.fetch_one(
        "SELECT * FROM credits WHERE credit_id = :credit_id", {"credit_id": credit_id}
    )
    if not row:
        raise HTTPException(404, f"Credit #{credit_id} not found")
    credit = dict(row)

    response = {
        "credit_id": credit_id,
        "on_chain": False,
        "status": credit.get("status"),
        "energy_kwh": round(credit.get("total_kwh") or 0, 3),
        "co2_avoided_kg": round(credit.get("co2_avoided_kg") or 0, 3),
        "methodology": credit.get("methodology") or config.METHODOLOGY,
        "standard": credit.get("standard") or config.STANDARD,
        "device_id": credit.get("device_id") or "",
        "period": f"{str(credit.get('period_start') or '')[:10]} → "
                  f"{str(credit.get('period_end') or '')[:10]}",
        "ipfs_master": config.IPFS_SEED_URL,
        "certificate_ipfs": gateway_url(credit.get("ipfs_hash")),
    }

    on_chain_id = credit.get("on_chain_id")
    if not on_chain_id:
        response["detail"] = "Issued and certified, but not yet minted on-chain."
        return response

    try:
        record = await chain.get_credit(on_chain_id)
    except Exception as exc:
        response["chain_error"] = str(exc)
        return response

    if not record:
        response["chain_error"] = f"On-chain record #{on_chain_id} is empty."
        return response

    response.update(
        {
            "on_chain": True,
            "on_chain_id": record["on_chain_id"],
            "on_chain_energy_kwh": record["energy_kwh"],
            "on_chain_co2_avoided_kg": record["co2_avoided_kg"],
            "timestamp": record["timestamp"],
            "retired": record["retired"],
            "holder": record["holder"],
            "ipfs_hash": record["ipfs_hash"],
            "verify_ipfs": gateway_url(record["ipfs_hash"]),
            "tx_hash": credit.get("tx_hash"),
            "polygonscan": chain.tx_url(credit["tx_hash"]) if credit.get("tx_hash")
                           else chain.contract_url(),
            # The recorded quantities should equal what the database holds; a
            # mismatch means the two ledgers have diverged.
            "values_match": (
                round(record["energy_kwh"], 3) == response["energy_kwh"]
                and round(record["co2_avoided_kg"], 3) == response["co2_avoided_kg"]
            ),
        }
    )
    return response


@app.post("/retire/{credit_id}")
@limiter.limit(config.CHAIN_WRITE_RATE_LIMIT)
async def retire_credit(
    request: Request,
    credit_id: int,
    reason: str = "Admin retirement",
    admin: dict = Depends(require_admin),
):
    """
    Permanently retire a minted credit, completing the offset.

    Addressed by the credit's on-chain id, not its database id — the contract
    assigns its own sequence and the two do not correspond.
    """
    chain.require_configured()

    row = await database.fetch_one(
        "SELECT * FROM credits WHERE credit_id = :credit_id", {"credit_id": credit_id}
    )
    if not row:
        raise HTTPException(404, f"Credit #{credit_id} not found")
    credit = dict(row)

    if not credit.get("on_chain_id"):
        raise HTTPException(400, f"Credit #{credit_id} has not been minted on-chain yet.")
    if credit.get("status") == "retired":
        raise HTTPException(409, f"Credit #{credit_id} is already retired.")

    await log_admin_action(
        admin_id=admin["id"],
        action="retire",
        target_type="credit",
        target_id=str(credit_id),
        reason=reason,
        details=f"on_chain_id={credit['on_chain_id']}",
    )

    try:
        tx_hash = await chain.retire(credit["on_chain_id"])
    except chain.ChainError as exc:
        raise HTTPException(502, str(exc))

    await db_execute_with_retry(
        query="""UPDATE credits
                 SET status = 'retired', retired_at = :now, retire_tx_hash = :tx_hash
                 WHERE credit_id = :credit_id""",
        values={"now": time.time(), "tx_hash": tx_hash, "credit_id": credit_id},
    )

    return {
        "status": "retired",
        "credit_id": credit_id,
        "on_chain_id": credit["on_chain_id"],
        "tx_hash": tx_hash,
        "polygonscan": chain.tx_url(tx_hash),
        "message": "Credit permanently retired — the offset is now verified on-chain.",
    }
