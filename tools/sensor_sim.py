#!/usr/bin/env python3
"""
CTN sensor simulator — a reference client for the signed ingestion API.

Deliberately implements the signing scheme from the published specification
rather than importing the server's own module. If this script works, the spec at
GET /api/v1/spec is complete enough for someone to write firmware against, which
is the property that matters for an API other projects plug into.

    python tools/sensor_sim.py provision --device ROOF-01 --capacity-kw 5
    python tools/sensor_sim.py send      --device ROOF-01 --kwh 0.61
    python tools/sensor_sim.py credit    --device PLANT-01   # needs a utility-scale capacity
    python tools/sensor_sim.py attack    --device ROOF-01
    python tools/sensor_sim.py resync    --device ROOF-01
    python tools/sensor_sim.py verify    --device ROOF-01

It speaks CTN-READING-V2, as the firmware does: every reading carries the
device's lifetime meter counter and its enclosure tamper counter, and energy
is tracked in integer watt-hours so the signed text is exact.

Keys are written to .sensor-keys/ so a provisioned device can keep reporting.
That directory is gitignored: a real device's private key never leaves it, and
this file is the closest local equivalent.
"""

import argparse
import json
import math
import pathlib
import sys
import time
from datetime import datetime, timezone

import requests
from eth_account import Account
from eth_account.messages import encode_defunct

DEFAULT_API = "http://127.0.0.1:8000"
KEY_DIR = pathlib.Path(__file__).parent.parent / ".sensor-keys"

MESSAGE_VERSION = "CTN-READING-V2"
KWH_DECIMALS = 6


# ── The signing scheme, as published ───────────────────────────────────────

def canonical_message(device_id, sequence, timestamp, delta_wh, meter_wh, tamper_count):
    """The exact text a device signs. Must match the server byte for byte."""
    return (
        f"{MESSAGE_VERSION}\n"
        f"device:{device_id}\n"
        f"sequence:{sequence}\n"
        f"timestamp:{timestamp}\n"
        f"delta_kwh:{delta_wh / 1000:.{KWH_DECIMALS}f}\n"
        f"meter_wh:{meter_wh}\n"
        f"tamper_count:{tamper_count}"
    )


def sign(private_key, device_id, sequence, timestamp, delta_wh, meter_wh, tamper_count):
    message = canonical_message(device_id, sequence, timestamp, delta_wh, meter_wh, tamper_count)
    signature = Account.sign_message(encode_defunct(text=message), private_key).signature
    return signature.hex() if signature.hex().startswith("0x") else "0x" + signature.hex()


def utc_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def reading(device, device_id, delta_wh, timestamp=None, meter_wh=None, sequence=None,
            sign_delta_wh=None):
    """Build a signed V2 reading. `sign_delta_wh` signs a different value than is sent."""
    sequence = sequence or device["sequence"] + 1
    timestamp = timestamp or utc_now()
    meter_wh = device["meter_wh"] + delta_wh if meter_wh is None else meter_wh
    signed_wh = delta_wh if sign_delta_wh is None else sign_delta_wh
    return {
        "device_id": device_id,
        "sequence": sequence,
        "timestamp": timestamp,
        "delta_kwh": float(f"{delta_wh / 1000:.{KWH_DECIMALS}f}"),
        "message_version": MESSAGE_VERSION,
        "meter_wh": meter_wh,
        "tamper_count": device["tamper_count"],
        "signature": sign(device["private_key"], device_id, sequence, timestamp,
                          signed_wh, meter_wh, device["tamper_count"]),
    }


# ── Local key storage ──────────────────────────────────────────────────────

def key_path(device_id):
    return KEY_DIR / f"{device_id}.json"


def load_device(device_id):
    path = key_path(device_id)
    if not path.exists():
        sys.exit(f"No key for '{device_id}'. Run: sensor_sim.py provision --device {device_id}")
    return json.loads(path.read_text())


