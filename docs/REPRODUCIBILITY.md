# Reproducibility

What a reviewer needs to regenerate CTN's figures, and an honest account of
where each part of the system stands.

## Evaluation data

The demo dataset the deployed site runs on was **not measured by CTN
hardware**. It is a replay of a public dataset:

> A. Kannal, *Solar Power Generation Data*, Kaggle, 2020.
> Two plants in India, 34 days (15 May – 17 June 2020), 15-minute inverter
> readings. `1BY6WEcLGh8j5v7` is an inverter `SOURCE_KEY` from Plant 1.

The seed file pinned on IPFS (`IPFS_SEED_URL` in `backend/config.py`) was
produced by `ctn-mvp-colab.ipynb`. That notebook has known problems, recorded
in its first cell. It derives energy as `DAILY_YIELD / 96` per row, which is
not interval energy. Its "credits" are 50 kWh units. Its "signatures" are
SHA-256 over the record plus a secret printed in the notebook, computed in
Colab rather than on a device. It also labels the records "Patna, Bihar",
which is a demo placeholder, not the plant's location.

Describe any result built on this data as a replay of the cited dataset, not
as a field measurement.

### Regenerating the figures

```bash
# Download Plant_1_Generation_Data.csv from the Kaggle dataset page, then:
python tools/replay_dataset.py Plant_1_Generation_Data.csv --source-key 1BY6WEcLGh8j5v7
```

This prints the sample count, date range, days, total and mean daily energy,
the inverter's independent `TOTAL_YIELD` advance as a cross-check, avoided CO₂
at the configured factor, and how many whole one-tonne records that makes plus
the remainder. With `--out readings.json` it also writes the interval
readings in the shape the platform ingests. Unit tests for the arithmetic are
in `backend/tests/test_replay_dataset.py`.

State in the paper the numbers this prints, the dataset citation, and the
emission factor with its source and vintage (below).

## Emission factor

`EMISSION_FACTOR = 0.82 kg CO₂/kWh`, the CEA CO₂ Baseline Database v19.0
weighted average for FY2022-23 (0.817 t/MWh), rounded. It is a versioned
parameter:

- every reading stores the factor it was computed with (`emission_factor`)
- every certificate carries `methodology.factor_value`, `factor_unit`,
  `factor_source`, and `factor_vintage`
- changing the factor affects new readings only; history is never restated

A crediting methodology for grid-connected solar would normally use a combined
margin rather than a generation-weighted average. That choice belongs to the
methodology, not the code, and is not made here.

## What each credit can be checked against

Each credit stores a `ctn-certificate/v2` document, and its `local-<sha256>`
hash (or IPFS CID once pinned) commits to those exact bytes. Holding only that
document, anyone can:

1. recover the EIP-191 signer of every attested reading's `signed_message`
   and compare it with `device.public_key`;
2. add up `allocated_kg` across readings and confirm it equals
   `co2_avoided_kg`;
3. see which readings were imported rather than attested, and whether any
   were flagged by screening.

`GET /verify/{credit_id}` also compares the on-chain record with the database.

## Test suites

| Suite | Command | What it covers |
|---|---|---|
| Backend | `cd backend && python -m pytest` | Attestation, plausibility rules, V2 meter/tamper protocol, allocation, certificates, anomaly screening, exactly-once minting, marketplace, accounts |
| Contracts | `cd carboncredit-deploy && npx hardhat test` | V1 as deployed; V2 minting, duplicate certificates, retirement, frozen retired tokens, two-step ownership |
| Firmware | `firmware/test/run_host_test.sh` | The firmware's crypto and protocol code, built for the host and checked byte for byte against the server |

All three run in CI on every push.

## Status of components described in the technical paper

| Component | State in this repository |
|---|---|
| Device identity and packet signing | secp256k1 key generated on the device; EIP-191 signatures. The server holds only the public key, so it cannot forge readings. This is stronger than a shared-secret HMAC. |
| Replay protection | Monotonic per-device sequence numbers |
| Timestamp window | ±300 s against server time, strictly increasing, 72 h backfill window, never before registration |
| Physical plausibility | Energy bounded by declared rated capacity × elapsed interval; V2 readings must match the device's lifetime meter counter |
| Tamper detection | Reed-switch counter signed into every V2 reading; an increase withdraws installation confirmation |
| Transport | HTTPS with the server certificate validated against pinned roots |
| Sensing hardware | PZEM-004T, DS3231, and reed-switch drivers written; firmware crypto verified in CI; **not yet run on a physical board** |
| Anomaly screening | Night generation, flatlines, robust outliers. Flags for human review, never rejects. |
| Evidence storage | Self-verifying certificate per credit; pinned byte-exact to IPFS when Pinata is configured |
| Settlement | CarbonCreditV2 (ERC-721, one token per certificate, retirement beneficiary) live on Polygon Amoy at `0x890b51626Cc77E41d83fCaa147CF57955d62fA1c`; the original V1 at `0x1b4F…7Cf6` still serves credits minted before the move. |
| Payment split | 70/20/10 recorded for every sale in exact integer amounts, and `CTNSettlement` applies it on-chain. Marketplace payments are still simulated, so no funds move yet. |
| HSM / secure element, decentralised oracle, on-chain device registry | Not implemented |
| Third-party validation and verification, registry listing | Not undertaken |

## Before citing a release

1. Tag the commit your results came from.
2. Connect the repository to Zenodo and create a GitHub release, so the tag
   gets a DOI. `CITATION.cff` supplies the metadata.
3. Put the DOI in the README and the paper.
4. Archive the output of `tools/replay_dataset.py` and the transaction hashes
   of any on-chain records you cite, alongside the release.
