# CTN — Carbon Token Network

[![CI](https://github.com/aquiadi/ctn-mvp-complete/actions/workflows/ci.yml/badge.svg)](https://github.com/aquiadi/ctn-mvp-complete/actions/workflows/ci.yml)

A platform for turning metered solar generation into verifiable carbon credits:
readings are ingested and signed, accumulated into one-tonne credits, pinned to
IPFS, minted on Polygon (Amoy testnet), traded on a marketplace, and finally
retired on-chain to complete an offset.

**Live:** frontend on [Vercel](https://ctn-mvp-complete-j52y.vercel.app) ·
API on [Railway](https://ctn-api-railway-production.up.railway.app/api).

---

## Architecture

```
frontend/                     Static pages, no build step
  static/ctn.js               API base resolution, auth, HTTP, formatting
  static/wallet.js            EIP-1193 wallet linking
  index.html                  Public landing page and credit verification
  login.html  app.html  app-history.html  marketplace.html  admin.html

backend/
  config.py                   Every tunable value, read from the environment
  main.py                     App wiring, public read routes, on-chain endpoints
  auth.py                     Sessions, password hashing, role dependencies
  chain.py                    Contract access (off the event loop)
  database.py                 Schema, migrations, ingestion, credit issuance
  data_utils.py               Cumulative meter readings → per-interval deltas
  ipfs_utils.py               Pinata certificate storage
  rate_limit.py               Shared limiter
  routes/                     auth · installer · marketplace · admin
  tests/                      pytest suite

carboncredit-deploy/          Hardhat project for the CarbonCredit contract
```

Constants live in `backend/config.py` and are published to the browser through
`GET /config`. No price, threshold, or address is written into the frontend.

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

On first start the schema is created, the admin account is seeded, and the
public seed dataset is pulled from IPFS and aggregated into credits.

### Configuration

Copy `.env.example` and fill in what you need. Every setting has a development
default; the security-relevant ones are enforced when `ENVIRONMENT=production`,
and the process refuses to start if they are missing.

Two capabilities degrade gracefully when unconfigured rather than failing at
runtime:

| Missing | Effect |
|---|---|
| `PRIVATE_KEY` | Minting and retirement return `503`. Everything else works. |
| `PINATA_API_KEY` / `PINATA_SECRET` | Certificates are hashed locally and marked unpinned, never given a fake CID. |

---

## Demo credentials

Development defaults, overridable via `ADMIN_PASSWORD` and
`DEMO_INSTALLER_PASSWORD`. Change them before deploying — the server warns at
startup while they are in place and refuses to start in production.

| Role | Email | Password |
|---|---|---|
| Admin | `admin@ctn.org` | `ctn-admin-2024` |
| Installer | `demo@installer.ctn` | `demo-installer-2024` |

Buyer accounts are self-registered from the sign-up form. Admin accounts cannot
be — the role is rejected at validation.

---

## How a credit is made

0. **Onboard.** An installer submits a device from their dashboard; it stays
   inactive until an administrator approves it. Approval is deliberate rather
   than automatic — a credit is only as trustworthy as the attestation of the
   device behind it, so self-registration would amount to self-issuing credits.
   Both approvals and rejections are audit-logged, and a rejection carries a
   reason the installer can see.
1. **Ingest.** Readings arrive from the seed dataset or an admin CSV upload.
   Each is hashed into a signature and stored under a fingerprint derived from
   its device and timestamp, so re-ingesting the same data is a no-op.
2. **Accumulate.** Unconsumed readings are summed per device. Once 1,000 kg of
   avoided CO₂ has accrued, a discrete credit is issued and its contributing
   readings are marked consumed. Any remainder carries forward.
3. **Certify.** A certificate naming the contributing readings is pinned to
   IPFS at issuance.
4. **Mint.** An admin mints the credit to a wallet. The on-chain id, transaction
   hash, and CID are recorded against the credit.
5. **Trade.** The installer lists it; a buyer reserves and purchases it.
   Reservations expire after 15 minutes and are swept back to the marketplace.
6. **Retire.** An admin retires it on-chain, completing the offset.

Verification (`GET /verify/{credit_id}`) resolves the stored on-chain id and
compares the contract's recorded quantities against the database, reporting
whether the two ledgers agree.

---

## Reviewer walkthrough

**Installer.** Sign in as the demo installer. `/app` shows generation totals and
signed readings; `/app/history` filters credits by lifecycle status. Listing
credits requires a linked wallet — the demo account has one so the flow can be
exercised; a real installer links their own by signing a challenge with
MetaMask, and the private key never leaves the wallet.

**Marketplace.** Sign up as a buyer, browse the listings, and complete a
purchase. Reserve and confirm are separate steps; payment is simulated and
labelled as such throughout. Installers can browse but the buy action is
disabled for them.

**Admin.** Sign in as the admin. *Credits* is the ledger, with mint and retire
actions and links to Polygonscan. *Devices & data* holds the pending device
queue, the registered-device list, and ingests reading CSVs (`device_id, timestamp, delta_kwh`) — an invalid row rejects the
whole file rather than importing part of it. *Transactions*, *Audit log*, and
*Health* show marketplace activity, every admin action with its stated reason,
and RPC/contract/wallet status.

Mint and retire need `PRIVATE_KEY` set. Without it the admin panel says so up
front instead of failing at the point of use.

---

## Tests

```bash
cd backend && ../.venv/bin/python -m pytest
```

96 tests covering authentication and role enforcement, wallet-signature
verification, credit issuance and idempotency, marketplace concurrency, device
onboarding and approval, CSV validation, and the audit trail. They run against a temporary database and need
no network access.

Several are regression tests for specific defects, including two buyers
concurrently reserving the same credit, and daily averages computed against a
hardcoded period rather than the real data.

---

## Continuous integration

`.github/workflows/ci.yml` runs on every push and pull request to `master`:

- **Backend** — installs pinned dependencies, lints with pyflakes, runs the full
  pytest suite.
- **Frontend** — parses every shared module and inline page script, so a syntax
  error in a page can't reach the deployed site.

---

## Deployment

The two tiers deploy independently and are wired together by CORS and the
frontend's API-base resolution:

| Tier | Host | Serves |
|---|---|---|
| Frontend | Vercel | The static pages (`vercel.json` maps clean URLs to files) |
| API | Railway | The FastAPI backend (`backend/railway.json`) |

Both auto-deploy on push to `master`. The frontend calls the Railway API and
falls back to a bearer token when the cross-origin session cookie is blocked, so
no per-environment frontend build is required.

### Persistent storage

The API stores data in SQLite. A container filesystem is ephemeral, so unless
the database sits on a mounted volume every deploy restarts from an empty file
and all accounts, listings, and purchases are lost.

On Railway: open the service, **Variables → + New Volume**, mount it at `/data`,
then set `DATABASE_URL=sqlite:////data/ctn.db` (four slashes — three for the
scheme, one for the absolute path). The directory is created on first boot, and
in production the server warns at startup if the database is not on a volume.

This keeps a single instance durable, which is what SQLite supports. Serving
from more than one instance needs a networked database such as Postgres; the
`databases` layer already speaks it, and the SQLite-specific parts are the raw
DDL in `_apply_schema`, the `PRAGMA table_info` migration check, and
`GROUP_CONCAT` in the marketplace listing query.

**Required production environment variables** (set on the API host). With
`ENVIRONMENT=production` the process validates these at startup and refuses to
boot if any is unsafe, so a misconfiguration fails loudly rather than silently:

| Variable | Why |
|---|---|
| `ENVIRONMENT=production` | Turns the startup warnings into hard failures |
| `JWT_SECRET` | Signs sessions. Generate with `openssl rand -base64 48` |
| `ADMIN_PASSWORD` | Must differ from the documented demo password |
| `COOKIE_SECURE=true` | Sends the session cookie only over HTTPS |
| `CORS_ORIGINS` | Exact frontend origin(s), e.g. the Vercel URL |
| `DATABASE_URL` | Path inside the mounted volume, or data is lost on redeploy |
| `PRIVATE_KEY` | Optional — enables minting and retirement |
| `PINATA_API_KEY` / `PINATA_SECRET` | Optional — enables real IPFS pinning |

### Container

The root `Dockerfile` produces a portable image for Docker-based hosts (Render,
Fly.io, Cloud Run) or local runs. It builds from the repository root, since the
API also serves the frontend directory.

```bash
docker build -t ctn-api .
docker run -p 8000:8000 -e JWT_SECRET="$(openssl rand -base64 48)" ctn-api
```

It lives at the root rather than in `backend/` on purpose. Railway's service
root is `backend/`, and a Dockerfile there takes precedence over the Nixpacks
builder — Railway would then build with `backend/` as the context and the
`COPY backend/...` paths would not resolve.

---

## Security notes

- Sessions are httpOnly cookies; a bearer-token fallback covers cross-origin
  deployments where third-party cookies are blocked.
- `SameSite=None` requires `Secure`, which a plain-HTTP localhost cannot
  satisfy, so cookies default to `SameSite=Lax` outside production.
- CORS is an explicit allow-list. A wildcard-suffix pattern is not usable here:
  the API sends credentials, so any matching host could act as a signed-in user.
- Credential endpoints are rate limited. Login failures return one message for
  both unknown addresses and wrong passwords, so accounts cannot be enumerated.
- Admin actions are audited before they execute, so failed attempts are recorded
  too.
- With `ENVIRONMENT=production` the configuration is validated at startup and an
  unsafe setting aborts the boot, so the service cannot come up with a known
  signing key or a demo password still in place.

> [!CAUTION]
> **Exposed secrets in history.** Earlier commits (still reachable in this
> public repository) contain `backend/cookie.txt` — a session token signed with
> the old default `JWT_SECRET`, which is also in history — plus SQLite databases
> holding bcrypt password hashes. Until the API sets its own `JWT_SECRET`, that
> default key can be used to forge admin sessions against the live backend.
>
> Required: set a fresh `JWT_SECRET` and `ADMIN_PASSWORD` on the API host (this
> both closes the hole and invalidates the leaked token). Recommended: rewrite
> history to purge the blobs — for example with
> [`git filter-repo`](https://github.com/newren/git-filter-repo):
>
> ```bash
> git filter-repo --invert-paths \
>   --path backend/cookie.txt \
>   --path backend/ctn.db --path backend/ctn_v2.db --path backend/test.db
> ```
>
> This rewrites every commit and requires a force-push, so it is left as a
> deliberate manual step.

---

## Stack

Python (FastAPI, SQLite, web3.py) · vanilla HTML/CSS/JS, no build step ·
Solidity on Polygon Amoy · IPFS via Pinata.
