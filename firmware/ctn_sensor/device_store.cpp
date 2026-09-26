#include "device_store.h"

#include <Preferences.h>

namespace {
Preferences prefs;
}

namespace store {

void begin() {
  prefs.begin("ctn", false);
}

void load(DeviceState& state) {
  state.hasKey = prefs.getBytesLength("priv") == ctn::kPrivateKeySize &&
                 prefs.getBytesLength("pub") == ctn::kPublicKeySize;
  if (state.hasKey) {
    prefs.getBytes("priv", state.privateKey, ctn::kPrivateKeySize);
    prefs.getBytes("pub", state.publicKey, ctn::kPublicKeySize);
  }
  state.enrolled = prefs.getBool("enrolled", false);

  state.ackedSequence = prefs.getULong("seq", 0);
  state.ackedEpoch = prefs.getLong64("ack_ts", 0);
  state.ackedWh = prefs.getULong64("ack_wh", 0);

  state.totalWh = prefs.getULong64("total_wh", 0);
  state.registerWh = prefs.getULong("reg_wh", 0);
  state.registerInitialised = prefs.getBool("reg_init", false);

  state.tamperCount = prefs.getULong("tamper", 0);
}

void saveKey(const DeviceState& state) {
  prefs.putBytes("priv", state.privateKey, ctn::kPrivateKeySize);
  prefs.putBytes("pub", state.publicKey, ctn::kPublicKeySize);
}

void saveEnrolled(const DeviceState& state) {
  prefs.putBool("enrolled", state.enrolled);
  saveAck(state);
}

void saveMeter(const DeviceState& state) {
  prefs.putULong64("total_wh", state.totalWh);
  prefs.putULong("reg_wh", state.registerWh);
  prefs.putBool("reg_init", state.registerInitialised);
}

void saveAck(const DeviceState& state) {
  prefs.putULong("seq", state.ackedSequence);
  prefs.putLong64("ack_ts", state.ackedEpoch);
  prefs.putULong64("ack_wh", state.ackedWh);
}

void saveTamper(const DeviceState& state) {
  prefs.putULong("tamper", state.tamperCount);
}

}  // namespace store
