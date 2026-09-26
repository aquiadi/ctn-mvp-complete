// PZEM-004T v3.0 energy measurement.
//
// Reads the module's cumulative energy register over Modbus and folds each
// reading into the device's own lifetime counter. The PZEM's register can be
// cleared by a Modbus command and saturates at 9999.99 kWh; the lifetime
// counter is 64-bit and only ever increases, which is what the server checks.
//
// The PZEM measures AC on the inverter output and must be wired to mains by a
// qualified electrician. It is not a revenue-grade meter: accuracy is about
// ±0.5 % and it carries no calibration certificate.
#pragma once

#include "device_store.h"

namespace meter {

void begin();

// Fold the current register value into state.totalWh. Returns false if the
// module did not answer, in which case nothing changes. `reset` reports that
// the register went backwards (cleared or wrapped).
bool poll(DeviceState& state, bool& reset);

// Instantaneous AC power in watts, or a negative value if unavailable.
float powerWatts();

}  // namespace meter
