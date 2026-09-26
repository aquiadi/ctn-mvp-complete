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

# A version string is the first line of every signed message, so a signature
# can only ever be checked against the field set it was produced over. Adding a
# version never invalidates an older one; devices in the field keep working.
MESSAGE_VERSION_V1 = "CTN-READING-V1"
MESSAGE_VERSION_V2 = "CTN-READING-V2"
SUPPORTED_VERSIONS = (MESSAGE_VERSION_V1, MESSAGE_VERSION_V2)

# What new firmware should emit. V2 adds the device's lifetime energy counter,
# which lets the server check each interval against the meter and lets a device
# resynchronise after a lost response without counting energy twice, and the
# enclosure tamper counter, which revokes installation confirmation when the
# box has been opened.
MESSAGE_VERSION = MESSAGE_VERSION_V2

# Fixed precision for the energy field. Floats have no single textual form, so a
# device writing "0.61" and a server reading 0.6100000000000001 would otherwise
# disagree on what was signed.
KWH_DECIMALS = 6


class AttestationError(ValueError):
    """A reading's signature could not be verified against its device."""


def canonical_message(
    device_id: str,
    sequence: int,
    timestamp: str,
    delta_kwh: float,
    version: str = MESSAGE_VERSION_V1,
    meter_wh: Optional[int] = None,
    tamper_count: Optional[int] = None,
) -> str:
    """
    The exact text a device signs.

    Newline-delimited and explicitly labelled so it is readable in firmware logs
    and cannot be reordered. Every field that affects the credit is covered:
    omitting any one of them would let it be altered in transit.

    V2 appends the lifetime meter counter and the tamper counter, both as plain
    unsigned integers so there is exactly one textual form for each.
    """
    message = (
        f"{version}\n"
        f"device:{device_id}\n"
        f"sequence:{sequence}\n"
        f"timestamp:{timestamp}\n"
        f"delta_kwh:{delta_kwh:.{KWH_DECIMALS}f}"
    )

    if version == MESSAGE_VERSION_V1:
        return message
    if version != MESSAGE_VERSION_V2:
        raise AttestationError(f"Unsupported message version '{version}'.")
    if meter_wh is None or tamper_count is None:
        raise AttestationError(f"{MESSAGE_VERSION_V2} requires meter_wh and tamper_count.")
    return f"{message}\nmeter_wh:{int(meter_wh)}\ntamper_count:{int(tamper_count)}"


def _candidate_signatures(signature: str) -> list[bytes]:
    """
    Signature forms to attempt, most likely first.

    A full Ethereum signature is 65 bytes: r, s, and a recovery id. Embedded
    secp256k1 libraries — micro-ecc among them — produce only the 64-byte r||s
    and expose no way to derive the recovery id, so requiring it would mean
    every firmware author reimplementing point recovery to guess a single byte.

    A 65-byte signature is used as given. A 64-byte one is tried under both
    possible recovery ids, which costs one extra elliptic-curve operation and
    removes an entire class of firmware bug.
    """
    raw = bytes.fromhex(signature[2:] if signature.startswith("0x") else signature)

    if len(raw) == 65:
        return [raw]
    if len(raw) == 64:
        return [raw + bytes([27]), raw + bytes([28])]

    raise AttestationError(
        f"Signature must be 64 or 65 bytes, got {len(raw)}."
    )


def recover_signer(message: str, signature: str, expected: str = None) -> str:
    """
    Recover the address that produced `signature` over `message`.

    Uses EIP-191 personal_sign, which every wallet, hardware signer, and
    embedded secp256k1 library already implements. When `expected` is given and
    the signature omits its recovery id, the candidate matching that address is
    returned; otherwise the first that decodes is.
    """
    signable = encode_defunct(text=message)
    candidates = _candidate_signatures(signature)

    recovered_any = None
    for candidate in candidates:
        try:
            recovered = Account.recover_message(signable, signature=candidate)
        except Exception:
            continue

        recovered_any = recovered_any or recovered
        if expected is None or recovered.lower() == expected.lower():
            return recovered

    if recovered_any:
        return recovered_any

    raise AttestationError("Signature could not be decoded.")


def verify_reading(
    device_public_key: str,
    device_id: str,
    sequence: int,
    timestamp: str,
    delta_kwh: float,
    signature: str,
    version: str = MESSAGE_VERSION_V1,
    meter_wh: Optional[int] = None,
    tamper_count: Optional[int] = None,
) -> str:
    """
    Confirm a reading was signed by the device it claims to come from.

    Returns the canonical message on success so it can be stored verbatim for
    independent re-verification. Raises AttestationError otherwise.
    """
    message = canonical_message(
        device_id, sequence, timestamp, delta_kwh, version, meter_wh, tamper_count
    )
    recovered = recover_signer(message, signature, expected=device_public_key)

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
    private_key: str,
    device_id: str,
    sequence: int,
    timestamp: str,
    delta_kwh: float,
    version: str = MESSAGE_VERSION_V1,
    meter_wh: Optional[int] = None,
    tamper_count: Optional[int] = None,
) -> str:
    """
    Produce a reading signature. This is the reference implementation of what
    firmware must do, and is what the test suite's simulated sensor uses.
    """
    message = canonical_message(
        device_id, sequence, timestamp, delta_kwh, version, meter_wh, tamper_count
    )
    signed = Account.sign_message(encode_defunct(text=message), private_key)
    return signed.signature.hex()


def signature_hex(signature: str) -> Optional[str]:
    """Normalise a signature to 0x-prefixed hex."""
    if not signature:
        return None
    return signature if signature.startswith("0x") else f"0x{signature}"
