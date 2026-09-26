#include "ctn_crypto.h"

#include <stdio.h>
#include <string.h>

#include <KeccakCore.h>
#include <SHA256.h>
#include <uECC.h>

namespace ctn {

namespace {

// secp256k1 group order n, big-endian.
constexpr uint8_t kCurveOrder[32] = {
    0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFE,
    0xBA, 0xAE, 0xDC, 0xE6, 0xAF, 0x48, 0xA0, 0x3B, 0xBF, 0xD2, 0x5E, 0x8C, 0xD0, 0x36, 0x41, 0x41,
};

// n / 2, big-endian. A signature with s above this has a twin at n - s.
constexpr uint8_t kHalfCurveOrder[32] = {
    0x7F, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF,
    0x5D, 0x57, 0x6E, 0x73, 0x57, 0xA4, 0x50, 0x1D, 0xDF, 0xE9, 0x2F, 0x46, 0x68, 0x1B, 0x20, 0xA0,
};

// micro-ecc's RFC 6979 signer takes a hash through this context. The uECC
// struct must be the first member: micro-ecc passes a pointer to it back into
// the callbacks, which cast it to the enclosing type.
struct Sha256Context {
  uECC_HashContext uECC;
  SHA256 sha;
  uint8_t scratch[2 * SHA256::HASH_SIZE + SHA256::BLOCK_SIZE];
};

void shaInit(const uECC_HashContext* base) {
  reinterpret_cast<Sha256Context*>(const_cast<uECC_HashContext*>(base))->sha.reset();
}

void shaUpdate(const uECC_HashContext* base, const uint8_t* message, unsigned length) {
  reinterpret_cast<Sha256Context*>(const_cast<uECC_HashContext*>(base))->sha.update(message, length);
}

void shaFinish(const uECC_HashContext* base, uint8_t* out) {
  reinterpret_cast<Sha256Context*>(const_cast<uECC_HashContext*>(base))
      ->sha.finalize(out, SHA256::HASH_SIZE);
}

bool greaterThan(const uint8_t a[32], const uint8_t b[32]) {
  return memcmp(a, b, 32) > 0;
}

// value = n - value, both big-endian 256-bit.
void subtractFromOrder(uint8_t value[32]) {
  int borrow = 0;
  for (int i = 31; i >= 0; --i) {
    int diff = static_cast<int>(kCurveOrder[i]) - value[i] - borrow;
    borrow = diff < 0;
    value[i] = static_cast<uint8_t>(diff + (borrow ? 256 : 0));
  }
}

}  // namespace

void keccak256(const uint8_t* data, size_t length, uint8_t out[kDigestSize]) {
  KeccakCore core;
  core.setCapacity(512);  // rate 1088 bits: Keccak-256
  core.update(data, length);
  core.pad(0x01);         // Keccak padding. SHA3-256 would be 0x06.
  core.extract(out, kDigestSize);
  core.clear();
}

bool generateKeypair(uint8_t privateKey[kPrivateKeySize], uint8_t publicKey[kPublicKeySize]) {
  return uECC_make_key(publicKey, privateKey, uECC_secp256k1()) == 1;
}

bool publicKeyFromPrivate(const uint8_t privateKey[kPrivateKeySize],
                          uint8_t publicKey[kPublicKeySize]) {
  return uECC_compute_public_key(privateKey, publicKey, uECC_secp256k1()) == 1;
}

void addressHex(const uint8_t publicKey[kPublicKeySize], char out[kAddressHexSize]) {
  uint8_t digest[kDigestSize];
  keccak256(publicKey, kPublicKeySize, digest);
  toHex(digest + 12, 20, out);
}

void eip191Digest(const char* message, size_t length, uint8_t out[kDigestSize]) {
  char prefix[48];
  int prefixLength = snprintf(prefix, sizeof(prefix), "\x19" "Ethereum Signed Message:\n%u",
                              static_cast<unsigned>(length));

  KeccakCore core;
  core.setCapacity(512);
  core.update(prefix, static_cast<size_t>(prefixLength));
  core.update(message, length);
  core.pad(0x01);
  core.extract(out, kDigestSize);
  core.clear();
}

bool signMessage(const uint8_t privateKey[kPrivateKeySize], const char* message, size_t length,
                 uint8_t signature[kSignatureSize]) {
  uint8_t digest[kDigestSize];
  eip191Digest(message, length, digest);

  Sha256Context context;
  context.uECC.init_hash = &shaInit;
  context.uECC.update_hash = &shaUpdate;
  context.uECC.finish_hash = &shaFinish;
  context.uECC.block_size = SHA256::BLOCK_SIZE;
  context.uECC.result_size = SHA256::HASH_SIZE;
  context.uECC.tmp = context.scratch;

  bool ok = uECC_sign_deterministic(privateKey, digest, sizeof(digest), &context.uECC, signature,
                                    uECC_secp256k1()) == 1;
  context.sha.clear();
  memset(context.scratch, 0, sizeof(context.scratch));
  if (!ok) return false;

  uint8_t* s = signature + 32;
  if (greaterThan(s, kHalfCurveOrder)) subtractFromOrder(s);
  return true;
}

void toHex(const uint8_t* bytes, size_t length, char* out) {
  static const char kDigits[] = "0123456789abcdef";
  *out++ = '0';
  *out++ = 'x';
  for (size_t i = 0; i < length; ++i) {
    *out++ = kDigits[bytes[i] >> 4];
    *out++ = kDigits[bytes[i] & 0x0F];
  }
  *out = '\0';
}

}  // namespace ctn
