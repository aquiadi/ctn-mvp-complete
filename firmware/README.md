# CTN sensor firmware

ESP32 firmware that measures AC energy from a solar inverter, signs each
interval with a key that never leaves the device, and reports it to the CTN API
as a `CTN-READING-V2` packet.

## Status

| Part | Verified how |
|---|---|
| Keccak-256, address derivation, EIP-191 signing | Host build in CI: 200 random cases per run. The address, timestamp, and signed message match the server byte for byte, and every signature recovers through the server's verifier and eth_account. |
| Canonical message, timestamp formatting, meter register arithmetic | Same host build |
| PZEM-004T, DS3231, reed switch, WiFi/TLS, NVS | Written against the libraries' documented APIs and syntax-checked on a host with stubs. **Not yet run on a physical board.** Work through the bring-up checklist below before deploying. |

The previous sketch hashed with SHA3-256 instead of Keccak-256. Every address
it enrolled and every signature it produced would have been rejected. It also
never installed an RNG after the first boot, so it stopped signing after a
power cycle. The host test pins both: it fails if either comes back.

## Hardware

| Part | Notes |
|---|---|
| ESP32 DevKit (WROOM-32) | Any ESP32 with WiFi |
| PZEM-004T v3.0, 100 A split-core CT | Measures the inverter's AC output. **Mains wiring: licensed electrician only.** |
| DS3231 RTC module + CR2032 | Keeps UTC through outages; ±2 ppm |
| Normally-closed reed switch + magnet | Mounted so opening the lid moves the magnet away |
| 5 V supply, IP65 enclosure, cable glands | |

| Signal | ESP32 pin | Notes |
|---|---|---|
| PZEM TX → ESP RX2 | GPIO16 | PZEM TTL is 5 V: use a level shifter or the 3.3 V mod |
| PZEM RX ← ESP TX2 | GPIO17 | |
| DS3231 SDA / SCL | GPIO21 / GPIO22 | |
| Reed switch | GPIO27 ↔ GND | Internal pull-up. Lid closed = LOW |

Pins are set in `ctn_sensor/config.h`.

## Build

Libraries (Arduino Library Manager names):

| Library | Version tested against |
|---|---|
| ESP32 Arduino core | 2.0.x / 3.x |
| `micro-ecc` (kmackay) | 1.1 |
| `Crypto` (rweather) | `37a76b8` |
| `ArduinoJson` (bblanchon) | 7.x |
| `PZEM-004T-v30` (mandulaj) | 1.1.x |
| `RTClib` (Adafruit) | 2.x |

```bash
arduino-cli core install esp32:esp32
arduino-cli lib install micro-ecc Crypto ArduinoJson PZEM-004T-v30 RTClib
arduino-cli compile --fqbn esp32:esp32:esp32 firmware/ctn_sensor
arduino-cli upload  --fqbn esp32:esp32:esp32 -p /dev/ttyUSB0 firmware/ctn_sensor
```

Edit `config.h` first: WiFi, API host, device id, and a pairing code from the
dashboard. When you create the pairing code, declare the system's **rated AC
capacity** and **site coordinates**. The server caps every reading at that
capacity, and uses the coordinates to flag generation at night.

## How it behaves

- **First boot**: connects WiFi, syncs NTP into the DS3231, generates a
  secp256k1 keypair with the hardware TRNG (radio on), stores it in NVS, and
  enrols with the pairing code. The PZEM's existing register value becomes the
  baseline, so energy from before enrolment is never claimed.
- **Every 15 minutes**: folds the PZEM register into a 64-bit lifetime
  counter, signs `delta = total − acknowledged` with the counter and the tamper
  count, and posts it.
- **Offline**: nothing is lost or buffered as separate packets. The counter
  keeps running, and the first accepted reading afterwards covers the whole gap.
  Its longer interval also raises its capacity allowance accordingly.
- **Lost response** (409 replay, or 422 `meter_discontinuity`): reads
  `last_sequence` and `last_meter_wh` from `GET /api/v1/devices/{id}` and
  continues from there. Committed energy is never claimed twice.
- **Lid opened**: the tamper counter increments and persists immediately. The
  next reading carries it, and the server withdraws the installation's
  confirmation until an operator re-checks the site.
- **Unrecoverable** (401, counter regressed, key mismatch, pairing code
  rejected): stops reporting and logs why on serial. These need a person, and
  retrying would not help.
- **No progress for 6 hours**: reboots, to recover a wedged network stack.

## Bench bring-up checklist

Do this with the PZEM on a known resistive load (a 1 kW heater) before any
roof install:

1. Serial shows `[clock] RTC set from NTP` and a plausible UTC time.
2. `[key] device address 0x…` appears, and the same address is shown after a
   reboot (the key persisted).
3. Enrolment returns HTTP 200, and the device appears in the seller dashboard.
4. After one interval, `[report] … HTTP 200`. `GET /api/v1/readings/{id}/proof`
   returns `signature_valid: true`.
5. Heater for 1 h ≈ 1.0 kWh across the readings (PZEM ±0.5 %).
6. Pull the network for 45 minutes, then restore it. One reading covers the
   gap, and the energy total is unchanged.
7. Open the lid. `[tamper] … count now 1`, and the dashboard shows the device
   as unconfirmed.
8. Power-cycle mid-interval. Sequence and counters continue without a 409 loop.
9. Remove `CTN_ALLOW_INSECURE_TLS` if you set it, and confirm reporting still
   works against the production host's certificate.

## Securing the key

The private key sits in NVS, which is **not encrypted by default**. Anyone
holding the board can read it out over USB and sign readings as this device.
Before deployment:

1. Enable **flash encryption** (release mode) and **secure boot v2** with
   `espefuse.py` / `idf.py`, following Espressif's production guides for your
   chip. Both are one-way eFuse operations: test on a spare board first.
2. Or better, move the key into a secure element (ATECC608B) so it is generated
   and used inside the chip and cannot be extracted at all. That also allows a
   manufacturer attestation certificate, which is what would let the server
   confirm installations automatically instead of by a person.

Physical tamper evidence (sealed enclosure, tamper-evident labels on the CT
and terminals) is still required. The reed switch detects the lid, not a
drilled box or a bypassed clamp.

## Host test

```bash
firmware/test/run_host_test.sh   # needs a C++ compiler, git, and backend/requirements-dev.txt
```

This fetches micro-ecc and rweather Crypto at the pinned revisions and builds
`ctn_crypto.cpp` and `ctn_protocol.cpp` exactly as the sketch uses them. It
then checks them against `backend/attestation.py`. It runs in CI on every push.
