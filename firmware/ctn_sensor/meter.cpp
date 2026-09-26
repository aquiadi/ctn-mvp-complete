#include "meter.h"

#include <math.h>

#include <Arduino.h>
#include <PZEM004Tv30.h>

#include "config.h"
#include "ctn_protocol.h"

namespace {
PZEM004Tv30 pzem(Serial2, kPzemRxPin, kPzemTxPin);
}

namespace meter {

void begin() {
  // The library opens Serial2 at 9600 8N1 itself.
}

bool poll(DeviceState& state, bool& reset) {
  reset = false;
  const float kwh = pzem.energy();
  if (isnan(kwh) || kwh < 0) return false;

  const uint32_t raw = static_cast<uint32_t>(lroundf(kwh * 1000.0f));

  if (!state.registerInitialised) {
    // First contact with this module: whatever is already on its register was
    // generated before this device was watching, so it becomes the baseline.
    state.registerWh = raw;
    state.registerInitialised = true;
    return true;
  }

  uint32_t baseline = state.registerWh;
  state.totalWh += ctn::registerAdvance(state.registerWh, raw, reset, baseline);
  state.registerWh = baseline;
  return true;
}

float powerWatts() {
  const float watts = pzem.power();
  return isnan(watts) ? -1.0f : watts;
}

}  // namespace meter
