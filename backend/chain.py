"""
Polygon (Amoy) contract access.

web3.py's HTTP provider is synchronous, so every call here is dispatched to a
worker thread. Calling it directly from a coroutine would stall the event loop
for the full round-trip — a signed transaction can take tens of seconds.

Writes are split into broadcast and confirmation so the caller can persist a
transaction hash before waiting on it. If the process dies in between, the hash
is on record and the outcome can be reconciled rather than guessed at.
"""

import asyncio
import threading
from typing import Optional

from fastapi import HTTPException, status
from web3 import Web3
from web3.exceptions import TimeExhausted, TransactionNotFound
from web3.logs import DISCARD

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
    # CarbonCreditV2 only: retirement records who the offset is claimed for.
    {
        "inputs": [
            {"internalType": "uint256", "name": "id", "type": "uint256"},
            {"internalType": "string", "name": "beneficiary", "type": "string"},
        ],
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

_contracts = {contract.address.lower(): contract}


def contract_at(address: Optional[str] = None):
    """
    The contract at `address`, or the current one.

    Credits are read and retired on the contract they were minted on, which is
    not necessarily the one new mints go to.
    """
    if not address:
        return contract
    key = address.lower()
    if key not in _contracts:
        _contracts[key] = w3.eth.contract(address=Web3.to_checksum_address(address), abi=ABI)
    return _contracts[key]


def version_at(address: Optional[str] = None) -> int:
    """
    Interface version of the contract at `address`. CONTRACT_VERSION describes
    the current contract; anything else is an earlier V1 deployment.
    """
    if not address or address.lower() == config.CONTRACT_ADDRESS.lower():
        return config.CONTRACT_VERSION
    return 1


class ChainError(RuntimeError):
    """A contract call or transaction failed."""


class ChainReverted(ChainError):
    """The transaction was mined and reverted. Nothing changed on-chain."""


class ChainPending(ChainError):
    """The transaction was broadcast but no receipt arrived in time."""


# One platform wallet signs everything. Nonces are allocated from the node's
# pending count, which two concurrent sends would read identically; serialising
# the build-sign-broadcast step is what keeps them distinct.
_send_lock = threading.Lock()


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

def _broadcast(function_call, gas_limit: int) -> str:
    """Sign and broadcast a transaction. Returns its hash without waiting."""
    account = signing_account()
    with _send_lock:
        tx = function_call.build_transaction(
            {
                "from": account.address,
                "nonce": w3.eth.get_transaction_count(account.address, "pending"),
                "gas": gas_limit,
                "gasPrice": w3.eth.gas_price,
                "chainId": w3.eth.chain_id,
            }
        )
        signed = w3.eth.account.sign_transaction(tx, config.PRIVATE_KEY)
        try:
            return w3.eth.send_raw_transaction(signed.raw_transaction).to_0x_hex()
        except Exception as exc:
            # Some nodes execute the transaction on submission and refuse one
            # that would revert, instead of mining the revert. Either way
            # nothing changed on-chain, and callers should see the same error.
            if "revert" in str(exc).lower():
                raise ChainReverted(f"Transaction would revert: {exc}") from exc
            raise


def _await_receipt(tx_hash: str):
    """Wait for a receipt. Raises ChainReverted or ChainPending."""
    try:
        receipt = w3.eth.wait_for_transaction_receipt(
            tx_hash, timeout=config.TX_RECEIPT_TIMEOUT_SECONDS
        )
    except TimeExhausted:
        raise ChainPending(f"No receipt for {tx_url(tx_hash)} yet; it may still be mined.")
    if receipt.status == 0:
        raise ChainReverted(f"Transaction reverted on-chain. See {tx_url(tx_hash)}")
    return receipt


def _minted_id(receipt) -> int:
    """
    The credit id assigned by a mint, read from its own CreditMinted event.

    Reading totalCredits() after the fact returns whatever the counter is by
    then — another mint landing in between hands this credit someone else's id.
    """
    for event in contract.events.CreditMinted().process_receipt(receipt, errors=DISCARD):
        # Any contract CTN has deployed emits the same event; take the one
        # from the contract the transaction was sent to.
        if event.address.lower() == (receipt["to"] or "").lower():
            return int(event.args.id)
    raise ChainError("Mint succeeded but emitted no CreditMinted event from this contract.")


def _broadcast_mint(recipient: str, ipfs_hash: str, energy_kwh: float, co2_kg: float) -> str:
    return _broadcast(
        contract.functions.mintCredit(
            recipient, ipfs_hash, _to_chain_units(energy_kwh), _to_chain_units(co2_kg)
        ),
        config.MINT_GAS_LIMIT,
    )


def _confirm_mint(tx_hash: str) -> int:
    return _minted_id(_await_receipt(tx_hash))


def _lookup_mint(tx_hash: str) -> Optional[int]:
    """
    Resolve a previously broadcast mint. Returns the id, None if the node has
    never seen the transaction, or raises ChainReverted / ChainPending.
    """
    try:
        receipt = w3.eth.get_transaction_receipt(tx_hash)
    except TransactionNotFound:
        try:
            w3.eth.get_transaction(tx_hash)
        except TransactionNotFound:
            return None
        raise ChainPending(f"{tx_url(tx_hash)} is known to the node but not yet mined.")
    if receipt.status == 0:
        raise ChainReverted(f"Transaction reverted on-chain. See {tx_url(tx_hash)}")
    return _minted_id(receipt)


def _retire(on_chain_id: int, beneficiary: str, address: Optional[str]) -> str:
    # retireCredit() is holder-only. Credits minted into an installer's wallet
    # cannot be retired by the platform through it, so the owner-only
    # retireCreditFor() is used instead. V2 records the beneficiary on-chain.
    target = contract_at(address)
    if version_at(address) >= 2:
        call = target.get_function_by_signature("retireCreditFor(uint256,string)")(
            on_chain_id, beneficiary
        )
    else:
        call = target.get_function_by_signature("retireCreditFor(uint256)")(on_chain_id)
    tx_hash = _broadcast(call, config.RETIRE_GAS_LIMIT)
    _await_receipt(tx_hash)
    return tx_hash


def _get_credit(on_chain_id: int, address: Optional[str] = None) -> Optional[dict]:
    ipfs_hash, energy, co2, timestamp, retired, holder = contract_at(address).functions.getCredit(
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
        "contract_version": config.CONTRACT_VERSION,
        "legacy_contract": config.LEGACY_CONTRACT_ADDRESS,
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

async def broadcast_mint(recipient: str, ipfs_hash: str, energy_kwh: float, co2_kg: float) -> str:
    """Broadcast a mint to `recipient`. Returns the tx hash; does not wait."""
    return await asyncio.to_thread(_broadcast_mint, recipient, ipfs_hash, energy_kwh, co2_kg)


async def confirm_mint(tx_hash: str) -> int:
    """Wait for a broadcast mint and return the on-chain id it was assigned."""
    return await asyncio.to_thread(_confirm_mint, tx_hash)


async def lookup_mint(tx_hash: str) -> Optional[int]:
    """Resolve an earlier mint without waiting. See _lookup_mint."""
    return await asyncio.to_thread(_lookup_mint, tx_hash)


async def retire(on_chain_id: int, beneficiary: str = "", address: Optional[str] = None) -> str:
    """Retire a credit on the contract it was minted on. Returns the tx hash."""
    return await asyncio.to_thread(_retire, on_chain_id, beneficiary, address)


async def get_credit(on_chain_id: int, address: Optional[str] = None) -> Optional[dict]:
    """Read one on-chain credit, or None if that slot was never minted."""
    return await asyncio.to_thread(_get_credit, on_chain_id, address)


async def health() -> dict:
    """RPC, contract, and wallet status."""
    return await asyncio.to_thread(_health)
