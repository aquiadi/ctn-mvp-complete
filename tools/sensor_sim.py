#!/usr/bin/env python3
"""
CTN sensor simulator — a reference client for the signed ingestion API.

Deliberately implements the signing scheme from the published specification
rather than importing the server's own module. If this script works, the spec at
GET /api/v1/spec is complete enough for someone to write firmware against, which
is the property that matters for an API other projects plug into.

    python tools/sensor_sim.py provision --device ROOF-01 --owner demo@installer.ctn
    python tools/sensor_sim.py send      --device ROOF-01 --kwh 0.61 --count 3
    python tools/sensor_sim.py credit    --device ROOF-01
    python tools/sensor_sim.py attack    --device ROOF-01
    python tools/sensor_sim.py verify    --device ROOF-01

Keys are written to .sensor-keys/ so a provisioned device can keep reporting.
That directory is gitignored: a real device's private key never leaves it, and
this file is the closest local equivalent.
"""

import argparse
import json
import pathlib
import sys

import requests
from eth_account import Account
from eth_account.messages import encode_defunct

DEFAULT_API = "http://127.0.0.1:8000"
KEY_DIR = pathlib.Path(__file__).parent.parent / ".sensor-keys"

MESSAGE_VERSION = "CTN-READING-V1"
KWH_DECIMALS = 6


# ── The signing scheme, as published ───────────────────────────────────────

def canonical_message(device_id, sequence, timestamp, delta_kwh):
    """The exact text a device signs. Must match the server byte for byte."""
    return (
        f"{MESSAGE_VERSION}\n"
        f"device:{device_id}\n"
        f"sequence:{sequence}\n"
        f"timestamp:{timestamp}\n"
        f"delta_kwh:{delta_kwh:.{KWH_DECIMALS}f}"
    )


def sign(private_key, device_id, sequence, timestamp, delta_kwh):
    message = canonical_message(device_id, sequence, timestamp, delta_kwh)
    signature = Account.sign_message(encode_defunct(text=message), private_key).signature
    return signature.hex() if signature.hex().startswith("0x") else "0x" + signature.hex()


# ── Local key storage ──────────────────────────────────────────────────────

def key_path(device_id):
    return KEY_DIR / f"{device_id}.json"


def load_device(device_id):
    path = key_path(device_id)
    if not path.exists():
        sys.exit(f"No key for '{device_id}'. Run: sensor_sim.py provision --device {device_id}")
    return json.loads(path.read_text())


def save_device(device_id, private_key, address, sequence=0):
    KEY_DIR.mkdir(exist_ok=True)
    key_path(device_id).write_text(json.dumps(
        {"device_id": device_id, "private_key": private_key,
         "public_key": address, "sequence": sequence}, indent=2))


# ── HTTP helpers ───────────────────────────────────────────────────────────

def admin_token(api, password):
    response = requests.post(
        f"{api}/api/auth/login",
        json={"email": "admin@ctn.org", "password": password}, timeout=30)
    if response.status_code != 200:
        sys.exit(f"Admin login failed ({response.status_code}). Check --admin-password.")
    return response.json()["token"]


def show(label, response):
    try:
        body = json.dumps(response.json(), indent=2)
    except ValueError:
        body = response.text
    print(f"\n{label}  [HTTP {response.status_code}]\n{body}")
    return response


# ── Commands ───────────────────────────────────────────────────────────────

def cmd_provision(args):
    """Generate a keypair and register the device. Only the address is sent."""
    account = Account.create()
    save_device(args.device, account.key.hex(), account.address)

    print(f"Generated keypair for {args.device}")
    print(f"  public address : {account.address}   (sent to CTN)")
    print(f"  private key    : stored in {key_path(args.device)}   (never sent)")

    token = admin_token(args.api, args.admin_password)
    show("Registering device", requests.post(
        f"{args.api}/api/admin/devices",
        headers={"Authorization": f"Bearer {token}"},
        json={"device_id": args.device, "owner_email": args.owner,
              "location": args.location, "public_key": account.address},
        timeout=30))


