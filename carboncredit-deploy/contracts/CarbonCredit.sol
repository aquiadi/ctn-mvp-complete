// SPDX-License-Identifier: MIT
pragma solidity ^0.8.0;

contract CarbonCredit {
    address public owner;
    
    struct Credit {
        string ipfsHash;
        uint256 energyKwh;
        uint256 co2AvoidedKg;
        uint256 timestamp;
        bool retired;
        address holder;
    }
    
    mapping(uint256 => Credit) public credits;
    uint256 public totalCredits;
    
    event CreditMinted(uint256 indexed id, string ipfsHash, address holder);
    event CreditRetired(uint256 indexed id, address holder);
    event CreditTransferred(uint256 indexed id, address newHolder);
    
    constructor() {
        owner = msg.sender;
    }
    
    modifier onlyOwner() {
        require(msg.sender == owner, "Not authorized");
        _;
    }
    
    function mintCredit(
        address recipient,
        string memory ipfsHash,
        uint256 energyKwh,
        uint256 co2AvoidedKg
    ) external onlyOwner {
        totalCredits++;
        credits[totalCredits] = Credit(
            ipfsHash,
            energyKwh,
            co2AvoidedKg,
            block.timestamp,
            false,
            recipient
        );
        emit CreditMinted(totalCredits, ipfsHash, recipient);
    }
    
    function retireCredit(uint256 creditId) external {
        require(credits[creditId].holder == msg.sender, "Not your credit");
        require(!credits[creditId].retired, "Already retired");
        credits[creditId].retired = true;
        emit CreditRetired(creditId, msg.sender);
    }

    /// @notice Owner can transfer a credit to a new holder (e.g. custodial → installer,
    ///         or as part of a marketplace sale)
    function transferCredit(uint256 creditId, address newHolder) external onlyOwner {
        require(credits[creditId].holder != address(0), "Credit does not exist");
        require(!credits[creditId].retired, "Cannot transfer retired credit");
        credits[creditId].holder = newHolder;
        emit CreditTransferred(creditId, newHolder);
    }

    /// @notice Owner can retire a credit on behalf of any holder (used by marketplace
    ///         after purchase is complete)
    function retireCreditFor(uint256 creditId) external onlyOwner {
        require(credits[creditId].holder != address(0), "Credit does not exist");
        require(!credits[creditId].retired, "Already retired");
        credits[creditId].retired = true;
        emit CreditRetired(creditId, credits[creditId].holder);
    }
    
    function getCredit(uint256 creditId) external view returns (Credit memory) {
        return credits[creditId];
    }
}
