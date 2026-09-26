// UTC time from a DS3231, disciplined by NTP when the network is up.
//
// Every reading is timestamped, and the server rejects timestamps more than
// five minutes ahead of its own clock or not after the previous reading. The
// DS3231 (±2 ppm, battery backed) keeps time through outages and reboots, so
// a device that comes up without network still knows what time it is.
#pragma once

#include <stdint.h>

namespace rtc_clock {

// Returns false if no DS3231 answers on the bus.
bool begin();

// Try to set the RTC from NTP. Call once WiFi is connected.
bool syncFromNtp();

// Current UTC epoch seconds. Returns false if the time cannot be trusted: the
// RTC lost power and NTP has not yet corrected it.
bool now(int64_t& epoch);

}  // namespace rtc_clock
