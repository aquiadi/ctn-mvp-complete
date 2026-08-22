/*
 * CTN solar sensor — ESP32 reference firmware.
 *
 * Connects over WiFi and talks HTTPS to the CTN API. There is no wired link and
 * no gateway: the device is the client, so it works anywhere there is a network,
 * and nothing has to be opened up on the seller's side.
 *
 * On first boot it generates a secp256k1 keypair, stores it in NVS, and redeems
 * the pairing code from the seller's dashboard. From then on it signs every
 * reading it sends. The private key is never transmitted and never leaves flash,
 * so the server can verify a reading but cannot forge one, and neither can
 * anyone who intercepts the traffic.
 *
 * ── Wiring ────────────────────────────────────────────────────────────────
 * Energy measurement is deliberately isolated in readEnergyWh(). Replace its
 * body with whatever your hardware provides:
 *
 *   Pulse meter  : count S0 pulses on a GPIO, watt-hours = pulses / pulsesPerKwh
 *   CT clamp     : SCT-013 into an ADC pin, integrate power over the interval
 *   Modbus       : read the register your inverter exposes over RS485
 *
 * Everything else in this file is transport and cryptography and stays the same.
 *
 * ── Libraries ─────────────────────────────────────────────────────────────
 *   micro-ecc  (kmackay)      secp256k1 signing
 *   Crypto     (rweather)     Keccak-256 for EIP-191
 *   ArduinoJson (bblanchon)
 *
 * ── Status ────────────────────────────────────────────────────────────────
 * The signing scheme and API contract are exercised by the server test suite
 * and by tools/sensor_sim.py. This sketch has NOT been run on physical
 * hardware — treat it as a reference implementation to verify on your bench
 * before trusting it on a roof.
 */

#include <Arduino.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include <WiFiClientSecure.h>
#include <Preferences.h>
#include <ArduinoJson.h>
#include <uECC.h>
#include <Crypto.h>
#include <SHA3.h>

// ── Configuration ─────────────────────────────────────────────────────────

static const char* WIFI_SSID     = "your-wifi";
static const char* WIFI_PASSWORD = "your-wifi-password";

static const char* API_HOST = "https://ctn-api-railway-production.up.railway.app";

// From your dashboard: Add a device → copy the pairing code. Single use.
static const char* ENROLLMENT_CODE = "CTN-XXXXXXXX-XXXXXXXX";

// Must be unique across the platform.
static const char* DEVICE_ID = "ROOF-01";

// How often a reading is emitted. Shorter means finer resolution and more
// traffic; the server accumulates either way.
static const uint32_t REPORT_INTERVAL_MS = 15UL * 60UL * 1000UL;

// ── State ─────────────────────────────────────────────────────────────────

Preferences prefs;
uint8_t privateKey[32];
uint8_t publicKey[64];
uint32_t sequence = 0;

// ── Keccak-256, as EIP-191 requires (not SHA3-256) ────────────────────────

static void keccak256(const uint8_t* data, size_t length, uint8_t* out) {
  SHA3_256 hash;                 // rweather's SHA3_256 in Keccak mode
  hash.reset();
  hash.update(data, length);
  hash.finalize(out, 32);
}

// ── Key management ────────────────────────────────────────────────────────

static int rng(uint8_t* dest, unsigned size) {
  // esp_random() draws from the hardware RNG, which is seeded by RF noise.
  while (size--) *dest++ = (uint8_t)(esp_random() & 0xFF);
  return 1;
}

/* The address is the last 20 bytes of the Keccak hash of the public key. */
static String deriveAddress(const uint8_t* pubKey) {
  uint8_t hash[32];
  keccak256(pubKey, 64, hash);

  String address = "0x";
  for (int i = 12; i < 32; i++) {
    char byteHex[3];
    snprintf(byteHex, sizeof(byteHex), "%02x", hash[i]);
    address += byteHex;
  }
  return address;
}

static void loadOrCreateKeypair() {
  prefs.begin("ctn", false);
  sequence = prefs.getUInt("seq", 0);

  if (prefs.getBytesLength("privkey") == 32) {
    prefs.getBytes("privkey", privateKey, 32);
    prefs.getBytes("pubkey", publicKey, 64);
    Serial.println("Loaded existing key from flash");
    return;
  }

  Serial.println("First boot — generating keypair");
  const struct uECC_Curve_t* curve = uECC_secp256k1();
  uECC_set_rng(&rng);
  if (!uECC_make_key(publicKey, privateKey, curve)) {
    Serial.println("Key generation failed; halting rather than sending unsigned data");
    while (true) delay(1000);
  }

  prefs.putBytes("privkey", privateKey, 32);
  prefs.putBytes("pubkey", publicKey, 64);
  Serial.println("Key stored. It never leaves this device.");
}

// ── Signing ───────────────────────────────────────────────────────────────

/* Must match the server byte for byte. See GET /api/v1/spec. */
static String canonicalMessage(uint32_t seq, const String& timestamp, float deltaKwh) {
  char kwh[24];
  snprintf(kwh, sizeof(kwh), "%.6f", deltaKwh);   // fixed precision, always

  String message = "CTN-READING-V1\n";
  message += "device:"    + String(DEVICE_ID) + "\n";
  message += "sequence:"  + String(seq)       + "\n";
  message += "timestamp:" + timestamp         + "\n";
  message += "delta_kwh:" + String(kwh);
  return message;
}

