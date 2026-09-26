// Deploys CarbonCreditV2.
//
//   PRIVATE_KEY=... npx hardhat run scripts/deploy.js --network amoy
//
// CTN_OWNER sets the initial owner (defaults to the deployer). To move control
// to a multisig afterwards, call transferOwnership(safe) and accept from the
// safe; ownership is two-step, so a mistyped address cannot take it.
const { ethers, network } = require("hardhat");

async function main() {
  const [deployer] = await ethers.getSigners();
  if (!deployer) throw new Error("No deployer account. Set PRIVATE_KEY for this network.");

  const owner = process.env.CTN_OWNER || deployer.address;
  console.log(`network   ${network.name} (chain ${network.config.chainId ?? "local"})`);
  console.log(`deployer  ${deployer.address}`);
  console.log(`owner     ${owner}`);

  const Factory = await ethers.getContractFactory("CarbonCreditV2");
  const contract = await Factory.deploy(owner);
  const receipt = await contract.deploymentTransaction().wait();

  const address = await contract.getAddress();
  console.log(`contract  ${address}`);
  console.log(`block     ${receipt.blockNumber}`);
  console.log(`gas used  ${receipt.gasUsed}`);
  console.log("");
  console.log("Point the API at it:");
  console.log(`  CONTRACT_ADDRESS=${address}`);
  console.log("  CONTRACT_VERSION=2");
  console.log("Verify the source:");
  console.log(`  npx hardhat verify --network ${network.name} ${address} ${owner}`);
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
