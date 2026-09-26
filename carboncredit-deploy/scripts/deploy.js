// Deploys CarbonCreditV2 and, when payees are given, CTNSettlement.
//
//   PRIVATE_KEY=0x… npx hardhat run scripts/deploy.js --network amoy
//
// Environment:
//   CTN_OWNER          owner of both contracts. Defaults to the deployer. It must
//                      be the API's signing wallet (its PRIVATE_KEY) or the API
//                      cannot mint on V2.
//   TREASURY_ADDRESS   CTN treasury, receives the treasury share
//   RESERVE_ADDRESS    operational reserve, receives the reserve share
//   SPLIT_SELLER_BPS, SPLIT_TREASURY_BPS, SPLIT_RESERVE_BPS
//                      default 7000 / 2000 / 1000; must match the API's config
//   DEPLOY_SETTLEMENT  "false" to skip the splitter
//
// Writes deployments/<network>.json and, under GitHub Actions, a job summary.
const fs = require("fs");
const path = require("path");
const { ethers, network } = require("hardhat");

const EXPECTED_CHAIN = { amoy: 80002n };

function split() {
  const parts = ["SPLIT_SELLER_BPS", "SPLIT_TREASURY_BPS", "SPLIT_RESERVE_BPS"].map(
    (name, i) => BigInt(process.env[name] || [7000, 2000, 1000][i])
  );
  if (parts.reduce((a, b) => a + b, 0n) !== 10000n) {
    throw new Error(`Split must total 10000 bps, got ${parts.join("/")}`);
  }
  return parts;
}

function address(name, value) {
  if (!ethers.isAddress(value)) throw new Error(`${name} is not an address: ${value}`);
  return ethers.getAddress(value);
}

async function main() {
  const [deployer] = await ethers.getSigners();
  if (!deployer) throw new Error("No deployer account. Set PRIVATE_KEY for this network.");

  const { chainId } = await ethers.provider.getNetwork();
  const expected = EXPECTED_CHAIN[network.name];
  if (expected && chainId !== expected) {
    throw new Error(`RPC for ${network.name} reports chain ${chainId}, expected ${expected}`);
  }

  const owner = address("CTN_OWNER", process.env.CTN_OWNER || deployer.address);
  const balance = await ethers.provider.getBalance(deployer.address);
  console.log(`network   ${network.name} (chain ${chainId})`);
  console.log(`deployer  ${deployer.address}  balance ${ethers.formatEther(balance)}`);
  console.log(`owner     ${owner}`);
  if (balance === 0n) throw new Error("Deployer has no balance to pay gas.");

  const record = {
    network: network.name,
    chainId: chainId.toString(),
    deployer: deployer.address,
    owner,
    deployedAt: new Date().toISOString(),
    contracts: {},
  };

  const V2 = await ethers.getContractFactory("CarbonCreditV2");
  const credit = await V2.deploy(owner);
  const creditReceipt = await credit.deploymentTransaction().wait();
  record.contracts.CarbonCreditV2 = {
    address: await credit.getAddress(),
    tx: creditReceipt.hash,
    block: creditReceipt.blockNumber,
    args: [owner],
  };
  console.log(`CarbonCreditV2  ${record.contracts.CarbonCreditV2.address}`);

  const wantSettlement = process.env.DEPLOY_SETTLEMENT !== "false";
  if (wantSettlement && process.env.TREASURY_ADDRESS && process.env.RESERVE_ADDRESS) {
    const treasury = address("TREASURY_ADDRESS", process.env.TREASURY_ADDRESS);
    const reserve = address("RESERVE_ADDRESS", process.env.RESERVE_ADDRESS);
    const bps = split();
    const Settlement = await ethers.getContractFactory("CTNSettlement");
    const splitter = await Settlement.deploy(owner, treasury, reserve, ...bps);
    const splitterReceipt = await splitter.deploymentTransaction().wait();
    const args = [owner, treasury, reserve, ...bps.map(String)];
    record.contracts.CTNSettlement = {
      address: await splitter.getAddress(),
      tx: splitterReceipt.hash,
      block: splitterReceipt.blockNumber,
      args,
    };
    console.log(`CTNSettlement   ${record.contracts.CTNSettlement.address}  split ${bps.join("/")}`);
  } else if (wantSettlement) {
    console.log("CTNSettlement   skipped: set TREASURY_ADDRESS and RESERVE_ADDRESS to deploy it");
  }

  const dir = path.join(__dirname, "..", "deployments");
  fs.mkdirSync(dir, { recursive: true });
  const file = path.join(dir, `${network.name}.json`);
  fs.writeFileSync(file, JSON.stringify(record, null, 2) + "\n");
  console.log(`wrote ${path.relative(process.cwd(), file)}`);

  const v2 = record.contracts.CarbonCreditV2.address;
  const lines = [
    `## Deployed to ${network.name} (chain ${chainId})`,
    "",
    "| Contract | Address | Tx |",
    "|---|---|---|",
    ...Object.entries(record.contracts).map(([name, c]) => `| ${name} | \`${c.address}\` | \`${c.tx}\` |`),
    "",
    "### Point the API at V2",
    "",
    "Set these on the API service (Railway → Variables), then redeploy it:",
    "",
    "```",
    `CONTRACT_ADDRESS=${v2}`,
    "CONTRACT_VERSION=2",
    "```",
    "",
    "Credits already minted stay readable and retirable on the old contract; they remember where they were minted.",
  ];
  if (owner.toLowerCase() !== deployer.address.toLowerCase()) {
    lines.push("", `Owner is \`${owner}\`, not the deployer. The API can mint only if its PRIVATE_KEY controls that address.`);
  }
  if (process.env.GITHUB_STEP_SUMMARY) fs.appendFileSync(process.env.GITHUB_STEP_SUMMARY, lines.join("\n") + "\n");
  console.log("\n" + lines.join("\n"));
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
