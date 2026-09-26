// Everything the device must remember across power cycles, in NVS.
//
// The split between "total" and "acked" is what makes reporting loss-free:
// total_wh is every watt-hour the meter has measured since enrolment, and
// acked_wh is how much of it the server has accepted. The next reading always
// claims the difference, so energy measured during an outage is carried by
// the first reading after it rather than dropped.
//
// NVS is not encrypted by default, so the private key can be read out over
// USB by anyone holding the board. Enable flash encryption and secure boot
// before deployment; see firmware/README.md.
#pragma once

#include <stdint.h>

#include "ctn_crypto.h"

struct DeviceState {
  uint8_t privateKey[ctn::kPrivateKeySize];
  uint8_t publicKey[ctn::kPublicKeySize];
  bool hasKey = false;
  bool enrolled = false;

  uint32_t ackedSequence = 0;   // highest sequence the server accepted
  int64_t ackedEpoch = 0;       // timestamp of that reading
  uint64_t ackedWh = 0;         // lifetime counter at that reading

  uint64_t totalWh = 0;         // lifetime energy measured since enrolment
  uint32_t registerWh = 0;      // last raw PZEM register value folded in
  bool registerInitialised = false;

  uint32_t tamperCount = 0;     // enclosure-open events, only ever increases
};

namespace store {

void begin();
void load(DeviceState& state);

void saveKey(const DeviceState& state);
void saveEnrolled(const DeviceState& state);
void saveMeter(const DeviceState& state);
void saveAck(const DeviceState& state);
void saveTamper(const DeviceState& state);

}  // namespace store
