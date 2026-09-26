// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {Ownable} from "@openzeppelin/contracts/access/Ownable.sol";
import {Ownable2Step} from "@openzeppelin/contracts/access/Ownable2Step.sol";
import {ReentrancyGuard} from "@openzeppelin/contracts/utils/ReentrancyGuard.sol";

/// @title CTN settlement splitter
/// @notice Splits each payment for a credit between the generator who produced
///         it, the CTN treasury, and an operational reserve, at shares fixed
///         when the contract is deployed (70/20/10 by default).
/// @dev Pull payments: settle() only credits balances and withdraw() pays them
///      out. A payee whose address reverts on receipt can therefore never block
///      another payee's settlement, and no external call happens while shares
///      are being assigned. The API's settlement_payouts ledger applies the
///      identical split, in the same rounding direction, to simulated payments.
contract CTNSettlement is Ownable2Step, ReentrancyGuard {
    uint256 public constant BPS = 10_000;

    uint256 public immutable sellerBps;
    uint256 public immutable treasuryBps;
    uint256 public immutable reserveBps;

    address public treasury;
    address public reserve;

    mapping(address => uint256) public owed;
    uint256 public totalOwed;

    event Settled(
        bytes32 indexed saleRef,
        address indexed seller,
        uint256 amount,
        uint256 sellerShare,
        uint256 treasuryShare,
        uint256 reserveShare
    );
    event Withdrawn(address indexed payee, uint256 amount);
    event PayeesUpdated(address treasury, address reserve);

    error InvalidSplit();
    error ZeroAddress();
    error NothingOwed();
    error NoPayment();
    error TransferFailed();

    constructor(
        address initialOwner,
        address treasury_,
        address reserve_,
        uint256 sellerBps_,
        uint256 treasuryBps_,
        uint256 reserveBps_
    ) Ownable(initialOwner) {
        if (sellerBps_ + treasuryBps_ + reserveBps_ != BPS) revert InvalidSplit();
        if (treasury_ == address(0) || reserve_ == address(0)) revert ZeroAddress();
        sellerBps = sellerBps_;
        treasuryBps = treasuryBps_;
        reserveBps = reserveBps_;
        treasury = treasury_;
        reserve = reserve_;
    }

    /// @notice Pay for a sale. `saleRef` ties the payment to the off-chain
    ///         sale record (for example keccak256 of the transaction id).
    /// @dev Shares round down; the remainder goes to the reserve, so the three
    ///      shares always sum to msg.value exactly.
    function settle(address seller, bytes32 saleRef) external payable {
        if (msg.value == 0) revert NoPayment();
        if (seller == address(0)) revert ZeroAddress();

        uint256 sellerShare = (msg.value * sellerBps) / BPS;
        uint256 treasuryShare = (msg.value * treasuryBps) / BPS;
        uint256 reserveShare = msg.value - sellerShare - treasuryShare;

        owed[seller] += sellerShare;
        owed[treasury] += treasuryShare;
        owed[reserve] += reserveShare;
        totalOwed += msg.value;

        emit Settled(saleRef, seller, msg.value, sellerShare, treasuryShare, reserveShare);
    }

    /// @notice Withdraw everything owed to the caller.
    function withdraw() external nonReentrant {
        uint256 amount = owed[msg.sender];
        if (amount == 0) revert NothingOwed();
        owed[msg.sender] = 0;
        totalOwed -= amount;

        (bool ok, ) = payable(msg.sender).call{value: amount}("");
        if (!ok) revert TransferFailed();
        emit Withdrawn(msg.sender, amount);
    }

    /// @notice Redirect future treasury and reserve shares. Balances already
    ///         owed stay with the addresses they were owed to.
    function setPayees(address treasury_, address reserve_) external onlyOwner {
        if (treasury_ == address(0) || reserve_ == address(0)) revert ZeroAddress();
        treasury = treasury_;
        reserve = reserve_;
        emit PayeesUpdated(treasury_, reserve_);
    }
}
