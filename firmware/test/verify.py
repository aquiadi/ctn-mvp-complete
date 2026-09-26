"""
Check the firmware's portable modules against the server, byte for byte.

Generates random keys and readings, runs them through the host build of the
firmware code, and asserts that for every case:

  - the address the device would enrol is the one eth_account derives
  - its timestamp is the server's accepted format for the same instant
  - its signed text is exactly the server's canonical_message()
  - its signature recovers to its address through the server's verifier,
    is low-s, and is deterministic: the same input signs identically

Usage: verify.py <path to host_test binary>
"""

import json
import random
import secrets
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))

from eth_account import Account  # noqa: E402
from eth_account.messages import encode_defunct  # noqa: E402

import attestation  # noqa: E402

KECCAK_EMPTY = "0xc5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470"
KECCAK_ABC = "0x4e03657aea45a94fc7d47ba826c8d667c0d1e6e33a64a036ec44f58fa12d6c45"
# What the previous firmware computed for "": FIPS 202 SHA3-256, not Keccak.
SHA3_EMPTY = "0xa7ffc6f8bf1ed76651c14756a061d662f580ff4de43b49fa82d80a4b80f8434a"


def cases(count: int):
    rng = random.Random(20260926)
    edge_epochs = [0, 951782400, 1709164800, 1767225599, 4102444800]  # incl. leap days, year ends
    for i in range(count):
        key = "0x" + secrets.token_hex(32)
        epoch = edge_epochs[i] if i < len(edge_epochs) else rng.randint(1_600_000_000, 2_200_000_000)
        delta = rng.choice([0, 1, 999, 1000, 1001, rng.randint(0, 5_000_000)])
        meter = delta + rng.randint(0, 2**40)
        yield key, f"ROOF-{i:03d}", rng.randint(1, 2**32 - 1), epoch, delta, meter, rng.randint(0, 50)


def main(binary: str) -> int:
    inputs = list(cases(200))
    stdin = "".join(" ".join(map(str, c)) + "\n" for c in inputs)
    result = subprocess.run([binary], input=stdin, capture_output=True, text=True, check=True)
    lines = [json.loads(line) for line in result.stdout.splitlines()]

    header, outputs = lines[:5], lines[5:]
    assert header[0]["keccak256_empty"] == KECCAK_EMPTY, header[0]
    assert header[0]["keccak256_empty"] != SHA3_EMPTY
    assert header[1]["keccak256_abc"] == KECCAK_ABC, header[1]
    assert header[2] == {"advance": [1000, 1250], "added": 250, "reset": False, "baseline": 1250}
    assert header[3] == {"advance": [9999990, 9999989], "added": 0, "reset": False,
                         "baseline": 9999990}, "one-Wh float jitter must not read as a reset"
    assert header[4] == {"advance": [9999990, 40], "added": 40, "reset": True, "baseline": 40}
    assert len(outputs) == len(inputs)

    half_order = 0x7FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF5D576E7357A4501DDFE92F46681B20A0
    for (key, device, sequence, epoch, delta_wh, meter_wh, tamper), out in zip(inputs, outputs):
        account = Account.from_key(key)
        assert out["address"] == account.address.lower(), (device, out["address"])

        timestamp = datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        assert out["timestamp"] == timestamp, (epoch, out["timestamp"])

        expected = attestation.canonical_message(
            device, sequence, timestamp, delta_wh / 1000, attestation.MESSAGE_VERSION_V2,
            meter_wh=meter_wh, tamper_count=tamper,
        )
        assert out["message"] == expected, (out["message"], expected)

        assert attestation.verify_reading(
            out["address"], device, sequence, timestamp, delta_wh / 1000, out["signature"],
            attestation.MESSAGE_VERSION_V2, meter_wh, tamper,
        ) == expected

        s_value = int(out["signature"][66:130], 16)
        assert 0 < s_value <= half_order, f"high-s signature for {device}"

    # Determinism: signing the same inputs again gives the same bytes.
    again = subprocess.run([binary], input=stdin, capture_output=True, text=True, check=True)
    assert again.stdout == result.stdout, "signatures are not deterministic"

    # And the signature is a real EIP-191 signature, not merely one the
    # server's own code accepts: recover it with eth_account directly.
    key, device, sequence, epoch, delta_wh, meter_wh, tamper = inputs[-1]
    out = outputs[-1]
    recovered = Account.recover_message(
        encode_defunct(text=out["message"]),
        vrs=(27, int(out["signature"][2:66], 16), int(out["signature"][66:], 16)),
    )
    alternate = Account.recover_message(
        encode_defunct(text=out["message"]),
        vrs=(28, int(out["signature"][2:66], 16), int(out["signature"][66:], 16)),
    )
    assert out["address"] in (recovered.lower(), alternate.lower())

    print(f"ok: {len(inputs)} cases; addresses, timestamps, and messages match the server "
          "byte for byte, and every signature verifies, is low-s, and is deterministic")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
