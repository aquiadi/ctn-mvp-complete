// Per-device settings. Edit before flashing.
#pragma once

#include <stdint.h>

// ── Network ────────────────────────────────────────────────────────────────

#define CTN_WIFI_SSID     "your-wifi"
#define CTN_WIFI_PASSWORD "your-wifi-password"

// Scheme and host only, no trailing slash. Must chain to a root in root_ca.h.
#define CTN_API_HOST "https://ctn-api-railway-production.up.railway.app"

// Uncomment only on the bench, never on a deployed device. Without certificate
// validation anyone on the network path can impersonate the API.
// #define CTN_ALLOW_INSECURE_TLS

// ── Identity ───────────────────────────────────────────────────────────────

// Unique across the platform: letters, digits, dot, dash, underscore.
#define CTN_DEVICE_ID "ROOF-01"

// From the seller dashboard: Add a device → pairing code. Single use, one hour.
// Declare the system's rated capacity and site coordinates when generating
// it; the server bounds every reading by that capacity.
#define CTN_ENROLLMENT_CODE "CTN-XXXXXXXX-XXXXXXXX"

// ── Reporting ──────────────────────────────────────────────────────────────

// One signed reading per interval. Energy measured while offline is not lost:
// the next accepted reading covers everything since the last acknowledged one.
static const uint32_t kReportIntervalMs = 15UL * 60UL * 1000UL;

// If nothing has been accepted for this long, reboot. Recovers from a wedged
// WiFi or TLS stack without a site visit.
static const uint32_t kNoProgressRebootMs = 6UL * 60UL * 60UL * 1000UL;

static const uint32_t kHttpTimeoutMs = 15000;

// ── Wiring (ESP32 DevKit) ──────────────────────────────────────────────────

// PZEM-004T v3.0 on UART2. Its TX goes to the ESP32's RX pin and vice versa.
// The PZEM's TTL side is 5 V; use a level shifter or the 3.3 V-modified board.
static const uint8_t kPzemRxPin = 16;
static const uint8_t kPzemTxPin = 17;

// DS3231 on the default I2C bus.
static const uint8_t kI2cSdaPin = 21;
static const uint8_t kI2cSclPin = 22;

// Normally-closed reed switch between this pin and GND, magnet in the lid.
// Lid closed: switch closed, pin LOW. Lid opened: pin pulled HIGH.
static const uint8_t kTamperPin = 27;
static const uint32_t kTamperDebounceMs = 50;

// ── Time ───────────────────────────────────────────────────────────────────

#define CTN_NTP_PRIMARY   "pool.ntp.org"
#define CTN_NTP_SECONDARY "time.google.com"

// Re-discipline the RTC from NTP this often while online.
static const uint32_t kNtpResyncMs = 6UL * 60UL * 60UL * 1000UL;
