require("@nomicfoundation/hardhat-toolbox");
require("dotenv").config();

// Tests and compilation must work with no secrets present. The deployer key is
// only attached to the live network, and only when it is actually set.
const deployerKey = process.env.PRIVATE_KEY
  ? [process.env.PRIVATE_KEY.startsWith("0x") ? process.env.PRIVATE_KEY : `0x${process.env.PRIVATE_KEY}`]
  : [];

module.exports = {
  solidity: {
    version: "0.8.26",
    settings: {
      optimizer: { enabled: true, runs: 200 },
      evmVersion: "cancun",
    },
  },
  networks: {
    amoy: {
      url: process.env.AMOY_RPC || "https://polygon-amoy-bor-rpc.publicnode.com",
      chainId: 80002,
      accounts: deployerKey,
    },
  },
  etherscan: {
    apiKey: process.env.POLYGONSCAN_API_KEY || "",
  },
  gasReporter: {
    enabled: Boolean(process.env.REPORT_GAS),
  },
};
