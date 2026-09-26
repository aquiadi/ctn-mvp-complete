#include "ctn_protocol.h"

#include <stdio.h>
#include <string.h>

namespace ctn {

const char kMessageVersion[] = "CTN-READING-V2";

size_t formatUint64(uint64_t value, char* out, size_t capacity) {
  char reversed[21];
  size_t length = 0;
  do {
    reversed[length++] = static_cast<char>('0' + value % 10);
    value /= 10;
  } while (value != 0);

  if (length + 1 > capacity) return 0;
  for (size_t i = 0; i < length; ++i) out[i] = reversed[length - 1 - i];
  out[length] = '\0';
  return length;
}

size_t formatKwh(uint64_t wattHours, char* out, size_t capacity) {
  char whole[21];
  if (formatUint64(wattHours / 1000, whole, sizeof(whole)) == 0) return 0;
  // Six decimals: three significant (Wh) and three zeros, matching KWH_DECIMALS.
  int length = snprintf(out, capacity, "%s.%03u000", whole,
                        static_cast<unsigned>(wattHours % 1000));
  return (length > 0 && static_cast<size_t>(length) < capacity) ? static_cast<size_t>(length) : 0;
}

size_t canonicalMessage(char* out, size_t capacity, const char* deviceId, uint32_t sequence,
                        const char* timestamp, uint64_t deltaWh, uint64_t meterWh,
                        uint32_t tamperCount) {
  char kwh[32];
  char meter[21];
  if (formatKwh(deltaWh, kwh, sizeof(kwh)) == 0) return 0;
  if (formatUint64(meterWh, meter, sizeof(meter)) == 0) return 0;

  int length = snprintf(out, capacity,
                        "%s\n"
                        "device:%s\n"
                        "sequence:%lu\n"
                        "timestamp:%s\n"
                        "delta_kwh:%s\n"
                        "meter_wh:%s\n"
                        "tamper_count:%lu",
                        kMessageVersion, deviceId, static_cast<unsigned long>(sequence), timestamp,
                        kwh, meter, static_cast<unsigned long>(tamperCount));
  return (length > 0 && static_cast<size_t>(length) < capacity) ? static_cast<size_t>(length) : 0;
}

void formatTimestamp(int64_t epochSeconds, char out[kTimestampSize]) {
  // Howard Hinnant's civil_from_days: exact for the proleptic Gregorian
  // calendar, no tables, no library calls.
  int64_t days = epochSeconds / 86400;
  int64_t secondsOfDay = epochSeconds % 86400;
  if (secondsOfDay < 0) {
    secondsOfDay += 86400;
    days -= 1;
  }

  days += 719468;
  const int64_t era = (days >= 0 ? days : days - 146096) / 146097;
  const unsigned dayOfEra = static_cast<unsigned>(days - era * 146097);
  const unsigned yearOfEra = (dayOfEra - dayOfEra / 1460 + dayOfEra / 36524 - dayOfEra / 146096) / 365;
  const unsigned dayOfYear = dayOfEra - (365 * yearOfEra + yearOfEra / 4 - yearOfEra / 100);
  const unsigned monthPrime = (5 * dayOfYear + 2) / 153;
  const unsigned day = dayOfYear - (153 * monthPrime + 2) / 5 + 1;
  const unsigned month = monthPrime < 10 ? monthPrime + 3 : monthPrime - 9;
  const int64_t year = static_cast<int64_t>(yearOfEra) + era * 400 + (month <= 2);

  // Years outside 0000-9999 cannot be represented in this format; clamp rather
  // than overflow the buffer. No real clock produces them.
  const int printableYear = year < 0 ? 0 : (year > 9999 ? 9999 : static_cast<int>(year));
  snprintf(out, kTimestampSize, "%04d-%02u-%02uT%02u:%02u:%02uZ", printableYear, month % 100,
           day % 100, static_cast<unsigned>(secondsOfDay / 3600) % 100,
           static_cast<unsigned>(secondsOfDay / 60 % 60), static_cast<unsigned>(secondsOfDay % 60));
}

uint64_t registerAdvance(uint32_t lastRawWh, uint32_t rawWh, bool& reset, uint32_t& baseline) {
  reset = false;
  if (rawWh >= lastRawWh) {
    baseline = rawWh;
    return rawWh - lastRawWh;
  }
  if (lastRawWh - rawWh <= kRegisterJitterWh) {
    baseline = lastRawWh;
    return 0;
  }
  reset = true;
  baseline = rawWh;
  return rawWh;
}

}  // namespace ctn
