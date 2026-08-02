from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import requests
import json
from web3 import Web3
import os
import hashlib

app = FastAPI(title="CTN API", version="1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"]
)

# ── Config ─────────────────────────────────────────────────────────────────

IPFS_URL = "https://ivory-geographical-lungfish-400.mypinata.cloud/ipfs/bafybeifpn7y2r2rsjtvm4hun3dy63jkp5ah7qxfwhk5u6bemkapmis2qku"
EMISSION_FACTOR = 0.82
CREDIT_VALUE_USD = 5
INR_RATE = 83
TOTAL_DAYS = 34

CONTRACT_ADDRESS = "0x1b4F5A7CEf1c2CFb914A5642CC82F887AB0C7Cf6"
AMOY_RPC = os.getenv("AMOY_RPC", "https://polygon-amoy-bor-rpc.publicnode.com")
EXPLORER = "https://amoy.polygonscan.com"

# ── Load data from IPFS once at startup ────────────────────────────────────

def load_credits_from_ipfs():
    try:
        r = requests.get(IPFS_URL, timeout=30)
        data = r.json()
        if isinstance(data, list):
            return data
        elif "credits" in data:
            return data["credits"]
        else:
            return []
    except Exception as e:
        print(f"IPFS load error: {e}")
        return []

print("Loading credits from IPFS...")
CREDITS = load_credits_from_ipfs()
print(f"Loaded {len(CREDITS)} credits")

# ── Helper ─────────────────────────────────────────────────────────────────

def calculate_stats():
    if not CREDITS:
        return {}
    total_kwh = CREDITS[-1].get("total_kwh", 0)
    total_co2 = CREDITS[-1].get("co2_avoided_kg", 0)
    total_credits = len(CREDITS)
    return {
        "total_kwh": round(total_kwh, 2),
        "total_co2_kg": round(total_co2, 2),
        "total_co2_tonnes": round(total_co2 / 1000, 2),
        "total_credits": total_credits,
        "daily_avg_kwh": round(total_kwh / TOTAL_DAYS, 2),
        "daily_avg_co2_kg": round(total_co2 / TOTAL_DAYS, 2),
        "daily_avg_credits": round(total_credits / TOTAL_DAYS, 1),
        "monthly_credits": round(total_credits / TOTAL_DAYS * 30),
        "yearly_credits": round(total_credits / TOTAL_DAYS * 365),
        "value_usd": round(total_credits * CREDIT_VALUE_USD, 2),
        "value_inr": round(total_credits * CREDIT_VALUE_USD * INR_RATE, 2),
        "device_id": CREDITS[0].get("device_id", "unknown"),
        "period_start": CREDITS[0].get("period_start", ""),
        "period_end": CREDITS[-1].get("period_end", ""),
        "methodology": "CEA Grid Emission Factor 0.82 kg CO2/kWh",
        "ipfs_master": IPFS_URL,
        "contract": CONTRACT_ADDRESS,
        "explorer": f"{EXPLORER}/address/{CONTRACT_ADDRESS}"
    }

# ── Routes ─────────────────────────────────────────────────────────────────

@app.get("/")
def root():
    return {
        "name": "CTN API",
        "version": "1.0",
        "credits_loaded": len(CREDITS),
        "docs": "/docs"
    }

@app.get("/stats")
def get_stats():
    """Full dashboard stats — kWh, CO2, credits, INR value"""
    return calculate_stats()