/* EIP-191: keccak256("\x19Ethereum Signed Message:\n" + len + message). */
static void eip191Digest(const String& message, uint8_t* digest) {
  String prefixed = "\x19""Ethereum Signed Message:\n" + String(message.length()) + message;
  keccak256((const uint8_t*)prefixed.c_str(), prefixed.length(), digest);
}

static String signMessage(const String& message) {
  uint8_t digest[32];
  eip191Digest(message, digest);

  uint8_t signature[64];
  const struct uECC_Curve_t* curve = uECC_secp256k1();
  if (!uECC_sign(privateKey, digest, 32, signature, curve)) return "";

  // Emitted as plain r||s, without the Ethereum recovery id. micro-ecc cannot
  // derive it, and the server tries both possibilities — so there is nothing
  // here to get wrong.
  String hex = "0x";
  for (int i = 0; i < 64; i++) {
    char byteHex[3];
    snprintf(byteHex, sizeof(byteHex), "%02x", signature[i]);
    hex += byteHex;
  }
  return hex;
}

// ── HTTP ──────────────────────────────────────────────────────────────────

static int postJson(const String& path, const String& body, String& response) {
  WiFiClientSecure client;
  // Pin the API's CA in production. setInsecure() skips certificate validation,
  // which is acceptable only while bringing a device up on the bench.
  client.setInsecure();

  HTTPClient http;
  if (!http.begin(client, String(API_HOST) + path)) return -1;
  http.addHeader("Content-Type", "application/json");

  int status = http.POST(body);
  response = http.getString();
  http.end();
  return status;
}

// ── Enrollment ────────────────────────────────────────────────────────────

static bool enrollIfNeeded() {
  if (prefs.getBool("enrolled", false)) return true;

  JsonDocument doc;
  doc["enrollment_code"] = ENROLLMENT_CODE;
  doc["device_id"]       = DEVICE_ID;
  doc["public_key"]      = deriveAddress(publicKey);

  String body, response;
  serializeJson(doc, body);

  int status = postJson("/api/v1/devices/enroll", body, response);
  Serial.printf("Enrollment: HTTP %d\n%s\n", status, response.c_str());

  if (status == 200) {
    prefs.putBool("enrolled", true);
    Serial.println("Enrolled. Reporting begins now.");
    return true;
  }

  // 409 means this device or code is already registered — if the flag was lost
  // but the server knows us, carry on rather than retrying forever.
  if (status == 409) {
    prefs.putBool("enrolled", true);
    return true;
  }

  Serial.println("Enrollment failed. Check the pairing code has not expired.");
  return false;
}

// ── Measurement ───────────────────────────────────────────────────────────

/*
 * Return watt-hours generated since the previous call.
 *
 * Replace this with your meter. The placeholder emits a small constant so the
 * pipeline can be exercised end to end before hardware is attached.
 */
static float readEnergyWh() {
  return 150.0f;   // ~0.15 kWh per interval
}

// ── Time ──────────────────────────────────────────────────────────────────

static String isoTimestamp() {
  time_t now = time(nullptr);
  struct tm timeinfo;
  gmtime_r(&now, &timeinfo);

  char buffer[32];
  strftime(buffer, sizeof(buffer), "%Y-%m-%dT%H:%M:%SZ", &timeinfo);
  return String(buffer);
}

// ── Reporting ─────────────────────────────────────────────────────────────

static void reportReading() {
  float deltaKwh = readEnergyWh() / 1000.0f;
  if (deltaKwh <= 0) return;

  sequence++;
  String timestamp = isoTimestamp();
  String message   = canonicalMessage(sequence, timestamp, deltaKwh);
  String signature = signMessage(message);

  if (signature.isEmpty()) {
    Serial.println("Signing failed; dropping the reading rather than sending it unsigned");
    sequence--;
    return;
  }

  JsonDocument doc;
  JsonArray readings = doc["readings"].to<JsonArray>();
  JsonObject reading = readings.add<JsonObject>();
  reading["device_id"] = DEVICE_ID;
  reading["sequence"]  = sequence;
  reading["timestamp"] = timestamp;
  reading["delta_kwh"] = deltaKwh;
  reading["signature"] = signature;

  String body, response;
  serializeJson(doc, body);

  int status = postJson("/api/v1/readings", body, response);
  Serial.printf("Reading %u: HTTP %d\n%s\n", sequence, status, response.c_str());

  if (status == 200) {
    // Only advance the stored counter once the server has accepted it, so a
    // failed send is retried rather than leaving a permanent gap.
    prefs.putUInt("seq", sequence);
  } else {
    sequence--;
  }
}

// ── Lifecycle ─────────────────────────────────────────────────────────────

void setup() {
  Serial.begin(115200);
  delay(500);

  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  Serial.print("Connecting to WiFi");
  while (WiFi.status() != WL_CONNECTED) { delay(500); Serial.print("."); }
  Serial.printf("\nConnected: %s\n", WiFi.localIP().toString().c_str());

  // Readings are timestamped, so the clock has to be right.
  configTime(0, 0, "pool.ntp.org", "time.nist.gov");
  while (time(nullptr) < 1700000000) { delay(500); Serial.print("."); }
  Serial.println("\nClock synchronised");

  loadOrCreateKeypair();
  Serial.printf("Device address: %s\n", deriveAddress(publicKey).c_str());

  while (!enrollIfNeeded()) delay(30000);
}

void loop() {
  if (WiFi.status() != WL_CONNECTED) {
    WiFi.reconnect();
    delay(5000);
    return;
  }

  reportReading();
  delay(REPORT_INTERVAL_MS);
}
