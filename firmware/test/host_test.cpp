// Host harness for the firmware's portable modules.
//
// Reads one case per line on stdin:
//   <private key hex> <device id> <sequence> <epoch> <delta Wh> <meter Wh> <tamper>
// and writes one JSON object per line with what the firmware would produce:
// its address, timestamp, canonical message, and signature. verify.py checks
// every field against the server's own attestation code.
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "ctn_crypto.h"
#include "ctn_protocol.h"

static bool parseHex(const char* hex, uint8_t* out, size_t length) {
  if (strncmp(hex, "0x", 2) == 0) hex += 2;
  if (strlen(hex) != length * 2) return false;
  for (size_t i = 0; i < length; ++i) {
    unsigned byte;
    if (sscanf(hex + 2 * i, "%2x", &byte) != 1) return false;
    out[i] = static_cast<uint8_t>(byte);
  }
  return true;
}

static void jsonString(const char* text) {
  putchar('"');
  for (; *text; ++text) {
    if (*text == '\n') fputs("\\n", stdout);
    else if (*text == '"' || *text == '\\') { putchar('\\'); putchar(*text); }
    else putchar(*text);
  }
  putchar('"');
}

int main() {
  // Known-answer checks for the hash itself, before anything depends on it.
  uint8_t digest[32];
  char hex[2 + 64 + 1];
  ctn::keccak256(reinterpret_cast<const uint8_t*>(""), 0, digest);
  ctn::toHex(digest, 32, hex);
  printf("{\"keccak256_empty\": \"%s\"}\n", hex);
  ctn::keccak256(reinterpret_cast<const uint8_t*>("abc"), 3, digest);
  ctn::toHex(digest, 32, hex);
  printf("{\"keccak256_abc\": \"%s\"}\n", hex);

  // Register arithmetic: normal advance, float jitter, and a cleared register.
  const unsigned cases[][2] = {{1000, 1250}, {9999990, 9999989}, {9999990, 40}};
  for (const auto& c : cases) {
    bool reset = false;
    uint32_t baseline = 0;
    unsigned long long added = ctn::registerAdvance(c[0], c[1], reset, baseline);
    printf("{\"advance\": [%u, %u], \"added\": %llu, \"reset\": %s, \"baseline\": %u}\n",
           c[0], c[1], added, reset ? "true" : "false", baseline);
  }

  char line[512];
  while (fgets(line, sizeof(line), stdin)) {
    char keyHex[80], deviceId[80];
    unsigned long sequence, tamper;
    long long epoch;
    unsigned long long deltaWh, meterWh;
    if (sscanf(line, "%79s %79s %lu %lld %llu %llu %lu", keyHex, deviceId, &sequence, &epoch,
               &deltaWh, &meterWh, &tamper) != 7) {
      fprintf(stderr, "bad case: %s", line);
      return 2;
    }

    uint8_t privateKey[ctn::kPrivateKeySize], publicKey[ctn::kPublicKeySize];
    if (!parseHex(keyHex, privateKey, sizeof(privateKey)) ||
        !ctn::publicKeyFromPrivate(privateKey, publicKey)) {
      fprintf(stderr, "bad key: %s\n", keyHex);
      return 2;
    }

    char address[ctn::kAddressHexSize];
    ctn::addressHex(publicKey, address);

    char timestamp[ctn::kTimestampSize];
    ctn::formatTimestamp(epoch, timestamp);

    char message[384];
    size_t length = ctn::canonicalMessage(message, sizeof(message), deviceId, sequence, timestamp,
                                          deltaWh, meterWh, tamper);
    if (length == 0) {
      fprintf(stderr, "message did not fit\n");
      return 2;
    }

    uint8_t signature[ctn::kSignatureSize];
    if (!ctn::signMessage(privateKey, message, length, signature)) {
      fprintf(stderr, "signing failed\n");
      return 2;
    }
    char signatureHex[2 + 128 + 1];
    ctn::toHex(signature, sizeof(signature), signatureHex);

    printf("{\"address\": \"%s\", \"timestamp\": \"%s\", \"message\": ", address, timestamp);
    jsonString(message);
    printf(", \"signature\": \"%s\"}\n", signatureHex);
  }
  return 0;
}
