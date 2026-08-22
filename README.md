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
their value, with the signed reading history behind them. Submit a device for
approval, or list verified credits for sale.

**As a buyer** — browse the marketplace, reserve a batch, and complete a
purchase. Reserve and confirm are separate steps, and a reservation expires
after 15 minutes so an abandoned checkout cannot hold a credit hostage.

**As an admin** — approve device submissions, ingest reading CSVs, mint and
retire credits on-chain, and read the audit trail. Every administrative action
is recorded with who did it and why.

**Without an account** — the landing page lists every issued credit. Click one
to verify it: the API resolves its on-chain record and reports whether the
contract and the database agree.

---

## How a credit is made

1. **Onboard.** An installer submits a device; it stays inactive until an
   administrator approves it. Approval is deliberate — a credit is only as
   trustworthy as the attestation of the device behind it, so self-registration
   would amount to self-issuing credits.
2. **Ingest.** Readings arrive from the seed dataset or an admin CSV upload.
   Each is hashed into a signature and stored under a fingerprint derived from
   its device and timestamp, so re-ingesting the same data changes nothing.
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
  data_utils.py               Cumulative meter readings → per-interval deltas
  ipfs_utils.py               Pinata certificate storage
  routes/                     auth · installer · marketplace · admin
  tests/                      113 tests

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

113 tests covering authentication and role enforcement, wallet-signature
verification, credit issuance and idempotency, marketplace concurrency, device
onboarding and approval, account transfer and closure, CSV validation, and the
audit trail. They run against a temporary database and need no network access.

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
- **Device data is admin-mediated.** Installers submit devices for approval and
  readings arrive by CSV. Signed inverter or meter feeds, with no human upload,
  are the next step.
- **Testnet only.** Amoy, not Polygon mainnet.
- **The methodology is not accredited.** CO₂ is derived from the CEA grid
  emission factor; `CTN-SOLAR-V1` is this project's own standard, not Gold
  Standard or Verra.

---

## Stack

Python (FastAPI, SQLite, web3.py) · vanilla HTML/CSS/JS with no build step ·
Solidity on Polygon Amoy · IPFS via Pinata.
