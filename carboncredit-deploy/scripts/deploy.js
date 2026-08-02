const { ethers } = require("hardhat");

async function main() {
  console.log("Deploying CarbonCredit...");
  const CarbonCredit = await ethers.getContractFactory("CarbonCredit");
  const contract = await CarbonCredit.deploy();
  await contract.waitForDeployment();
  console.log("✅ Deployed to:", await contract.getAddress());
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