def load_device_state(device_id):
    device = load_device(device_id)
    device.setdefault("meter_wh", 0)
    device.setdefault("tamper_count", 0)
    device.setdefault("last_epoch", 0)
    return device


def save_device(device_id, private_key, address, sequence=0, meter_wh=0, tamper_count=0,
                last_epoch=0):
    KEY_DIR.mkdir(exist_ok=True)
    key_path(device_id).write_text(json.dumps(
        {"device_id": device_id, "private_key": private_key, "public_key": address,
         "sequence": sequence, "meter_wh": meter_wh, "tamper_count": tamper_count,
         "last_epoch": last_epoch}, indent=2))


def save_state(device_id, device):
    save_device(device_id, device["private_key"], device["public_key"], device["sequence"],
                device["meter_wh"], device["tamper_count"], device["last_epoch"])


def site(args):
    fields = {"rated_capacity_kw": args.capacity_kw}
    if args.lat is not None and args.lon is not None:
        fields.update(latitude=args.lat, longitude=args.lon)
    return fields


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
              "location": args.location, "public_key": account.address, **site(args)},
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
        json={"label": args.device, "location": args.location, **site(args)},
        timeout=30))


def cmd_send(args):
    """Sign and submit readings, exactly as firmware would."""
    device = load_device_state(args.device)
    delta_wh = round(args.kwh * 1000)
    readings = []
    last_epoch = device["last_epoch"]

    for _ in range(args.count):
        # Real time, strictly after the last reading signed, across runs too:
        # the server refuses anything else.
        last_epoch = max(int(time.time()), last_epoch + 1)
        timestamp = datetime.fromtimestamp(last_epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        packet = reading(device, args.device, delta_wh, timestamp=timestamp)
        readings.append(packet)
        device["sequence"], device["meter_wh"] = packet["sequence"], packet["meter_wh"]

    first = readings[0]
    print(f"Signing {args.count} reading(s). First message:\n")
    print(canonical_message(args.device, first["sequence"], first["timestamp"], delta_wh,
                            first["meter_wh"], first["tamper_count"]))

    response = show("Submitting", requests.post(
        f"{args.api}/api/v1/readings", json={"readings": readings}, timeout=60))

    if response.status_code == 200:
        device["last_epoch"] = last_epoch
        save_state(args.device, device)
    elif response.status_code == 409:
        print("\nThe server already has this sequence. Run `resync` and try again.")


def cmd_credit(args):
    """Send enough signed generation to issue a whole credit, if capacity allows."""
    config = requests.get(f"{args.api}/config", timeout=30).json()
    spec = requests.get(f"{args.api}/api/v1/spec", timeout=30).json()["limits"]
    record = requests.get(f"{args.api}/api/v1/devices/{args.device}", timeout=30).json()

    needed_kwh = config["kg_co2_per_credit"] / config["emission_factor_kg_per_kwh"]
    capacity = record.get("rated_capacity_kw") or spec["default_rated_capacity_kw"]
    # Readings sent back to back are each held to the minimum interval.
    per_reading_cap = (capacity * spec["min_plausibility_interval_seconds"] / 3600
                       * spec["capacity_tolerance"])
    count = max(args.count, math.ceil(needed_kwh / (per_reading_cap * 0.99)))

    if count > 500:
        sys.exit(
            f"One credit needs {needed_kwh:.0f} kWh, but a {capacity:g} kW system may claim at most "
            f"{per_reading_cap:.1f} kWh per reading, so it would take real hours of generation. "
            "Provision a simulated utility-scale plant instead, e.g. --capacity-kw 6000."
        )

    args.kwh = math.ceil(needed_kwh / count * 1000 + 1) / 1000
    args.count = count
    print(f"One credit needs {needed_kwh:.2f} kWh; sending {count} x {args.kwh} kWh "
          f"({capacity:g} kW declared)")
    cmd_send(args)


def cmd_resync(args):
    """Adopt the server's committed sequence and meter counter, as firmware does."""
    device = load_device_state(args.device)
    record = requests.get(f"{args.api}/api/v1/devices/{args.device}", timeout=30).json()
    if (record.get("public_key") or "").lower() != device["public_key"].lower():
        sys.exit("The server holds a different key for this device id.")

    device["sequence"] = record["last_sequence"]
    if record.get("last_meter_wh") is not None:
        device["meter_wh"] = record["last_meter_wh"]
    device["tamper_count"] = max(device["tamper_count"], record.get("tamper_count") or 0)
    save_state(args.device, device)
    print(f"Resynced {args.device}: sequence {device['sequence']}, "
          f"meter {device['meter_wh']} Wh, tamper {device['tamper_count']}")


def cmd_tamper(args):
    """Simulate the enclosure being opened: the next reading carries the new count."""
    device = load_device_state(args.device)
    device["tamper_count"] += 1
    save_state(args.device, device)
    print(f"Tamper count for {args.device} is now {device['tamper_count']}. "
          "The next accepted reading withdraws the installation's confirmation.")


def cmd_attack(args):
    """Demonstrate what the pipeline refuses."""
    device = load_device_state(args.device)

    def post(readings):
        return requests.post(f"{args.api}/api/v1/readings",
                             json={"readings": readings}, timeout=30)

    # 1. Energy altered after signing — the credit-inflation attack.
    tampered = reading(device, args.device, 610, sign_delta_wh=610)
    tampered["delta_kwh"] = 9999.0
    show("1. Inflated energy, signature covers the original value", post([tampered]))

    # 2. Signed by a key the device does not own.
    impostor = dict(device, private_key=Account.create().key.hex())
    show("2. Valid signature from the wrong key", post([reading(impostor, args.device, 610)]))

    # 3. A previously accepted sequence, resubmitted.
    if device["sequence"] > 0:
        replayed = reading(device, args.device, 610, sequence=device["sequence"])
        show("3. Replayed packet at an already-accepted sequence", post([replayed]))

    # 4. Correctly signed, physically impossible: 50 MWh dated 2099.
    show("4. Authentic signature, impossible reading",
         post([reading(device, args.device, 50_000_000, timestamp="2099-01-01T00:00:00Z")]))

    # 5. Correctly signed, but claims more energy than the meter advanced.
    if device["meter_wh"] > 0:
        # Dated just after the last accepted reading so it is judged on energy.
        after = max(time.time(), device["last_epoch"]) + 1
        ahead = datetime.fromtimestamp(after, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        show("5. Energy that the meter counter does not account for",
             post([reading(device, args.device, 1000, timestamp=ahead,
                           meter_wh=device["meter_wh"] + 100)]))

    print("\nExpected: 401, 401, 409, 422 timestamp_future, 422 meter_discontinuity.")


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

    site_args = argparse.ArgumentParser(add_help=False)
    site_args.add_argument("--location", default="Simulated Site")
    site_args.add_argument("--capacity-kw", type=float, default=5.0,
                           help="rated AC capacity; bounds every reading (default 5)")
    site_args.add_argument("--lat", type=float, help="site latitude, for the night check")
    site_args.add_argument("--lon", type=float, help="site longitude")

    p = sub.add_parser("provision", parents=[common, site_args],
                       help="generate a keypair and register the device")
    p.add_argument("--owner", default="demo@installer.ctn")
    p.set_defaults(func=cmd_provision)

    p = sub.add_parser("code", parents=[common, site_args],
                       help="get a pairing code as a seller (no admin needed)")
    p.add_argument("--email", default="demo@installer.ctn")
    p.add_argument("--password", default="demo-installer-2024")
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
    p.set_defaults(func=cmd_attack)

    p = sub.add_parser("resync", parents=[common],
                       help="adopt the server's committed sequence and meter counter")
    p.set_defaults(func=cmd_resync)

    p = sub.add_parser("tamper", parents=[common], help="simulate opening the enclosure")
    p.set_defaults(func=cmd_tamper)

    p = sub.add_parser("verify", parents=[common], help="verify a proof locally")
    p.set_defaults(func=cmd_verify)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
