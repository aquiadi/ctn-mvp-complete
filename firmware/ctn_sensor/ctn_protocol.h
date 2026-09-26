// The CTN-READING-V2 wire format, and the meter arithmetic behind it.
//
// Portable C++ with no Arduino dependency: firmware/test compiles this file on
// a host and checks its output against the server's canonical_message().
#pragma once

#include <stddef.h>
#include <stdint.h>

namespace ctn {

extern const char kMessageVersion[];  // "CTN-READING-V2"

constexpr size_t kTimestampSize = 21;  // "YYYY-MM-DDTHH:MM:SSZ" + NUL

// The signed text, byte for byte what the server reconstructs:
//
//   CTN-READING-V2
//   device:<id>
//   sequence:<n>
//   timestamp:<YYYY-MM-DDTHH:MM:SSZ>
//   delta_kwh:<kWh, exactly 6 decimals>
//   meter_wh:<lifetime counter>
//   tamper_count:<n>
//
// Energy is carried as integer watt-hours end to end and only rendered as
// kWh here, so there is never a float whose textual form the device and the
// server could disagree on. Returns the length written, or 0 if `capacity`
// is too small.
size_t canonicalMessage(char* out, size_t capacity, const char* deviceId, uint32_t sequence,
                        const char* timestamp, uint64_t deltaWh, uint64_t meterWh,
                        uint32_t tamperCount);

// 1234 Wh -> "1.234000". Returns the length, or 0 if it does not fit.
size_t formatKwh(uint64_t wattHours, char* out, size_t capacity);

// Decimal without relying on printf's %llu, which some embedded C libraries omit.
size_t formatUint64(uint64_t value, char* out, size_t capacity);

// Unix seconds -> "YYYY-MM-DDTHH:MM:SSZ", independent of the C library's
// gmtime and of any timezone configuration.
void formatTimestamp(int64_t epochSeconds, char out[kTimestampSize]);

// The PZEM-004T keeps its own energy register, which a Modbus command can
// clear and which saturates at 9999.99 kWh. The Arduino library reports it as
// a float in kWh, so near the top of its range a conversion back to Wh can
// wobble by a unit. This folds a register reading into the lifetime counter:
//
//   raw >= lastRaw            normal advance
//   lastRaw - raw <= jitter   float conversion noise: no advance, keep lastRaw
//   otherwise                 the register was cleared or wrapped. Only `raw`
//                             is known to be new energy; anything between the
//                             last read and the reset is not claimed.
//
// Returns the watt-hours to add. Sets `reset` when the register went back, and
// `baseline` to the value the next reading should be compared with.
constexpr uint32_t kRegisterJitterWh = 2;

uint64_t registerAdvance(uint32_t lastRawWh, uint32_t rawWh, bool& reset, uint32_t& baseline);

}  // namespace ctn
