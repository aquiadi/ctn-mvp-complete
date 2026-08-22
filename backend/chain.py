"""
Polygon (Amoy) contract access.

web3.py's HTTP provider is synchronous, so every call here is dispatched to a
worker thread. Calling it directly from a coroutine would stall the event loop
for the full round-trip — a signed transaction can take tens of seconds.
"""

import asyncio
from typing import Optional

from fastapi import HTTPException, status
from web3 import Web3

import config

ABI = [
    {
        "inputs": [
            {"internalType": "address", "name": "recipient", "type": "address"},
            {"internalType": "string", "name": "ipfsHash", "type": "string"},
            {"internalType": "uint256", "name": "energyKwh", "type": "uint256"},
            {"internalType": "uint256", "name": "co2AvoidedKg", "type": "uint256"},
        ],
        "name": "mintCredit",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
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
                    {"internalType": "address", "name": "holder", "type": "address"},
                ],
                "internalType": "struct CarbonCredit.Credit",
                "name": "",
                "type": "tuple",
            }
        ],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "owner",
        "outputs": [{"internalType": "address", "name": "", "type": "address"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "totalCredits",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"internalType": "uint256", "name": "creditId", "type": "uint256"}],
        "name": "retireCredit",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [
            {"internalType": "uint256", "name": "creditId", "type": "uint256"},
            {"internalType": "address", "name": "newHolder", "type": "address"},
        ],
        "name": "transferCredit",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [{"internalType": "uint256", "name": "creditId", "type": "uint256"}],
        "name": "retireCreditFor",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "anonymous": False,
        "inputs": [
            {"indexed": True, "internalType": "uint256", "name": "id", "type": "uint256"},
            {"indexed": False, "internalType": "string", "name": "ipfsHash", "type": "string"},
            {"indexed": False, "internalType": "address", "name": "holder", "type": "address"},
        ],
        "name": "CreditMinted",
        "type": "event",
    },
    {
        "anonymous": False,
        "inputs": [
            {"indexed": True, "internalType": "uint256", "name": "id", "type": "uint256"},
            {"indexed": False, "internalType": "address", "name": "holder", "type": "address"},
        ],
        "name": "CreditRetired",
        "type": "event",
    },
    {
        "anonymous": False,
        "inputs": [
            {"indexed": True, "internalType": "uint256", "name": "id", "type": "uint256"},
            {"indexed": False, "internalType": "address", "name": "newHolder", "type": "address"},
        ],
        "name": "CreditTransferred",
        "type": "event",
    },
]

w3 = Web3(Web3.HTTPProvider(config.AMOY_RPC))
contract = w3.eth.contract(
    address=Web3.to_checksum_address(config.CONTRACT_ADDRESS), abi=ABI
)


class ChainError(RuntimeError):
    """A contract call or transaction failed."""


# ── Helpers ────────────────────────────────────────────────────────────────

def is_configured() -> bool:
    """True when a signing key is available for write operations."""
    return bool(config.PRIVATE_KEY)


def signing_account():
    """The platform wallet used to sign transactions."""
    if not is_configured():
        raise ChainError("PRIVATE_KEY is not configured — on-chain writes are disabled.")
    return w3.eth.account.from_key(config.PRIVATE_KEY)


def require_configured():
    """Reject a request early when the node cannot sign transactions."""
    if not is_configured():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Blockchain writes are disabled — the server has no signing key configured.",
        )


def parse_address(value: str) -> str:
    """Validate and checksum an address, or raise a 400."""
    try:
        return Web3.to_checksum_address(value.strip())
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"'{value}' is not a valid Ethereum address.",
        )


def tx_url(tx_hash: str) -> str:
    return f"{config.EXPLORER}/tx/{tx_hash}"


def contract_url() -> str:
    return f"{config.EXPLORER}/address/{config.CONTRACT_ADDRESS}"


def _to_chain_units(value: float) -> int:
    """Scale a decimal quantity into the integer form the contract stores."""
    return int(round(value * config.ON_CHAIN_SCALE))


