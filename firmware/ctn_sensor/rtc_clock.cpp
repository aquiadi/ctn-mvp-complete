#include "rtc_clock.h"

#include <time.h>

#include <Arduino.h>
#include <RTClib.h>
#include <Wire.h>

#include "config.h"

namespace {

RTC_DS3231 rtc;
bool present = false;
bool trusted = false;

// Anything earlier than this is an unset clock, not a real time.
constexpr int64_t kEarliestPlausibleEpoch = 1735689600;  // 2025-01-01

}  // namespace

namespace rtc_clock {

bool begin() {
  Wire.begin(kI2cSdaPin, kI2cSclPin);
  present = rtc.begin(&Wire);
  if (!present) {
    Serial.println("[clock] no DS3231 on I2C; falling back to NTP only");
    return false;
  }
  // A DS3231 that lost power restarts from an arbitrary time and says so.
  trusted = !rtc.lostPower() && rtc.now().unixtime() >= kEarliestPlausibleEpoch;
  Serial.printf("[clock] DS3231 %s\n", trusted ? "holds valid time" : "lost power; waiting for NTP");
  return true;
}

bool syncFromNtp() {
  configTime(0, 0, CTN_NTP_PRIMARY, CTN_NTP_SECONDARY);

  const uint32_t started = millis();
  time_t ntp = 0;
  while (millis() - started < 20000) {
    ntp = time(nullptr);
    if (ntp >= kEarliestPlausibleEpoch) break;
    delay(250);
  }
  if (ntp < kEarliestPlausibleEpoch) {
    Serial.println("[clock] NTP did not answer");
    return false;
  }

  if (present) {
    const int64_t drift = static_cast<int64_t>(rtc.now().unixtime()) - ntp;
    if (!trusted || drift > 1 || drift < -1) {
      rtc.adjust(DateTime(static_cast<uint32_t>(ntp)));
      Serial.printf("[clock] RTC set from NTP (drift was %lld s)\n", static_cast<long long>(drift));
    }
  }
  trusted = true;
  return true;
}

bool now(int64_t& epoch) {
  if (!trusted) return false;
  epoch = present ? static_cast<int64_t>(rtc.now().unixtime()) : static_cast<int64_t>(time(nullptr));
  return epoch >= kEarliestPlausibleEpoch;
}

}  // namespace rtc_clock
