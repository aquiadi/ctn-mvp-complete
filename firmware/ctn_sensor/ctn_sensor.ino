/*
 * CTN solar sensor — ESP32 firmware.
 *
 * Measures AC energy with a PZEM-004T, timestamps with a DS3231, counts
 * enclosure openings with a reed switch, and reports signed CTN-READING-V2
 * packets to the CTN API over HTTPS.
 *
 *   ctn_crypto     secp256k1 keys, Keccak-256, EIP-191 signing   (portable)
 *   ctn_protocol   canonical message, timestamps, meter maths    (portable)
 *   meter          PZEM-004T register → lifetime Wh counter
 *   rtc_clock      DS3231 disciplined by NTP
 *   tamper         reed switch → monotonic tamper counter
 *   device_store   NVS persistence
 *   api_client     HTTPS with certificate validation
 *
 * The portable modules are compiled on a host in CI and checked byte for byte
 * against the server's verifier (firmware/test). The hardware modules have
 * not yet been run on a physical board; see firmware/README.md for the bench
 * bring-up checklist before trusting a device on a roof.
 *
 * Libraries: micro-ecc (kmackay), Crypto (rweather), ArduinoJson 7 (bblanchon),
 * PZEM004Tv30 (mandulaj), RTClib (Adafruit).
 */

// meter_wh is a 64-bit counter; make sure ArduinoJson carries it exactly.
#define ARDUINOJSON_USE_LONG_LONG 1

#include <Arduino.h>
#include <ArduinoJson.h>
#include <WiFi.h>
#include <esp_system.h>
#include <uECC.h>

#include "api_client.h"
#include "config.h"
#include "ctn_crypto.h"
#include "ctn_protocol.h"
#include "device_store.h"
#include "meter.h"
#include "rtc_clock.h"
#include "tamper.h"

namespace {

DeviceState state;
char address[ctn::kAddressHexSize];

uint32_t lastReportAttemptMs = 0;
uint32_t lastProgressMs = 0;
uint32_t lastNtpSyncMs = 0;
bool halted = false;  // set when only a person can fix the problem

// esp_random() is a true RNG only while the radio is on, so keys are generated
// after WiFi connects. Signing does not depend on it (see ctn_crypto.h); it is
// used there only to blind the scalar multiplication.
int hardwareRng(uint8_t* dest, unsigned size) {
  esp_fill_random(dest, size);
  return 1;
}

void halt(const char* reason) {
  halted = true;
  Serial.printf("[halt] %s\n", reason);
}

// ── Connectivity ────────────────────────────────────────────────────────────

bool ensureWifi() {
  if (WiFi.status() == WL_CONNECTED) return true;

  WiFi.mode(WIFI_STA);
  WiFi.begin(CTN_WIFI_SSID, CTN_WIFI_PASSWORD);
  const uint32_t started = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - started < 30000) delay(250);

  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("[wifi] not connected; will retry");
    return false;
  }
  Serial.printf("[wifi] %s\n", WiFi.localIP().toString().c_str());
  return true;
}

// ── Identity and enrolment ──────────────────────────────────────────────────

void ensureKey() {
  if (state.hasKey) return;
  Serial.println("[key] first boot: generating keypair");
  if (!ctn::generateKeypair(state.privateKey, state.publicKey)) {
    halt("key generation failed");
    return;
  }
  state.hasKey = true;
  store::saveKey(state);
}

bool enrol() {
  if (state.enrolled) return true;

  JsonDocument doc;
  doc["enrollment_code"] = CTN_ENROLLMENT_CODE;
  doc["device_id"] = CTN_DEVICE_ID;
  doc["public_key"] = address;
  String body, response;
  serializeJson(doc, body);

  const int status = api::post("/api/v1/devices/enroll", body, response);
  Serial.printf("[enrol] HTTP %d %s\n", status, response.c_str());

  if (status == 200) {
    // Energy counted before enrolment was never attested; the first reading
    // claims only what is measured from here on.
    state.enrolled = true;
    state.ackedSequence = 0;
    state.ackedEpoch = 0;
    state.ackedWh = state.totalWh;
    store::saveEnrolled(state);
    return true;
  }
  if (status == 404 || status == 409 || status == 410) {
    halt("pairing code rejected (unknown, used, or expired); reflash with a new one");
  }
  return false;
}

// ── Resynchronisation ───────────────────────────────────────────────────────

// Adopt the server's committed position after an ambiguous failure: a lost
// response to an accepted reading, or a counter mismatch. Energy the server
// already has is not claimed again; energy it lacks is still carried.
bool resync() {
  String path = String("/api/v1/devices/") + CTN_DEVICE_ID;
  String response;
  const int status = api::get(path.c_str(), response);
  if (status != 200) {
    Serial.printf("[resync] HTTP %d\n", status);
    return false;
  }

  JsonDocument doc;
  if (deserializeJson(doc, response)) return false;

  const char* expected = doc["public_key"] | "";
  if (strcasecmp(expected, address) != 0) {
    halt("server holds a different key for this device id; re-enrol");
    return false;
  }

  state.ackedSequence = doc["last_sequence"] | 0UL;
  if (!doc["last_meter_wh"].isNull()) {
    const uint64_t serverWh = doc["last_meter_wh"].as<uint64_t>();
    if (serverWh > state.totalWh) {
      halt("server's meter counter is ahead of this device (storage lost?); re-enrol");
      return false;
    }
    state.ackedWh = serverWh;
  }
  const uint32_t serverTamper = doc["tamper_count"] | 0UL;
  if (serverTamper > state.tamperCount) {
    state.tamperCount = serverTamper;
    store::saveTamper(state);
  }
  store::saveAck(state);
  Serial.printf("[resync] sequence %lu, acked %llu Wh\n",
                static_cast<unsigned long>(state.ackedSequence),
                static_cast<unsigned long long>(state.ackedWh));
  return true;
}

