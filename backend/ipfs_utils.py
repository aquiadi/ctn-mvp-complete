"""
IPFS certificate storage via Pinata.

Certificates are pinned as files of their exact canonical bytes rather than as
JSON objects. Pinata re-serialises JSON it receives, and a document whose bytes
change on the way in no longer matches the hash recorded at issuance.
"""

import hashlib
import json

import requests

import config


class IPFSUploadError(RuntimeError):
    """Raised when a certificate could not be pinned."""


PINATA_FILE_URL = "https://api.pinata.cloud/pinning/pinFileToIPFS"

_warned_about_credentials = False


def pinning_configured() -> bool:
    return bool(config.PINATA_API_KEY and config.PINATA_SECRET)


def _headers() -> dict:
    return {
        "pinata_api_key": config.PINATA_API_KEY,
        "pinata_secret_api_key": config.PINATA_SECRET,
    }


def _canonical_bytes(document: dict) -> bytes:
    return json.dumps(
        document, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def pin_certificate(document: dict, credit_id) -> str:
    """Pin a certificate's canonical bytes and return the CID. Blocking."""
    if not pinning_configured():
        raise IPFSUploadError("Pinata credentials are not configured.")

    body = _canonical_bytes(document)
    try:
        response = requests.post(
            PINATA_FILE_URL,
            files={"file": (f"ctn-credit-{credit_id}.json", body, "application/json")},
            data={"pinataMetadata": json.dumps({"name": f"ctn-credit-{credit_id}"})},
            headers=_headers(),
            timeout=30,
        )
        response.raise_for_status()
        return response.json()["IpfsHash"]
    except Exception as exc:
        raise IPFSUploadError(f"Failed to pin certificate for credit {credit_id}: {exc}") from exc


def upload_credit_to_ipfs(credit: dict) -> str:
    """
    Pin a certificate and return its CID, or a local content hash when pinning
    is not configured.

    The local form is deterministic and prefixed, so it can never be mistaken
    for a real CID. Used for legacy credits issued before certificates were
    stored; new credits go through pin_certificate with their stored document.
    """
    label = credit.get("credit_id", "new")

    if not pinning_configured():
        # Seeding issues dozens of certificates at once; one notice is enough.
        global _warned_about_credentials
        if not _warned_about_credentials:
            _warned_about_credentials = True
            print("⚠ No Pinata credentials — certificates are hashed locally, not pinned")
        return f"local-{hashlib.sha256(_canonical_bytes(credit)).hexdigest()}"

    return pin_certificate(credit, label)


def is_real_cid(ipfs_hash: str) -> bool:
    """
    Whether a stored hash is an actual IPFS CID.

    Certificates issued without Pinata credentials carry a locally computed
    placeholder instead. Older records used a different placeholder prefix, so
    the check is on CID shape rather than on any one marker.
    """
    if not ipfs_hash:
        return False
    return (
        (ipfs_hash.startswith("Qm") and len(ipfs_hash) == 46)
        or (ipfs_hash.startswith("baf") and len(ipfs_hash) >= 50)
    )


def gateway_url(ipfs_hash: str) -> str | None:
    """Public URL for a pinned certificate, or None if it was never pinned."""
    return f"{config.IPFS_GATEWAY}/{ipfs_hash}" if is_real_cid(ipfs_hash) else None