@app.get("/credits")
def get_credits(page: int = 1, limit: int = 20):
    """Paginated credit list"""
    start = (page - 1) * limit
    end = start + limit
    return {
        "credits": CREDITS[start:end],
        "total": len(CREDITS),
        "page": page,
        "pages": -(-len(CREDITS) // limit)
    }

@app.get("/credits/{credit_id}")
def get_credit(credit_id: int):
    """Single credit by ID"""
    credit = next((c for c in CREDITS if c.get("credit_id") == credit_id), None)
    if not credit:
        raise HTTPException(404, f"Credit #{credit_id} not found")
    return credit

@app.get("/value/{credits}")
def credit_value(credits: int):
    """Calculate INR/USD value of N credits"""
    return {
        "credits": credits,
        "usd": round(credits * CREDIT_VALUE_USD, 2),
        "inr": round(credits * CREDIT_VALUE_USD * INR_RATE, 2),
        "co2_kg": round(credits * 50 * EMISSION_FACTOR, 2),
        "co2_equivalents": {
            "trees_10yr": round(credits * 50 * EMISSION_FACTOR / 21),
            "cars_off_road": round(credits * 50 * EMISSION_FACTOR / 4600),
            "flights_delhi_ny": round(credits * 50 * EMISSION_FACTOR / 8700, 2)
        }
    }

@app.get("/daily")
def daily_breakdown():
    """Average daily generation stats"""
    stats = calculate_stats()
    return {
        "kwh_per_day": stats["daily_avg_kwh"],
        "co2_per_day_kg": stats["daily_avg_co2_kg"],
        "credits_per_day": stats["daily_avg_credits"],
        "inr_per_day": round(stats["daily_avg_credits"] * CREDIT_VALUE_USD * INR_RATE, 2),
        "period_days": TOTAL_DAYS
    }

@app.get("/compare/{kg_co2}")
def compare_co2(kg_co2: float):
    """Compare CO2 to real world equivalents"""
    return {
        "kg_co2": kg_co2,
        "equivalent_to": {
            "trees_planted_10yr": round(kg_co2 / 21),
            "cars_off_road_1yr": round(kg_co2 / 4600, 2),
            "flights_delhi_mumbai": round(kg_co2 / 180, 1),
            "flights_delhi_ny": round(kg_co2 / 8700, 3),
            "km_not_driven": round(kg_co2 / 0.21),
            "smartphones_charged": round(kg_co2 / 0.008)
        }
    }

@app.get("/agent/context")
def agent_context():
    """Pre-built context for Gemma agent"""
    stats = calculate_stats()
    daily = {
        "kwh": stats["daily_avg_kwh"],
        "co2_kg": stats["daily_avg_co2_kg"],
        "credits": stats["daily_avg_credits"]
    }
    return {
        "system_prompt": f"""You are Krishi, CTN's friendly field agent for solar carbon credits in Patna, Bihar, India.

REAL DATA — always use these numbers, never make up values:
Device: {stats['device_id']}
Period: {TOTAL_DAYS} days ({stats['period_start'][:10]} to {stats['period_end'][:10]})

TOTALS:
- Energy generated: {stats['total_kwh']} kWh
- CO2 avoided: {stats['total_co2_kg']} kg ({stats['total_co2_tonnes']} tonnes)
- Credits earned: {stats['total_credits']}
- Value: ${stats['value_usd']} USD / ₹{stats['value_inr']:,} INR

DAILY AVERAGES:
- Per day: {daily['kwh']} kWh, {daily['co2_kg']} kg CO2, {daily['credits']} credits
- Per month: ~{stats['monthly_credits']} credits (~₹{round(stats['monthly_credits'] * CREDIT_VALUE_USD * INR_RATE):,})
- Per year: ~{stats['yearly_credits']} credits (~₹{round(stats['yearly_credits'] * CREDIT_VALUE_USD * INR_RATE):,})

CO2 CONTEXT:
- {stats['total_co2_kg']} kg CO2 = {round(stats['total_co2_kg']/21)} trees planted for 10 years
- {stats['total_co2_kg']} kg CO2 = taking {round(stats['total_co2_kg']/4600, 1)} cars off road for a year

RULES:
- If asked about ONE DAY → use daily averages
- If asked about total → use totals
- Respond in same language as question (Hindi or English)
- Use analogies farmers understand
- Never just repeat numbers — explain what they mean
- End with one actionable suggestion""",
        "stats": stats,
        "daily": daily
    }

# ── Blockchain config ──────────────────────────────────────────────────────

PRIVATE_KEY = os.getenv("PRIVATE_KEY", "").strip().strip('"').strip("'")
if PRIVATE_KEY.startswith("0x") or PRIVATE_KEY.startswith("0X"):
    PRIVATE_KEY = PRIVATE_KEY[2:]

PINATA_API_KEY = os.getenv("PINATA_API_KEY")
PINATA_SECRET = os.getenv("PINATA_SECRET")

w3 = Web3(Web3.HTTPProvider(AMOY_RPC))

ABI = [
    {
        "inputs": [
            {"internalType": "address", "name": "recipient", "type": "address"},
            {"internalType": "string", "name": "ipfsHash", "type": "string"},
            {"internalType": "uint256", "name": "energyKwh", "type": "uint256"},
            {"internalType": "uint256", "name": "co2AvoidedKg", "type": "uint256"}
        ],
        "name": "mintCredit",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function"
    },
    {
        "inputs": [{"internalType": "uint256", "name": "creditId", "type": "uint256"}],
        "name": "getCredit",
        "outputs": [
            {
                "components": [
                    {"internalType": "string", "name": "ipfsHash", "type": "string"},
                    {"internalType": "uint256", "name": "energyKwh", "type": "uint256"},
                    {"internalType": "uint256", "name": "co2AvoidedKg", "type": "uint256"},
                    {"internalType": "uint256", "name": "timestamp", "type": "uint256"},
                    {"internalType": "bool", "name": "retired", "type": "bool"},
                    {"internalType": "address", "name": "holder", "type": "address"}
                ],
                "internalType": "struct CarbonCredit.Credit",
                "name": "",
                "type": "tuple"
            }
        ],
        "stateMutability": "view",
        "type": "function"
    },
    {
        "inputs": [],
        "name": "owner",
        "outputs": [{"internalType": "address", "name": "", "type": "address"}],
        "stateMutability": "view",
        "type": "function"
    },
    {
    "inputs": [
        {"internalType": "uint256", "name": "creditId", "type": "uint256"}
    ],
    "name": "retireCredit",
    "outputs": [],
    "stateMutability": "nonpayable",
    "type": "function"
    }
]

contract = w3.eth.contract(
    address=Web3.to_checksum_address(CONTRACT_ADDRESS),
    abi=ABI
)

# ── Debug endpoint ─────────────────────────────────────────────────────────

@app.get("/debug")
def debug():
    try:
        account = w3.eth.account.from_key(PRIVATE_KEY)
        contract_owner = contract.functions.owner().call()
        balance = w3.eth.get_balance(account.address)
        return {
            "signing_wallet": account.address,
            "contract_owner": contract_owner,
            "is_owner": account.address.lower() == contract_owner.lower(),
            "wallet_balance_matic": round(w3.from_wei(balance, "ether"), 4),
            "chain_id": w3.eth.chain_id,
            "connected": w3.is_connected()
        }
    except Exception as e:
        return {"error": str(e)}

# ── Upload individual credit to IPFS ──────────────────────────────────────

def upload_credit_to_ipfs(credit: dict) -> str:
    url = "https://api.pinata.cloud/pinning/pinJSONToIPFS"
    headers = {
        "pinata_api_key": PINATA_API_KEY,
        "pinata_secret_api_key": PINATA_SECRET
    }
    payload = {
        "pinataContent": credit,
        "pinataMetadata": {"name": f"credit_{credit['credit_id']}"}
    }
    r = requests.post(url, json=payload, headers=headers)
    return r.json()["IpfsHash"]

# ── Mint a single credit on blockchain ───────────────────────────────────

@app.post("/mint/{credit_id}")
def mint_credit(credit_id: int, recipient: str):
    """
    Mint a specific credit on blockchain.
    recipient = wallet address to receive the credit
    """

    # 1. Find credit in loaded data
    credit = next((c for c in CREDITS if c.get("credit_id") == credit_id), None)
    if not credit:
        raise HTTPException(404, f"Credit #{credit_id} not found")

    # 2. Upload this individual credit to IPFS
    individual_hash = upload_credit_to_ipfs(credit)

    # 3. Build blockchain transaction
    account = w3.eth.account.from_key(PRIVATE_KEY)

    tx = contract.functions.mintCredit(
        Web3.to_checksum_address(recipient),
        individual_hash,
        int(credit.get("total_kwh", 0) * 1000),
        int(credit.get("co2_avoided_kg", 0) * 1000)
    ).build_transaction({
        "from": account.address,
        "nonce": w3.eth.get_transaction_count(account.address),
        "gas": 300000,
        "gasPrice": w3.eth.gas_price
    })

    # 4. Sign and send
    signed = w3.eth.account.sign_transaction(tx, PRIVATE_KEY)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash)

    # 5. Check if transaction actually succeeded on-chain
    if receipt.status == 0:
        raise HTTPException(
            500,
            f"Transaction REVERTED on-chain. Check tx on Polygonscan: {EXPLORER}/tx/{tx_hash.hex()}"
        )

    return {
        "status": "minted",
        "credit_id": credit_id,
        "recipient": recipient,
        "individual_ipfs_hash": individual_hash,
        "tx_hash": tx_hash.hex(),
        "polygonscan": f"{EXPLORER}/tx/{tx_hash.hex()}",
        "verify_ipfs": f"https://gateway.pinata.cloud/ipfs/{individual_hash}"
    }

