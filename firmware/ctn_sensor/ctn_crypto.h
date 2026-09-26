// Device identity and signing: secp256k1 keys, Ethereum addresses, EIP-191.
//
// Portable C++ with no Arduino dependency, so the exact code that runs on the
// ESP32 is also compiled on a host and checked byte for byte against the
// server's verifier (see firmware/test).
#pragma once

#include <stddef.h>
#include <stdint.h>

namespace ctn {

constexpr size_t kPrivateKeySize = 32;
constexpr size_t kPublicKeySize = 64;   // uncompressed X || Y, no 0x04 prefix
constexpr size_t kSignatureSize = 64;   // r || s
constexpr size_t kDigestSize = 32;
constexpr size_t kAddressHexSize = 43;  // "0x" + 40 hex + NUL

// Ethereum's Keccak-256: the original Keccak padding (0x01), not FIPS 202
// SHA3-256 (0x06). The two produce different digests for every input, and
// using SHA3 makes every address and signature fail verification.
void keccak256(const uint8_t* data, size_t length, uint8_t out[kDigestSize]);

// Key generation draws from the RNG installed with uECC_set_rng().
bool generateKeypair(uint8_t privateKey[kPrivateKeySize], uint8_t publicKey[kPublicKeySize]);
bool publicKeyFromPrivate(const uint8_t privateKey[kPrivateKeySize],
                          uint8_t publicKey[kPublicKeySize]);

// Last 20 bytes of keccak256(publicKey), as lowercase "0x…" hex.
void addressHex(const uint8_t publicKey[kPublicKeySize], char out[kAddressHexSize]);

// keccak256("\x19Ethereum Signed Message:\n" + decimal length + message).
void eip191Digest(const char* message, size_t length, uint8_t out[kDigestSize]);

// Sign an EIP-191 message. The nonce is derived deterministically from the key
// and digest with HMAC-SHA256 (micro-ecc's RFC 6979 variant, which reads the
// DRBG output in native word order, so signatures are valid but not byte-equal
// to other RFC 6979 signers). A weak or failed RNG therefore cannot leak the
// key through nonce reuse. s is normalised to the lower half of the curve
// order, as Ethereum tooling expects. Returns false only if the key is invalid.
bool signMessage(const uint8_t privateKey[kPrivateKeySize], const char* message, size_t length,
                 uint8_t signature[kSignatureSize]);

// "0x" + lowercase hex. `out` must hold 2 + 2 * length + 1 bytes.
void toHex(const uint8_t* bytes, size_t length, char* out);

}  // namespace ctn