def cmd_pair(args):
    """
    The newcomer path: redeem a pairing code, exactly as an ESP32 would.

    Unlike `provision`, this needs no admin access — the seller's own code is
    what authorises the device.
    """
    account = Account.create()
    save_device(args.device, account.key.hex(), account.address)

    print(f"Generated keypair on the 'device' for {args.device}")
    print(f"  address     : {account.address}   (sent)")
    print(f"  private key : {key_path(args.device)}   (never sent)")

    show("Enrolling with pairing code", requests.post(
        f"{args.api}/api/v1/devices/enroll",
        json={"enrollment_code": args.code, "device_id": args.device,
              "public_key": account.address},
        timeout=30))


def cmd_code(args):
    """Ask for a pairing code as the seller would from their dashboard."""
    response = requests.post(
        f"{args.api}/api/auth/login",
        json={"email": args.email, "password": args.password}, timeout=30)
    if response.status_code != 200:
        sys.exit(f"Login failed ({response.status_code}) for {args.email}")

    token = response.json()["token"]
    show("Pairing code", requests.post(
        f"{args.api}/api/installer/enrollment-codes",
        headers={"Authorization": f"Bearer {token}"},
        json={"label": args.device, "location": args.location},
        timeout=30))


def cmd_send(args):
    """Sign and submit readings, exactly as firmware would."""
    device = load_device(args.device)
    readings = []

    for _ in range(args.count):
        device["sequence"] += 1
        seq = device["sequence"]
        timestamp = f"2026-07-{(seq % 28) + 1:02d}T{seq % 24:02d}:00:00Z"
        readings.append({
            "device_id": args.device,
            "sequence": seq,
            "timestamp": timestamp,
            "delta_kwh": args.kwh,
            "signature": sign(device["private_key"], args.device, seq, timestamp, args.kwh),
        })

    print(f"Signing {args.count} reading(s). First message:\n")
    print(canonical_message(args.device, readings[0]["sequence"],
                            readings[0]["timestamp"], args.kwh))

    response = show("Submitting", requests.post(
        f"{args.api}/api/v1/readings", json={"readings": readings}, timeout=60))

    if response.status_code == 200:
        save_device(args.device, device["private_key"], device["public_key"], device["sequence"])


def cmd_credit(args):
    """Send enough signed generation to issue a whole credit."""
    config = requests.get(f"{args.api}/config", timeout=30).json()
    needed = config["kg_co2_per_credit"] / config["emission_factor_kg_per_kwh"]
    per_reading = round(needed / args.count + 0.01, 6)

    print(f"One credit needs {needed:.2f} kWh; sending {args.count} x {per_reading} kWh")
    args.kwh = per_reading
    cmd_send(args)


def cmd_attack(args):
    """Demonstrate what the pipeline refuses."""
    device = load_device(args.device)
    base_seq = device["sequence"] + 1
    timestamp = "2026-09-01T06:00:00Z"

    def post(readings):
        return requests.post(f"{args.api}/api/v1/readings",
                             json={"readings": readings}, timeout=30)

    # 1. Energy altered after signing — the credit-inflation attack.
    tampered = {
        "device_id": args.device, "sequence": base_seq, "timestamp": timestamp,
        "delta_kwh": 9999.0,
        "signature": sign(device["private_key"], args.device, base_seq, timestamp, 0.61),
    }
    show("1. Inflated energy, signature covers the original value", post([tampered]))

    # 2. Signed by a key the device does not own.
    impostor = Account.create()
    forged = {
        "device_id": args.device, "sequence": base_seq, "timestamp": timestamp,
        "delta_kwh": 0.61,
        "signature": sign(impostor.key.hex(), args.device, base_seq, timestamp, 0.61),
    }
    show("2. Valid signature from the wrong key", post([forged]))

    # 3. A previously accepted packet, resubmitted.
    if device["sequence"] > 0:
        seq = device["sequence"]
        replay_ts = f"2026-07-{(seq % 28) + 1:02d}T{seq % 24:02d}:00:00Z"
        replayed = {
            "device_id": args.device, "sequence": seq, "timestamp": replay_ts,
            "delta_kwh": args.kwh,
            "signature": sign(device["private_key"], args.device, seq, replay_ts, args.kwh),
        }
        show("3. Replayed packet at an already-accepted sequence", post([replayed]))

    print("\nAll three should be refused: 401, 401, 409.")