def from_chain_units(value: int) -> float:
    """Inverse of _to_chain_units."""
    return value / config.ON_CHAIN_SCALE


# ── Blocking implementations ───────────────────────────────────────────────

def _send(function_call, gas_limit: int) -> str:
    """Sign, send, and await a transaction. Returns the hash, or raises."""
    account = signing_account()
    tx = function_call.build_transaction(
        {
            "from": account.address,
            "nonce": w3.eth.get_transaction_count(account.address),
            "gas": gas_limit,
            "gasPrice": w3.eth.gas_price,
        }
    )
    signed = w3.eth.account.sign_transaction(tx, config.PRIVATE_KEY)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash)

    tx_hex = tx_hash.hex()
    if receipt.status == 0:
        raise ChainError(f"Transaction reverted on-chain. See {tx_url(tx_hex)}")
    return tx_hex


def _mint(recipient: str, ipfs_hash: str, energy_kwh: float, co2_kg: float) -> dict:
    tx_hex = _send(
        contract.functions.mintCredit(
            recipient, ipfs_hash, _to_chain_units(energy_kwh), _to_chain_units(co2_kg)
        ),
        config.MINT_GAS_LIMIT,
    )
    # The contract assigns ids by incrementing totalCredits, so the value read
    # back immediately after a confirmed mint is this credit's on-chain id.
    return {"tx_hash": tx_hex, "on_chain_id": contract.functions.totalCredits().call()}


def _retire(on_chain_id: int) -> str:
    # retireCredit() is holder-only. Credits minted into a installer's wallet
    # cannot be retired by the platform through it, so the owner-only
    # retireCreditFor() is used instead.
    return _send(contract.functions.retireCreditFor(on_chain_id), config.RETIRE_GAS_LIMIT)


def _get_credit(on_chain_id: int) -> Optional[dict]:
    ipfs_hash, energy, co2, timestamp, retired, holder = contract.functions.getCredit(
        on_chain_id
    ).call()

    if not ipfs_hash and int(holder, 16) == 0:
        return None

    return {
        "on_chain_id": on_chain_id,
        "ipfs_hash": ipfs_hash,
        "energy_kwh": from_chain_units(energy),
        "co2_avoided_kg": from_chain_units(co2),
        "timestamp": timestamp,
        "retired": retired,
        "holder": holder,
    }


def _health() -> dict:
    info = {
        "rpc_connected": w3.is_connected(),
        "chain_id": w3.eth.chain_id,
        "contract_owner": contract.functions.owner().call(),
        # Lifetime counter on the shared testnet contract, including credits
        # minted by earlier deployments — not a count of this platform's mints.
        "contract_lifetime_credits": contract.functions.totalCredits().call(),
        "signing_configured": is_configured(),
    }

    if is_configured():
        account = signing_account()
        info["signing_wallet"] = account.address
        info["wallet_balance_matic"] = float(
            round(w3.from_wei(w3.eth.get_balance(account.address), "ether"), 4)
        )
        info["is_owner"] = account.address.lower() == info["contract_owner"].lower()

    return info


# ── Async interface ────────────────────────────────────────────────────────

async def mint(recipient: str, ipfs_hash: str, energy_kwh: float, co2_kg: float) -> dict:
    """Mint a credit to `recipient`. Returns its tx hash and on-chain id."""
    return await asyncio.to_thread(_mint, recipient, ipfs_hash, energy_kwh, co2_kg)


async def retire(on_chain_id: int) -> str:
    """Retire an on-chain credit. Returns the tx hash."""
    return await asyncio.to_thread(_retire, on_chain_id)


async def get_credit(on_chain_id: int) -> Optional[dict]:
    """Read one on-chain credit, or None if that slot was never minted."""
    return await asyncio.to_thread(_get_credit, on_chain_id)


async def health() -> dict:
    """RPC, contract, and wallet status."""
    return await asyncio.to_thread(_health)
