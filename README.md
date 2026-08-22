# CTN — Carbon Token Network

[![CI](https://github.com/aquiadi/ctn-mvp-complete/actions/workflows/ci.yml/badge.svg)](https://github.com/aquiadi/ctn-mvp-complete/actions/workflows/ci.yml)

Turns metered solar generation into carbon credits you can actually check.
Readings are signed on arrival, accumulated until a full tonne of CO₂ has been
avoided, certified to IPFS, minted on Polygon, traded on a marketplace, and
retired on-chain to complete the offset.

Every figure the site shows traces back to a meter reading and a transaction
hash. Nothing is estimated, and nothing is hardcoded in the frontend.

| | |
|---|---|
| **Live site** | https://ctn-mvp-complete-j52y.vercel.app |
| **API** | https://ctn-api-railway-production.up.railway.app/api |
| **API docs** | https://ctn-api-railway-production.up.railway.app/docs |
| **Contract** | [`0x1b4F…7Cf6`](https://amoy.polygonscan.com/address/0x1b4F5A7CEf1c2CFb914A5642CC82F887AB0C7Cf6) on Polygon Amoy |

> [!NOTE]
> A testnet MVP. Payments are simulated and clearly labelled as such throughout;
> no money moves. Credits are minted on Amoy, not mainnet.

---

## Try it

| Role | Email | Password |
|---|---|---|
| Installer | `demo@installer.ctn` | `demo-installer-2024` |
| Admin | `admin@ctn.org` | set via `ADMIN_PASSWORD` |

Buyers self-register from the sign-up form. Admin accounts cannot be — the role
is rejected at validation, so the only way to create one is with server access.

**As an installer** — the dashboard shows generation, CO₂ avoided, credits, and
their value, with the signed reading history behind them. Connect a sensor with a
pairing code, or upload meter readings as a spreadsheet, then list verified
credits for sale.

**As a buyer** — browse the marketplace, reserve a batch, and complete a
purchase. Reserve and confirm are separate steps, and a reservation expires
after 15 minutes so an abandoned checkout cannot hold a credit hostage.

**As an admin** — confirm device installations, ingest reading CSVs, mint and
retire credits on-chain, and read the audit trail. Every administrative action
is recorded with who did it and why.

**Without an account** — the landing page lists every issued credit. Click one
to verify it: the API resolves its on-chain record and reports whether the
contract and the database agree.

---

## Connecting a sensor

A new seller connects hardware without anyone's help, and generation starts
counting immediately.

1. **Sign up** as an installer. No approval, no waiting.
2. **Get a pairing code** from the dashboard — single use, valid an hour.
3. **Flash the device** with WiFi credentials and that code.
4. **Power it on.** It generates a keypair, enrols itself, and starts reporting.
5. **Credits accrue** as one tonne of avoided CO₂ accumulates.

No sensor yet? The dashboard also offers a spreadsheet upload — see below.

```
     ESP32 ──── WiFi ──── HTTPS ────▶  POST /api/v1/readings
       │                                        │
  signs each reading                   verifies the signature
  with a key that                      against the registered
  never leaves it                      public key
```

The device is the client, over ordinary WiFi. Nothing is wired, no gateway sits
in between, and nothing has to be opened up on the seller's network — it works
behind any home router.

`firmware/ctn_sensor/` is a working ESP32 sketch. Measurement is isolated in one
function, so a pulse meter, a CT clamp, or a Modbus inverter register all drop
in without touching the transport or the cryptography.

> [!NOTE]
> The signing scheme and API contract are covered by the test suite and by
> `tools/sensor_sim.py`. The sketch itself has not been run on physical
> hardware — verify it on a bench before trusting it on a roof.

### Earning versus selling

Data flows the moment a device is paired. **Selling waits for a human.**

A signature proves a reading came from a particular device and was not altered
in transit. It cannot prove the device is measuring a real solar array — a
bench-top ESP32 signs just as convincingly as a rooftop one. So credits from a
self-enrolled device are issued as `pending` and cannot be listed; an operator
confirms the installation and the accrued credits are released. They were always
valid measurements, only their salability was in question.

Set `TRUST_SELF_ENROLLED_DEVICES=true` to skip that gate for a closed pilot
where every device is already known.

---

## Signed sensor data

The platform never has to be trusted about where a reading came from.

A sensor holds a secp256k1 private key that never leaves it and signs every
reading it emits. CTN stores only the derived public address, so it can verify a
reading but cannot forge one. Readings post straight to the API — the signature
is the credential, so there is no session to expire and no shared API key to
extract from firmware.

```
POST /api/v1/readings
{ "readings": [ { "device_id": "ROOF-01", "sequence": 1042,
                  "timestamp": "2026-06-01T06:00:00Z",
                  "delta_kwh": 0.61, "signature": "0x…" } ] }
```

The device signs exactly this text, and the server reconstructs it to recover
the signer:

```
CTN-READING-V1
device:ROOF-01
sequence:1042
timestamp:2026-06-01T06:00:00Z
delta_kwh:0.610000
```

Energy is signed at fixed precision because floats have no single textual form,
and every field that affects the credit is covered — anything left out could be
altered in transit without breaking the signature. `GET /api/v1/spec` serves the
contract from the code that enforces it, so the documentation cannot drift.

**What is rejected.** A signature from any other key. A packet whose energy,
timestamp, device, or sequence was altered after signing. A replayed packet —
sequences are monotonic per device, so a captured reading cannot be resubmitted.
A batch is applied whole or not at all, since a partial apply would leave a gap
indistinguishable from missing generation.

**Verifying without trusting CTN.** `GET /api/v1/readings/{id}/proof` returns the
signed text, the signature, and the device's public key. Recover the EIP-191
signer and compare — a check anyone can run offline, in any language:

```python
from eth_account import Account
from eth_account.messages import encode_defunct

Account.recover_message(
    encode_defunct(text=proof["signed_message"]),
    signature=proof["device_signature"],
) == proof["device_public_key"]
```

Device keys are public at `GET /api/v1/devices/{device_id}` — a key only the
issuer can see would prove nothing to anyone else.

### Trying it

`tools/sensor_sim.py` is a reference client. It implements the signing scheme
from the published spec rather than importing the server's own module, so if it
works, the spec is complete enough to write firmware against.

As a seller pairing new hardware, which needs no admin access:

```bash
python tools/sensor_sim.py code --device ROOF-01 \
    --email demo@installer.ctn --password demo-installer-2024
python tools/sensor_sim.py pair   --device ROOF-01 --code CTN-XXXX-XXXX
python tools/sensor_sim.py credit --device ROOF-01   # enough generation for one credit
```

Or as an operator registering a device directly:

```bash
python tools/sensor_sim.py provision --device ROOF-01   # keypair; only the address is sent
python tools/sensor_sim.py attack    --device ROOF-01   # tampered, forged, replayed → 401, 401, 409
python tools/sensor_sim.py verify    --device ROOF-01   # recover the signer locally
```

Private keys are written to `.sensor-keys/` (gitignored) — the closest local
equivalent to a key that never leaves the device. Add `--api <url>` to point it
at a deployed instance.

### Spreadsheet upload — a deliberate stopgap

> [!IMPORTANT]
> CSV upload exists **for this stage only**. Signed sensor data is the real
> ingestion path; uploading is how the platform stays usable while hardware is
> still being rolled out, and it is expected to fall away as devices arrive.

Not everyone has a sensor yet, so a seller can add a meter from their dashboard
and upload readings as a CSV of `device_id, timestamp, delta_kwh`. Only their own
devices are accepted, so a row naming someone else's meter is refused rather than
crediting the wrong account.

An uploaded number is an assertion by whoever typed it. It carries no device
signature, so it is recorded as **`imported`** rather than attested, shown that
way in the dashboard and the proof endpoint, and its credits are gated exactly
like a self-enrolled device's. The weaker guarantee is visible rather than
quietly equated with a signed reading.

---

## How a credit is made

1. **Onboard.** An installer submits a device with its public key; it stays
   inactive until an administrator approves it. Approval is deliberate — a credit is only as
   trustworthy as the attestation of the device behind it, so self-registration
   would amount to self-issuing credits.
2. **Ingest.** Readings arrive signed from the device, or are imported from a
   CSV. Each is stored under a fingerprint derived from its device and
   timestamp, so re-ingesting the same data changes nothing.
3. **Accumulate.** Unconsumed readings sum per device. At 1,000 kg of avoided
   CO₂ a credit is issued and its contributing readings are marked consumed.
   The remainder carries forward rather than being discarded.
4. **Certify.** A certificate naming those readings is pinned to IPFS.
5. **Mint.** An admin mints the credit to a wallet. The on-chain id, transaction
   hash, and CID are recorded against it.
6. **Trade.** The installer lists it; a buyer reserves and purchases it.
7. **Retire.** An admin retires it on-chain, completing the offset.

`GET /verify/{credit_id}` resolves the stored on-chain id and compares the
contract's recorded quantities against the database, reporting whether the two
ledgers agree — a direct lookup, not a scan.

---

## Running locally

```bash
python3 -m venv .venv && .venv/bin/pip install -r backend/requirements.txt
```

```bash
cd backend && ../.venv/bin/python -m uvicorn main:app --reload --port 8000
```

Open <http://localhost:8000>. The backend serves the frontend, so the pages
target the same origin automatically — no configuration needed.

First start creates the schema, seeds the admin account, and pulls the public
seed dataset from IPFS into credits. There is no build step for the frontend.

### Configuration

Copy `.env.example` and fill in what you need. Every setting has a development
default. Two capabilities degrade rather than fail when unconfigured:

| Missing | Effect |
|---|---|
| `PRIVATE_KEY` | Minting and retirement return `503`; everything else works |
| `PINATA_API_KEY` / `PINATA_SECRET` | Certificates are hashed locally and marked unpinned, never given a fake CID |

---

## Architecture

```
frontend/                     Static pages, no build step
  static/ctn.js               API base resolution, auth, HTTP, formatting
  static/wallet.js            EIP-1193 wallet linking
  index.html                  Landing page and credit verification
  login.html  app.html  app-history.html
  marketplace.html  admin.html  profile.html

backend/
  config.py                   Every tunable value, read from the environment
  main.py                     App wiring, public routes, on-chain operations
  auth.py                     Sessions, password hashing, role dependencies
  chain.py                    Contract access, off the event loop
  database.py                 Schema, migrations, ingestion, credit issuance
  attestation.py              Canonical signed message, signature verification
  data_utils.py               Cumulative meter readings → per-interval deltas
  ipfs_utils.py               Pinata certificate storage
  routes/                     auth · installer · marketplace · admin · ingest
  tests/                      164 tests

firmware/
  ctn_sensor/                 ESP32 sketch: enrol, sign, report over WiFi

tools/
  sensor_sim.py               Reference client: pair, sign, submit, verify

carboncredit-deploy/          Hardhat project for the CarbonCredit contract
```

Constants live in `backend/config.py` and reach the browser through
`GET /config`. No price, threshold, or contract address is written into the
frontend, so changing a value is a config change rather than a code change.

web3.py is synchronous, so every contract call is dispatched to a worker thread.
Calling it directly from a coroutine would stall the event loop for the whole
round trip, and a signed transaction can take tens of seconds.

---

## Accounts

Every role has a profile page at `/profile`.

**Transferring.** Changing the email address moves control of the account and
everything it owns. The current password is required, so a borrowed session
cannot quietly take it over.

**Closing.** Requires the password and a typed confirmation. The row is retained
and anonymised rather than deleted — email and wallet cleared, login disabled —
because credits, transactions, and audit entries reference it, and removing it
would break the trail that makes those credits verifiable. The freed address can
be registered again.

Closure is refused while anything is mid-transaction: an installer with credits
listed or reserved, a buyer holding a reservation, or the last administrator.

---

## Tests

```bash
cd backend && ../.venv/bin/python -m pytest
```

164 tests covering device attestation, self-service onboarding, authentication and role enforcement,
wallet-signature verification, credit issuance and idempotency, marketplace
concurrency, device onboarding and approval, account transfer and closure, CSV
validation, and the audit trail.

The attestation tests drive a simulated sensor — a real keypair and the
reference signing routine — and assert that forged, tampered, and replayed
packets are refused, and that a proof verifies independently of the server. They run against a temporary database and need no network access.

Several are regression tests for specific defects, among them two buyers
concurrently reserving the same credit, daily averages divided by a hardcoded
period instead of the real one, and a request creating a device without review.

### Continuous integration

`.github/workflows/ci.yml` runs on every push and pull request to `master`:
the backend job installs pinned dependencies, lints with pyflakes, and runs the
suite; the frontend job parses every shared module and inline page script, so a
syntax error in a page cannot reach the deployed site.

---

## Deployment

| Tier | Host | Config |
|---|---|---|
| Frontend | Vercel | `frontend/vercel.json` maps clean URLs to files |
| API | Railway | `backend/railway.json`, Nixpacks builder, `/healthz` probe |

Both auto-deploy on push to `master`. The frontend resolves its API base at
runtime, so the same files work locally and in production without a rebuild.

### Required in production

With `ENVIRONMENT=production` these are validated at startup and the process
refuses to boot if any is unsafe, so a misconfiguration fails loudly instead of
silently running insecure.

| Variable | Why |
|---|---|
| `ENVIRONMENT=production` | Turns startup warnings into hard failures |
| `JWT_SECRET` | Signs sessions — `openssl rand -base64 48` |
| `ADMIN_PASSWORD` | Must differ from the documented demo password |
| `COOKIE_SECURE=true` | Sends the session cookie only over HTTPS |
| `CORS_ORIGINS` | Exact frontend origin(s) |
| `DATABASE_URL` | Must point inside a mounted volume — see below |
| `PRIVATE_KEY` | Optional; enables minting and retirement |
| `PINATA_API_KEY` / `PINATA_SECRET` | Optional; enables real IPFS pinning |

### Persistent storage

A container filesystem is ephemeral. Unless the database sits on a mounted
volume, every deploy restarts from an empty file and all accounts, listings, and
purchases are lost.

Mount a volume at `/data` and set `DATABASE_URL=sqlite:////data/ctn.db` — four
slashes, three for the scheme and one for the absolute path. The directory is
created on first boot, and in production the server warns at startup if the
database is not on a volume.

```bash
railway volume update -m /data
railway variable set DATABASE_URL=sqlite:////data/ctn.db
```

This keeps a single instance durable, which is what SQLite supports. Serving
from several instances needs a networked database; the `databases` layer already
speaks Postgres, and the SQLite-specific parts are the raw DDL in
`_apply_schema`, the `PRAGMA table_info` migration check, and `GROUP_CONCAT` in
the marketplace listing query.

### Container

The root `Dockerfile` builds a portable image for Docker-based hosts. It builds
from the repository root because the API also serves the frontend directory.

```bash
docker build -t ctn-api .
docker run -p 8000:8000 -e JWT_SECRET="$(openssl rand -base64 48)" ctn-api
```

It lives at the root rather than in `backend/` deliberately: Railway's service
root is `backend/`, and a Dockerfile there takes precedence over the Nixpacks
builder, after which Railway builds with `backend/` as the context and the
`COPY backend/…` paths no longer resolve.

---

## Security

- Sessions are httpOnly cookies, with a bearer-token fallback for cross-origin
  deployments where browsers block third-party cookies.
- `SameSite=None` requires `Secure`, which plain-HTTP localhost cannot satisfy,
  so cookies default to `SameSite=Lax` outside production.
- CORS is an explicit allow-list. A wildcard-suffix pattern is unusable here:
  the API sends credentials, so any matching host could act as a signed-in user.
- Credential endpoints are rate limited. Login failures return one message for
  both unknown addresses and wrong passwords, so accounts cannot be enumerated.
- Wallet linking verifies an EIP-191 signature over a single-use nonce. The
  private key never leaves the wallet.
- Admin actions are audit-logged before they execute, so failed attempts are
  recorded too.
- Anyone can register as an installer or buyer, by design. New accounts hold
  nothing and can do nothing until an administrator approves a device, so
  automated sign-ups are inert.

---

## Known limitations

Honest about what this is — an MVP with real cryptography and real on-chain
state, but not a production carbon registry.

- **Payments are simulated.** No processor is integrated; the marketplace
  records a transaction and marks credits sold.
- **Single instance.** SQLite on a volume is durable but not horizontally
  scalable.
- **Not yet multi-tenant.** One platform instance serves one operator. Offering
  this as a service others plug into needs organisation isolation, per-tenant
  API scoping, and client libraries.
- **Installation confirmation is manual.** Devices self-enrol, but an operator
  confirms the installation before credits can be sold. A secure element with a
  manufacturer attestation key would let that step be automated rather than
  merely skipped.
- **Testnet only.** Amoy, not Polygon mainnet.
- **The methodology is not accredited.** CO₂ is derived from the CEA grid
  emission factor; `CTN-SOLAR-V1` is this project's own standard, not Gold
  Standard or Verra.

---

## Stack

Python (FastAPI, SQLite, web3.py) · vanilla HTML/CSS/JS with no build step ·
Solidity on Polygon Amoy · IPFS via Pinata.
