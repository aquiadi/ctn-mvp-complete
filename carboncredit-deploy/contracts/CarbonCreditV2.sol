// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {ERC721} from "@openzeppelin/contracts/token/ERC721/ERC721.sol";
import {Ownable} from "@openzeppelin/contracts/access/Ownable.sol";
import {Ownable2Step} from "@openzeppelin/contracts/access/Ownable2Step.sol";

/// @title CTN solar impact records
/// @notice Each token is one tonne of avoided CO2 as calculated by the CTN
///         methodology, backed by a certificate pinned to IPFS. These are
///         prototype impact records, not credits issued under an accredited
///         crediting programme.
/// @dev Keeps the V1 read and mint interface (mintCredit, getCredit,
///      totalCredits, owner, CreditMinted) so the API can talk to either
///      contract. What V2 adds:
///        - records are ERC-721 tokens, so holders see and move them in any wallet
///        - a certificate can back exactly one token; a second mint reverts
///        - retirement records who the offset was claimed for
///        - retired tokens are frozen in place rather than burned, so the record
///          and its certificate remain resolvable forever
///        - two-step ownership transfer, so control can be handed to a multisig
///          without the risk of typing the wrong address
contract CarbonCreditV2 is ERC721, Ownable2Step {
    struct Credit {
        string ipfsHash;
        uint256 energyKwh;      // scaled by 1000 (Wh precision)
        uint256 co2AvoidedKg;   // scaled by 1000 (g precision)
        uint256 timestamp;
        bool retired;
        address holder;         // mirrors ownerOf(); kept for V1 ABI compatibility
    }

    mapping(uint256 => Credit) private _credits;
    mapping(bytes32 => uint256) public creditIdByCertificate;
    mapping(uint256 => string) public retirementBeneficiary;
    uint256 public totalCredits;

    event CreditMinted(uint256 indexed id, string ipfsHash, address holder);
    event CreditRetired(uint256 indexed id, address holder, string beneficiary);

    error EmptyCertificate();
    error CertificateAlreadyMinted(uint256 existingId);
    error UnknownCredit(uint256 id);
    error CreditAlreadyRetired(uint256 id);
    error NotCreditHolder(uint256 id, address caller);
    error RetiredCreditIsFrozen(uint256 id);

    constructor(address initialOwner)
        ERC721("CTN Solar Impact Record", "CTN-IR")
        Ownable(initialOwner)
    {}

    // ── Issuance ──────────────────────────────────────────────────────────

    function mintCredit(
        address recipient,
        string calldata ipfsHash,
        uint256 energyKwh,
        uint256 co2AvoidedKg
    ) external onlyOwner returns (uint256 id) {
        if (bytes(ipfsHash).length == 0) revert EmptyCertificate();

        bytes32 key = keccak256(bytes(ipfsHash));
        uint256 existing = creditIdByCertificate[key];
        if (existing != 0) revert CertificateAlreadyMinted(existing);

        id = ++totalCredits;
        creditIdByCertificate[key] = id;
        _credits[id] = Credit(ipfsHash, energyKwh, co2AvoidedKg, block.timestamp, false, address(0));

        // _mint rather than _safeMint: the platform custody wallet and
        // multisigs are valid recipients, and a receiver hook would let a
        // recipient contract block issuance.
        _mint(recipient, id);
        emit CreditMinted(id, ipfsHash, recipient);
    }

    // ── Retirement ────────────────────────────────────────────────────────

    /// @notice Retire a credit you hold, claiming the offset for `beneficiary`.
    function retire(uint256 id, string calldata beneficiary) external {
        address holder = _requireOwned(id);
        if (holder != msg.sender) revert NotCreditHolder(id, msg.sender);
        _retire(id, holder, beneficiary);
    }

    /// @notice Retire a credit held in platform custody on behalf of a buyer
    ///         who purchased it off-chain.
    function retireCreditFor(uint256 id, string calldata beneficiary) external onlyOwner {
        _retire(id, _requireOwned(id), beneficiary);
    }

    function _retire(uint256 id, address holder, string calldata beneficiary) private {
        if (_credits[id].retired) revert CreditAlreadyRetired(id);
        _credits[id].retired = true;
        retirementBeneficiary[id] = beneficiary;
        emit CreditRetired(id, holder, beneficiary);
    }

    // ── Views ─────────────────────────────────────────────────────────────

    /// @notice A credit's record. An id never minted returns an empty record
    ///         rather than reverting, matching V1.
    function getCredit(uint256 id) external view returns (Credit memory credit) {
        credit = _credits[id];
        credit.holder = _ownerOf(id);
    }

    function tokenURI(uint256 id) public view override returns (string memory) {
        _requireOwned(id);
        return string.concat("ipfs://", _credits[id].ipfsHash);
    }

    // ── Transfer rules ────────────────────────────────────────────────────

    /// @dev A retired credit has been claimed against someone's emissions. It
    ///      stays where it was retired; moving it would let it be sold again.
    function _update(address to, uint256 id, address auth)
        internal
        override
        returns (address)
    {
        if (_credits[id].retired) revert RetiredCreditIsFrozen(id);
        return super._update(to, id, auth);
    }
}