// ── Reporting ───────────────────────────────────────────────────────────────

void report() {
  bool reset = false;
  if (!meter::poll(state, reset)) {
    Serial.println("[meter] PZEM did not answer; skipping this interval");
    return;
  }
  if (reset) Serial.println("[meter] PZEM register went backwards; energy before the reset is not claimed");
  store::saveMeter(state);

  int64_t epoch;
  if (!rtc_clock::now(epoch)) {
    Serial.println("[clock] time not trusted yet; holding the reading");
    return;
  }
  if (epoch <= state.ackedEpoch) {
    // Never sign a timestamp the server would reject as out of order.
    Serial.println("[clock] clock is behind the last accepted reading; holding");
    return;
  }

  const uint32_t sequence = state.ackedSequence + 1;
  const uint64_t deltaWh = state.totalWh - state.ackedWh;

  char timestamp[ctn::kTimestampSize];
  ctn::formatTimestamp(epoch, timestamp);

  char message[384];
  const size_t length = ctn::canonicalMessage(message, sizeof(message), CTN_DEVICE_ID, sequence,
                                              timestamp, deltaWh, state.totalWh, state.tamperCount);
  uint8_t signature[ctn::kSignatureSize];
  if (length == 0 || !ctn::signMessage(state.privateKey, message, length, signature)) {
    halt("could not build or sign a reading");
    return;
  }
  char signatureHex[2 + 2 * ctn::kSignatureSize + 1];
  ctn::toHex(signature, sizeof(signature), signatureHex);

  // delta_kwh is written as the exact decimal text that was signed, so no
  // float round trip can make the two disagree.
  char kwh[32];
  ctn::formatKwh(deltaWh, kwh, sizeof(kwh));

  JsonDocument doc;
  JsonObject reading = doc["readings"].to<JsonArray>().add<JsonObject>();
  reading["device_id"] = CTN_DEVICE_ID;
  reading["sequence"] = sequence;
  reading["timestamp"] = timestamp;
  reading["delta_kwh"] = serialized(kwh);
  reading["message_version"] = ctn::kMessageVersion;
  reading["meter_wh"] = state.totalWh;
  reading["tamper_count"] = state.tamperCount;
  reading["signature"] = signatureHex;

  String body, response;
  serializeJson(doc, body);
  const int status = api::post("/api/v1/readings", body, response);
  Serial.printf("[report] seq %lu, %s kWh: HTTP %d %s\n", static_cast<unsigned long>(sequence), kwh,
                status, response.c_str());

  switch (status) {
    case 200:
      state.ackedSequence = sequence;
      state.ackedEpoch = epoch;
      state.ackedWh = state.totalWh;
      store::saveAck(state);
      lastProgressMs = millis();
      break;

    case 409:  // replay: an earlier send was accepted but its response was lost
      resync();
      break;

    case 422: {
      JsonDocument error;
      deserializeJson(error, response);
      const char* code = error["detail"]["code"] | "";
      if (strcmp(code, "meter_discontinuity") == 0) {
        resync();
      } else if (strcmp(code, "exceeds_capacity") == 0) {
        // Keep carrying the energy: over a longer interval the bound grows.
        // A genuine meter fault never clears and needs a person.
        Serial.println("[report] above rated capacity; holding energy for the next interval");
      } else if (strcmp(code, "meter_regressed") == 0 || strcmp(code, "tamper_counter_regressed") == 0) {
        halt("server rejected a counter as regressed; re-enrol");
      }
      break;
    }

    case 401:
      halt("server does not recognise this device's signature; re-enrol");
      break;

    case 404:
      halt("device is not registered on the server");
      break;

    default:  // network errors and 5xx: energy stays unacknowledged and is retried
      break;
  }
}

}  // namespace

void setup() {
  Serial.begin(115200);
  delay(300);
  Serial.printf("\nCTN sensor %s\n", CTN_DEVICE_ID);

  store::begin();
  store::load(state);
  rtc_clock::begin();
  meter::begin();
  tamper::begin(state);
  uECC_set_rng(&hardwareRng);

  // The radio has to be on before key generation for esp_random to be a TRNG.
  while (!ensureWifi()) delay(5000);
  rtc_clock::syncFromNtp();
  lastNtpSyncMs = millis();

  ensureKey();
  if (!halted) {
    ctn::addressHex(state.publicKey, address);
    Serial.printf("[key] device address %s\n", address);
  }

  lastProgressMs = millis();
  // Report on the first loop iteration rather than a full interval later.
  lastReportAttemptMs = millis() - kReportIntervalMs;
}

void loop() {
  tamper::service(state);

  if (halted) {
    delay(1000);
    return;
  }

  const uint32_t now = millis();
  if (now - lastReportAttemptMs >= kReportIntervalMs) {
    lastReportAttemptMs = now;
    if (ensureWifi()) {
      if (now - lastNtpSyncMs >= kNtpResyncMs && rtc_clock::syncFromNtp()) lastNtpSyncMs = now;
      if (enrol()) report();
    } else {
      // Still fold the meter so a reset during the outage is noticed promptly.
      bool reset = false;
      if (meter::poll(state, reset)) store::saveMeter(state);
    }
  }

  if (millis() - lastProgressMs >= kNoProgressRebootMs) {
    Serial.println("[watchdog] no accepted reading for too long; restarting");
    delay(100);
    ESP.restart();
  }

  delay(50);
}