# ── Verify credit on blockchain ───────────────────────────────────────────

@app.get("/verify/{credit_id}")
def verify_on_chain(credit_id: int):
    """Check if a credit exists and is valid on blockchain"""
    try:
        result = contract.functions.getCredit(credit_id).call()
        # Empty credit = never minted
        if result[0] == "" and result[5] == "0x0000000000000000000000000000000000000000":
            return {"credit_id": credit_id, "on_chain": False, "error": "Credit does not exist on chain"}
        return {
            "credit_id": credit_id,
            "on_chain": True,
            "ipfs_hash": result[0],
            "energy_kwh": result[1],
            "co2_avoided_kg": result[2],
            "timestamp": result[3],
            "retired": result[4],
            "holder": result[5],
            "verify_ipfs": f"https://gateway.pinata.cloud/ipfs/{result[0]}",
            "polygonscan": f"{EXPLORER}/tx/{result[0]}"
        }
    except Exception as e:
        return {"credit_id": credit_id, "on_chain": False, "error": str(e)}

# ── Retire credit on blockchain ───────────────────────────────────────────

@app.post("/retire/{credit_id}")
def retire_credit(credit_id: int):
    """Permanently retire a credit — marks it as offset on-chain"""
    try:
        account = w3.eth.account.from_key(PRIVATE_KEY)
        
        tx = contract.functions.retireCredit(credit_id).build_transaction({
            "from": account.address,
            "nonce": w3.eth.get_transaction_count(account.address),
            "gas": 100000,
            "gasPrice": w3.eth.gas_price
        })
        
        signed = w3.eth.account.sign_transaction(tx, PRIVATE_KEY)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash)
        
        if receipt.status == 0:
            raise HTTPException(500, f"Retirement REVERTED. TX: {tx_hash.hex()}")
        
        return {
            "status": "retired",
            "credit_id": credit_id,
            "tx_hash": tx_hash.hex(),
            "polygonscan": f"{EXPLORER}/tx/{tx_hash.hex()}",
            "message": "Credit permanently retired — CO2 offset is now verified on-chain"
        }
    except Exception as e:
        raise HTTPException(500, str(e))
