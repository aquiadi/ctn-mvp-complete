const { expect } = require("chai");
const { ethers } = require("hardhat");

describe("CarbonCredit", function () {
  let carbonCredit;
  let owner;
  let user1;
  let user2;

  beforeEach(async function () {
    [owner, user1, user2] = await ethers.getSigners();
    const CarbonCredit = await ethers.getContractFactory("CarbonCredit");
    carbonCredit = await CarbonCredit.deploy();
  });

  it("Should allow owner to transfer an unretired credit", async function () {
    // Mint a credit to user1
    await carbonCredit.mintCredit(user1.address, "ipfs://hash", 1000, 500);
    
    // Transfer from user1 to user2 (onlyOwner)
    await expect(carbonCredit.transferCredit(1, user2.address))
      .to.emit(carbonCredit, "CreditTransferred")
      .withArgs(1, user2.address);

    const credit = await carbonCredit.getCredit(1);
    expect(credit.holder).to.equal(user2.address);
  });

  it("Should allow owner to retire a credit for any holder", async function () {
    // Mint to user1
    await carbonCredit.mintCredit(user1.address, "ipfs://hash", 1000, 500);

    // Retire for user1 (onlyOwner)
    await expect(carbonCredit.retireCreditFor(1))
      .to.emit(carbonCredit, "CreditRetired")
      .withArgs(1, user1.address);

    const credit = await carbonCredit.getCredit(1);
    expect(credit.retired).to.be.true;
  });

  it("Should prevent non-owners from transferring or retiring for others", async function () {
    await carbonCredit.mintCredit(user1.address, "ipfs://hash", 1000, 500);

    await expect(
      carbonCredit.connect(user1).transferCredit(1, user2.address)
    ).to.be.revertedWith("Not authorized");

    await expect(
      carbonCredit.connect(user1).retireCreditFor(1)
    ).to.be.revertedWith("Not authorized");
  });

  it("Should prevent transferring a retired credit", async function () {
    await carbonCredit.mintCredit(user1.address, "ipfs://hash", 1000, 500);
    await carbonCredit.retireCreditFor(1);

    await expect(
      carbonCredit.transferCredit(1, user2.address)
    ).to.be.revertedWith("Cannot transfer retired credit");
  });
});