def cmd_verify(args):
    """Fetch a proof and verify it locally, trusting nothing the server says."""
    device_record = requests.get(
        f"{args.api}/api/v1/devices/{args.device}", timeout=30).json()
    print(f"Device {args.device}: {device_record['readings_attested']} attested "
          f"of {device_record['readings_total']} readings")
    print(f"Registered key: {device_record['public_key']}")

    token = admin_token(args.api, args.admin_password)
    credits = requests.get(f"{args.api}/api/admin/credits?limit=200",
                           headers={"Authorization": f"Bearer {token}"}, timeout=30).json()
    reading_id = None
    for credit in credits["credits"]:
        if credit["device_id"] == args.device and credit.get("contributing_readings"):
            reading_id = json.loads(credit["contributing_readings"])[0]["reading_id"]
            break

    if not reading_id:
        sys.exit("No attested reading found yet. Run `send` first.")

    proof = requests.get(f"{args.api}/api/v1/readings/{reading_id}/proof", timeout=30).json()
    print(f"\nProof for reading {reading_id}:")
    print(proof["signed_message"])

    recovered = Account.recover_message(
        encode_defunct(text=proof["signed_message"]),
        signature=proof["device_signature"])

    print(f"\n  recovered signer : {recovered}")
    print(f"  device key       : {proof['device_public_key']}")
    match = recovered.lower() == proof["device_public_key"].lower()
    print(f"  VERIFIED         : {match}")
    print(f"  credit           : {proof['credit']}")
    if not match:
        sys.exit(1)


# ── Entry point ────────────────────────────────────────────────────────────

def main():
    # Shared options are attached to every subcommand as well as the top level,
    # so `send --device X` works as naturally as `--device X send`.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--api", default=DEFAULT_API, help=f"API base (default {DEFAULT_API})")
    common.add_argument("--admin-password", default="ctn-admin-2024")
    common.add_argument("--device", default="SIM-01")

    parser = argparse.ArgumentParser(
        description=__doc__, parents=[common],
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("provision", parents=[common],
                       help="generate a keypair and register the device")
    p.add_argument("--owner", default="demo@installer.ctn")
    p.add_argument("--location", default="Simulated Site")
    p.set_defaults(func=cmd_provision)

    p = sub.add_parser("code", parents=[common],
                       help="get a pairing code as a seller (no admin needed)")
    p.add_argument("--email", default="demo@installer.ctn")
    p.add_argument("--password", default="demo-installer-2024")
    p.add_argument("--location", default="Simulated Site")
    p.set_defaults(func=cmd_code)

    p = sub.add_parser("pair", parents=[common],
                       help="enrol using a pairing code, as an ESP32 would")
    p.add_argument("--code", required=True, help="pairing code from `code`")
    p.set_defaults(func=cmd_pair)

    p = sub.add_parser("send", parents=[common], help="sign and submit readings")
    p.add_argument("--kwh", type=float, default=0.61)
    p.add_argument("--count", type=int, default=1)
    p.set_defaults(func=cmd_send)

    p = sub.add_parser("credit", parents=[common], help="send enough generation to issue one credit")
    p.add_argument("--count", type=int, default=3)
    p.set_defaults(func=cmd_credit)

    p = sub.add_parser("attack", parents=[common], help="show what the pipeline refuses")
    p.add_argument("--kwh", type=float, default=0.61)
    p.set_defaults(func=cmd_attack)

    p = sub.add_parser("verify", parents=[common], help="verify a proof locally")
    p.set_defaults(func=cmd_verify)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
