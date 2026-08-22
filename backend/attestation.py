"""
Device attestation for generation readings.

A sensor holds a secp256k1 private key that never leaves it, and signs every
reading it emits. The platform stores only the derived public address, so it can
verify a reading came from that device without ever being able to forge one.

This is what separates a measured reading from an asserted one. A hash computed
by the server proves nothing about origin — anyone able to write the row can
write a matching hash. A signature recovered against a key the server does not
hold cannot be manufactured after the fact.

The canonical message below is part of the public contract: firmware in any
language must reproduce it byte for byte, so its construction is fixed and
deliberately free of ambiguity.
"""

from typing import Optional

from eth_account import Account
from eth_account.messages import encode_defunct
from eth_utils import to_checksum_address

# Bumping this invalidates every previously issued signature, so it changes only
# if the signed field set itself changes.
MESSAGE_VERSION = "CTN-READING-V1"

# Fixed precision for the energy field. Floats have no single textual form, so a
# device writing "0.61" and a server reading 0.6100000000000001 would otherwise
# disagree on what was signed.
KWH_DECIMALS = 6


class AttestationError(ValueError):
    """A reading's signature could not be verified against its device."""


def canonical_message(
    device_id: str, sequence: int, timestamp: str, delta_kwh: float
) -> str:
    """
    The exact text a device signs.

    Newline-delimited and explicitly labelled so it is readable in firmware logs
    and cannot be reordered. Every field that affects the credit is covered:
    omitting any one of them would let it be altered in transit.
    """
    return (
        f"{MESSAGE_VERSION}\n"
        f"device:{device_id}\n"
        f"sequence:{sequence}\n"
        f"timestamp:{timestamp}\n"
        f"delta_kwh:{delta_kwh:.{KWH_DECIMALS}f}"
    )


def recover_signer(message: str, signature: str) -> str:
    """
    Recover the address that produced `signature` over `message`.

    Uses EIP-191 personal_sign, which every wallet, hardware signer, and
    embedded secp256k1 library already implements.
    """
    try:
        return Account.recover_message(encode_defunct(text=message), signature=signature)
    except Exception as exc:
        raise AttestationError(f"Signature could not be decoded: {exc}") from exc


def verify_reading(
    device_public_key: str,
    device_id: str,
    sequence: int,
    timestamp: str,
    delta_kwh: float,
    signature: str,
) -> str:
    """
    Confirm a reading was signed by the device it claims to come from.

    Returns the canonical message on success so it can be stored verbatim for
    independent re-verification. Raises AttestationError otherwise.
    """
    message = canonical_message(device_id, sequence, timestamp, delta_kwh)
    recovered = recover_signer(message, signature)

    if recovered.lower() != device_public_key.lower():
        raise AttestationError(
            "Signature does not match the key registered for this device. "
            f"Signed by {recovered}, expected {device_public_key}."
        )

    return message


def normalise_public_key(value: str) -> str:
    """
    Validate a device's public address and return it in checksummed form.

    Devices are identified by the address derived from their key, the same
    representation used for wallets, so one verification path covers both.
    """
    try:
        return to_checksum_address(value.strip())
    except Exception as exc:
        raise ValueError(
            "Device public key must be a 0x-prefixed 20-byte address "
            "derived from the device's signing key."
        ) from exc


def generate_device_keypair() -> tuple[str, str]:
    """
    Create a keypair for provisioning a device.

    Provided for tests and for operators bootstrapping hardware that cannot
    generate its own key. In production the key should be generated on the
    device and the private half should never be transmitted.
    """
    account = Account.create()
    return account.key.hex(), account.address


def sign_reading(
    private_key: str, device_id: str, sequence: int, timestamp: str, delta_kwh: float
) -> str:
    """
    Produce a reading signature. This is the reference implementation of what
    firmware must do, and is what the test suite's simulated sensor uses.
    """
    message = canonical_message(device_id, sequence, timestamp, delta_kwh)
    signed = Account.sign_message(encode_defunct(text=message), private_key)
    return signed.signature.hex()


def signature_hex(signature: str) -> Optional[str]:
    """Normalise a signature to 0x-prefixed hex."""
    if not signature:
        return None
    return signature if signature.startswith("0x") else f"0x{signature}"
