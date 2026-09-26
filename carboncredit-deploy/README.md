# CTN contracts

On-chain records for CTN credits. Each token stands for one tonne of avoided
CO₂ as calculated by the CTN methodology and points at the certificate on IPFS
that the calculation came from.

These are **prototype impact records**, not credits issued under an accredited
programme. The contract proves what was recorded and when; the certificate
carries the evidence; neither makes the accounting official.

## Contracts

| Contract | Status | Notes |
|---|---|---|
| `CarbonCredit.sol` | Deployed on Amoy at `0x1b4F5A7CEf1c2CFb914A5642CC82F887AB0C7Cf6` | V1. Plain struct registry, single owner key, no guard against minting the same certificate twice. Kept unchanged because it is what the live deployment runs. |
| `CarbonCreditV2.sol` | Ready to deploy | ERC-721 (`CTN-IR`), one token per certificate, retirement records a beneficiary, retired tokens are frozen, two-step ownership for handing control to a multisig. |

V2 keeps V1's `mintCredit`, `getCredit`, `totalCredits`, `owner`, and
`CreditMinted` interface, so the API reads and mints against either. Only
retirement differs, and the API picks the right call from `CONTRACT_VERSION`.

Quantities are stored ×1000: `energyKwh` in Wh and `co2AvoidedKg` in grams.

## Develop

```bash
npm ci
npx hardhat test          # no keys needed
REPORT_GAS=1 npx hardhat test
npx hardhat coverage
```

## Deploy V2 to Amoy

```bash
PRIVATE_KEY=0x…  npx hardhat run scripts/deploy.js --network amoy
# optional: CTN_OWNER=0x… to set an owner other than the deployer
npx hardhat verify --network amoy <address> <owner>
```

Then set `CONTRACT_ADDRESS=<address>` and `CONTRACT_VERSION=2` on the API, and
fund the API's signing wallet with test POL.

### Handing control to a multisig

The owner can mint and retire custodial credits, so it should not be a single
hot key in production. Create a Safe, then:

```
transferOwnership(<safe>)      # from the current owner
acceptOwnership()              # from the Safe
```

Until the Safe accepts, the current owner keeps control, so a mistyped address
cannot lock the contract. Once the Safe owns it, the API's key can no longer
mint; minting then goes through Safe transactions or a dedicated minter role.
