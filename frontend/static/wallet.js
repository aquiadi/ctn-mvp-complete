/**
 * Wallet linking via EIP-1193 (MetaMask and compatible providers).
 *
 * The account proves control of its address by signing a server-issued nonce;
 * no private key or seed phrase ever leaves the wallet.
 */
(function (global) {
  'use strict';

  var CTN = global.CTN;

  function isAvailable() {
    return typeof global.ethereum !== 'undefined';
  }

  function requestAccount() {
    if (!isAvailable()) {
      return Promise.reject(new Error(
        'No Ethereum wallet detected. Install MetaMask, or another EIP-1193 wallet, to continue.'
      ));
    }
    return global.ethereum.request({ method: 'eth_requestAccounts' }).then(function (accounts) {
      if (!accounts || !accounts.length) throw new Error('No account was authorised.');
      return accounts[0];
    });
  }

  /**
   * Full linking flow: connect, fetch a challenge, sign it, submit the proof.
   * Resolves with the linked address.
   */
  function link() {
    var address;

    return requestAccount()
      .then(function (account) {
        address = account;
        return CTN.post('/api/auth/nonce');
      })
      .then(function (challenge) {
        return global.ethereum.request({
          method: 'personal_sign',
          params: [challenge.message, address],
        }).then(function (signature) {
          return CTN.post('/api/auth/link-wallet', {
            wallet_address: address,
            nonce: challenge.nonce,
            signature: signature,
          });
        });
      })
      .then(function (result) { return result.wallet_address; })
      .catch(function (error) {
        // 4001 is the EIP-1193 code for a user-rejected request.
        if (error && error.code === 4001) {
          throw new Error('Signature request was rejected in your wallet.');
        }
        throw error;
      });
  }

  global.CTNWallet = { isAvailable: isAvailable, link: link };
})(window);
