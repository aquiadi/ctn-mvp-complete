#!/usr/bin/env bash
# Build the firmware's portable modules for the host and verify them against
# the server's attestation code. Library sources are fetched at pinned
# revisions, the same ones the sketch is built against.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKETCH="$HERE/../ctn_sensor"
DEPS="${CTN_FIRMWARE_DEPS:-$HERE/.deps}"
BUILD="$HERE/.build"
PYTHON="${PYTHON:-python3}"

MICRO_ECC_REV=24c60e243580c7868f4334a1ba3123481fe1aa48   # v1.1
ARDUINOLIBS_REV=37a76b8f7516568e1c575b6dc9268da1ccaac6b6 # rweather Crypto

fetch() {  # fetch <dir> <url> <rev>
  if [ ! -d "$DEPS/$1/.git" ]; then
    git init -q "$DEPS/$1"
    git -C "$DEPS/$1" remote add origin "$2"
  fi
  if [ "$(git -C "$DEPS/$1" rev-parse -q --verify HEAD || true)" != "$3" ]; then
    git -C "$DEPS/$1" fetch -q --depth 1 origin "$3"
    git -C "$DEPS/$1" checkout -q FETCH_HEAD
  fi
}

mkdir -p "$DEPS" "$BUILD"
fetch micro-ecc https://github.com/kmackay/micro-ecc "$MICRO_ECC_REV"
fetch arduinolibs https://github.com/rweather/arduinolibs "$ARDUINOLIBS_REV"

CRYPTO="$DEPS/arduinolibs/libraries/Crypto"
cc -O2 -c "$DEPS/micro-ecc/uECC.c" -I"$DEPS/micro-ecc" -o "$BUILD/uECC.o"
c++ -std=c++17 -O2 -Wall -Wextra -Werror=return-type \
  -I"$SKETCH" -I"$CRYPTO" -I"$DEPS/micro-ecc" \
  "$HERE/host_test.cpp" "$SKETCH/ctn_crypto.cpp" "$SKETCH/ctn_protocol.cpp" \
  "$CRYPTO/KeccakCore.cpp" "$CRYPTO/SHA256.cpp" "$CRYPTO/Hash.cpp" "$CRYPTO/Crypto.cpp" \
  "$BUILD/uECC.o" -o "$BUILD/host_test"

"$PYTHON" "$HERE/verify.py" "$BUILD/host_test"
