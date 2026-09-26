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
| `CarbonCredit.sol` | Deployed on Amoy at `0x1b4F5A7CEf1c2CFb914A5642CC82F887AB0C7Cf6` | V1, now legacy. Plain struct registry, single owner key, no on-chain guard against minting the same certificate twice. Kept unchanged because credits minted before the move to V2 still live on it. |
| `CarbonCreditV2.sol` | **Live** on Amoy at `0x890b51626Cc77E41d83fCaa147CF57955d62fA1c` | ERC-721 (`CTN-IR`), one token per certificate, retirement records a beneficiary, retired tokens are frozen, two-step ownership for handing control to a multisig. |
| `CTNSettlement.sol` | Not yet deployed (needs treasury and reserve wallets) | Splits each payment 70/20/10 between the seller, treasury, and reserve (fixed at deploy). Pull payments: `settle()` credits balances, `withdraw()` pays them out. Rounding goes to the reserve, so shares always sum to the payment. |

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

## Deploy to Amoy

The easy way is the **Deploy contracts** GitHub Action (see the main README).
Locally:

```bash
PRIVATE_KEY=0x… TREASURY_ADDRESS=0x… RESERVE_ADDRESS=0x… \
  npx hardhat run scripts/deploy.js --network amoy
```

The script checks the chain id and the deployer's balance first. It writes
`deployments/amoy.json` with addresses, transactions, and constructor
arguments. The owner defaults to the deployer; set `CTN_OWNER` to change it.
The owner must be the API's signing wallet, or the API cannot mint.

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
