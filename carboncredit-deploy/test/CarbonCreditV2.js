const { expect } = require("chai");
const { ethers } = require("hardhat");

describe("CarbonCreditV2", function () {
  let contract, owner, alice, bob, safe;

  const CID = "bafkreigh2akiscaildcqabsyg3dfr6chu3fgpregiymsck7e7aqa4s52zy";
  const OTHER_CID = "bafkreibm6jg3ux5qumhcn2b3flc3tyu6dmlb4xa7u5bf44yegnrjhc4yeq";

  async function mint(to = alice.address, cid = CID) {
    await contract.mintCredit(to, cid, 1_219_512, 1_000_000);
  }

  beforeEach(async function () {
    [owner, alice, bob, safe] = await ethers.getSigners();
    const Factory = await ethers.getContractFactory("CarbonCreditV2");
    contract = await Factory.deploy(owner.address);
  });

  describe("minting", function () {
    it("mints an ERC-721 token and records the certificate", async function () {
      await expect(contract.mintCredit(alice.address, CID, 1_219_512, 1_000_000))
        .to.emit(contract, "CreditMinted")
        .withArgs(1, CID, alice.address);

      expect(await contract.ownerOf(1)).to.equal(alice.address);
      expect(await contract.totalCredits()).to.equal(1);
      expect(await contract.tokenURI(1)).to.equal(`ipfs://${CID}`);

      const credit = await contract.getCredit(1);
      expect(credit.ipfsHash).to.equal(CID);
      expect(credit.co2AvoidedKg).to.equal(1_000_000);
      expect(credit.holder).to.equal(alice.address);
      expect(credit.retired).to.equal(false);
    });

    it("refuses a second token for the same certificate", async function () {
      await mint();
      await expect(contract.mintCredit(bob.address, CID, 1, 1))
        .to.be.revertedWithCustomError(contract, "CertificateAlreadyMinted")
        .withArgs(1);
    });

    it("refuses an empty certificate reference", async function () {
      await expect(contract.mintCredit(alice.address, "", 1, 1))
        .to.be.revertedWithCustomError(contract, "EmptyCertificate");
    });

    it("only lets the owner mint", async function () {
      await expect(contract.connect(alice).mintCredit(alice.address, CID, 1, 1))
        .to.be.revertedWithCustomError(contract, "OwnableUnauthorizedAccount");
    });

    it("returns an empty record for an id never minted, like V1", async function () {
      const credit = await contract.getCredit(42);
      expect(credit.ipfsHash).to.equal("");
      expect(credit.holder).to.equal(ethers.ZeroAddress);
    });
  });

  describe("retirement", function () {
    it("lets the holder retire for a named beneficiary", async function () {
      await mint();
      await expect(contract.connect(alice).retire(1, "Acme Ltd FY2026 Scope 2"))
        .to.emit(contract, "CreditRetired")
        .withArgs(1, alice.address, "Acme Ltd FY2026 Scope 2");

      expect((await contract.getCredit(1)).retired).to.equal(true);
      expect(await contract.retirementBeneficiary(1)).to.equal("Acme Ltd FY2026 Scope 2");
    });

    it("does not let anyone else retire a holder's credit", async function () {
      await mint();
      await expect(contract.connect(bob).retire(1, "Bob"))
        .to.be.revertedWithCustomError(contract, "NotCreditHolder")
        .withArgs(1, bob.address);
    });

    it("lets the owner retire a custodial credit on a buyer's behalf", async function () {
      await mint(owner.address);
      await expect(contract.retireCreditFor(1, "Buyer #17"))
        .to.emit(contract, "CreditRetired")
        .withArgs(1, owner.address, "Buyer #17");
    });

    it("cannot retire twice", async function () {
      await mint();
      await contract.connect(alice).retire(1, "First");
      await expect(contract.connect(alice).retire(1, "Second"))
        .to.be.revertedWithCustomError(contract, "CreditAlreadyRetired");
    });

    it("cannot retire a credit that does not exist", async function () {
      await expect(contract.retireCreditFor(7, "Nobody"))
        .to.be.revertedWithCustomError(contract, "ERC721NonexistentToken");
    });

    it("freezes a retired credit so it cannot be resold", async function () {
      await mint();
      await contract.connect(alice).retire(1, "Claimed");
      await expect(contract.connect(alice).transferFrom(alice.address, bob.address, 1))
        .to.be.revertedWithCustomError(contract, "RetiredCreditIsFrozen")
        .withArgs(1);
    });
  });

  describe("transfers", function () {
    it("moves like any ERC-721 before retirement and keeps getCredit in step", async function () {
      await mint();
      await contract.connect(alice).transferFrom(alice.address, bob.address, 1);
      expect((await contract.getCredit(1)).holder).to.equal(bob.address);
    });
  });

  describe("ownership", function () {
    it("hands control to a multisig only once it accepts", async function () {
      await contract.transferOwnership(safe.address);
      expect(await contract.owner()).to.equal(owner.address);

      await contract.connect(safe).acceptOwnership();
      expect(await contract.owner()).to.equal(safe.address);

      await expect(contract.mintCredit(alice.address, OTHER_CID, 1, 1))
        .to.be.revertedWithCustomError(contract, "OwnableUnauthorizedAccount");
    });
  });
});
