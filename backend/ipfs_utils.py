"""
IPFS certificate storage via Pinata.
"""

import json

import requests

import config


class IPFSUploadError(RuntimeError):
    """Raised when a certificate could not be pinned."""


_warned_about_credentials = False


def upload_credit_to_ipfs(credit: dict) -> str:
    """
    Pin a credit certificate to IPFS and return its CID.

    Without Pinata credentials — the normal case in local development — the
    content is hashed locally instead. The result is deterministic and clearly
    marked, so it can never be mistaken for a real CID.
    """
    label = credit.get("credit_id", "new")

    if not config.PINATA_API_KEY or not config.PINATA_SECRET:
        # Seeding issues dozens of certificates at once; one notice is enough.
        global _warned_about_credentials
        if not _warned_about_credentials:
            _warned_about_credentials = True
            print("⚠ No Pinata credentials — certificates are hashed locally, not pinned")
        return f"local-{_local_digest(credit)}"

    try:
        response = requests.post(
            config.PINATA_PIN_URL,
            json={
                "pinataContent": credit,
                "pinataMetadata": {"name": f"credit_{label}"},
            },
            headers={
                "pinata_api_key": config.PINATA_API_KEY,
                "pinata_secret_api_key": config.PINATA_SECRET,
            },
            timeout=30,
        )
        response.raise_for_status()
        return response.json()["IpfsHash"]
    except Exception as exc:
        raise IPFSUploadError(f"Failed to pin certificate for credit {label}: {exc}") from exc


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


def _local_digest(credit: dict) -> str:
    import hashlib

    return hashlib.sha256(json.dumps(credit, sort_keys=True).encode("utf-8")).hexdigest()[:32]
