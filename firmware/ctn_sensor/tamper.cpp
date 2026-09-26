#include "tamper.h"

#include <Arduino.h>

#include "config.h"

namespace {

volatile bool edgeSeen = false;
volatile uint32_t edgeAtMs = 0;
bool lidOpen = false;

void IRAM_ATTR onEdge() {
  edgeSeen = true;
  edgeAtMs = millis();
}

bool readLidOpen() {
  return digitalRead(kTamperPin) == HIGH;
}

void record(DeviceState& state) {
  state.tamperCount++;
  store::saveTamper(state);
  Serial.printf("[tamper] enclosure opened, count now %lu\n",
                static_cast<unsigned long>(state.tamperCount));
}

}  // namespace

namespace tamper {

void begin(DeviceState& state) {
  pinMode(kTamperPin, INPUT_PULLUP);
  lidOpen = readLidOpen();
  if (lidOpen) record(state);  // opened while powered off, or left open
  attachInterrupt(digitalPinToInterrupt(kTamperPin), onEdge, CHANGE);
}

bool service(DeviceState& state) {
  if (!edgeSeen || millis() - edgeAtMs < kTamperDebounceMs) return false;
  edgeSeen = false;

  const bool open = readLidOpen();
  const bool opened = open && !lidOpen;
  lidOpen = open;
  if (opened) record(state);
  return opened;
}

}  // namespace tamper
