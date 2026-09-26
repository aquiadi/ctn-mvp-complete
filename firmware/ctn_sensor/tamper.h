// Enclosure tamper detection with a reed switch.
//
// Every opening of the lid increments a counter persisted in NVS. The counter
// is signed into every reading, and the server withdraws the installation's
// confirmation when it sees it advance, so opening the box cannot go
// unnoticed even if the device is offline at the time.
//
// Limits: this detects the lid, not a drilled enclosure or a bypassed CT
// clamp, and it only sees openings while powered. A lid found open at boot
// is counted, which covers the common case of tampering with the power off.
#pragma once

#include "device_store.h"

namespace tamper {

void begin(DeviceState& state);

// Count any debounced openings since the last call. Returns true if the
// counter advanced.
bool service(DeviceState& state);

}  // namespace tamper
