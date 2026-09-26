const { expect } = require("chai");
const { ethers } = require("hardhat");

describe("CTNSettlement", function () {
  let settlement, owner, seller, treasury, reserve, buyer, other;
  const REF = ethers.id("ctn-sale-42");

  async function deploy(split = [7000, 2000, 1000]) {
    const Factory = await ethers.getContractFactory("CTNSettlement");
    return Factory.deploy(owner.address, treasury.address, reserve.address, ...split);
  }

  beforeEach(async function () {
    [owner, seller, treasury, reserve, buyer, other] = await ethers.getSigners();
    settlement = await deploy();
  });

  it("splits a payment 70/20/10", async function () {
    await expect(settlement.connect(buyer).settle(seller.address, REF, { value: 1000n }))
      .to.emit(settlement, "Settled")
      .withArgs(REF, seller.address, 1000n, 700n, 200n, 100n);

    expect(await settlement.owed(seller.address)).to.equal(700n);
    expect(await settlement.owed(treasury.address)).to.equal(200n);
    expect(await settlement.owed(reserve.address)).to.equal(100n);
    expect(await settlement.totalOwed()).to.equal(1000n);
  });

  it("gives rounding to the reserve so shares always sum to the payment", async function () {
    await settlement.connect(buyer).settle(seller.address, REF, { value: 333n });
    expect(await settlement.owed(seller.address)).to.equal(233n);
    expect(await settlement.owed(treasury.address)).to.equal(66n);
    expect(await settlement.owed(reserve.address)).to.equal(34n);
  });

  it("pays out on withdraw, once", async function () {
    const value = ethers.parseEther("1");
    await settlement.connect(buyer).settle(seller.address, REF, { value });

    await expect(settlement.connect(seller).withdraw())
      .to.changeEtherBalances([seller, settlement], [ethers.parseEther("0.7"), -ethers.parseEther("0.7")]);
    await expect(settlement.connect(seller).withdraw())
      .to.be.revertedWithCustomError(settlement, "NothingOwed");
    expect(await settlement.totalOwed()).to.equal(ethers.parseEther("0.3"));
  });

  it("keeps the contract balance equal to what is owed", async function () {
    await settlement.connect(buyer).settle(seller.address, REF, { value: 12345n });
    await settlement.connect(treasury).withdraw();
    const balance = await ethers.provider.getBalance(await settlement.getAddress());
    expect(balance).to.equal(await settlement.totalOwed());
  });

  it("refuses a split that does not total 100%", async function () {
    const Factory = await ethers.getContractFactory("CTNSettlement");
    await expect(Factory.deploy(owner.address, treasury.address, reserve.address, 7000, 2000, 999))
      .to.be.revertedWithCustomError(Factory, "InvalidSplit");
  });

  it("refuses empty payments and a zero seller", async function () {
    await expect(settlement.settle(seller.address, REF))
      .to.be.revertedWithCustomError(settlement, "NoPayment");
    await expect(settlement.settle(ethers.ZeroAddress, REF, { value: 1n }))
      .to.be.revertedWithCustomError(settlement, "ZeroAddress");
  });

  it("lets only the owner redirect treasury and reserve, leaving old balances in place", async function () {
    await settlement.connect(buyer).settle(seller.address, REF, { value: 1000n });
    await expect(settlement.connect(other).setPayees(other.address, other.address))
      .to.be.revertedWithCustomError(settlement, "OwnableUnauthorizedAccount");

    await settlement.setPayees(other.address, other.address);
    await settlement.connect(buyer).settle(seller.address, REF, { value: 1000n });
    expect(await settlement.owed(treasury.address)).to.equal(200n);
    expect(await settlement.owed(other.address)).to.equal(300n);
  });
});
