"""
A simulated sensor for the test suite.

Stands in for firmware: holds a private key, counts its own sequence, keeps a
lifetime energy counter, and signs every reading it emits. The private key
never reaches the server.

Timestamps come from the real clock and are forced strictly forward, because
the server rejects readings dated before the device was registered, in the
future, or out of order — exactly as it would for hardware.
"""

import time
from datetime import datetime, timezone

import attestation


def iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class SimulatedSensor:
    def __init__(self, device_id: str, version: str = attestation.MESSAGE_VERSION_V1):
        self.device_id = device_id
        self.version = version
        self.private_key, self.public_key = attestation.generate_device_keypair()
        self.sequence = 0
        self.meter_wh = 0
        self.tamper_count = 0
        self._last_epoch = 0

    def next_timestamp(self) -> str:
        self._last_epoch = max(int(time.time()), self._last_epoch + 1)
        return iso(self._last_epoch)

    def reading(
        self,
        delta_kwh: float,
        timestamp: str = None,
        sequence: int = None,
        meter_wh: int = None,
        tamper_count: int = None,
    ) -> dict:
        if sequence is None:
            self.sequence += 1
            sequence = self.sequence
        timestamp = timestamp or self.next_timestamp()

        body = {
            "device_id": self.device_id,
            "sequence": sequence,
            "timestamp": timestamp,
            "delta_kwh": delta_kwh,
        }

        if self.version == attestation.MESSAGE_VERSION_V2:
            if meter_wh is None:
                self.meter_wh += round(delta_kwh * 1000)
                meter_wh = self.meter_wh
            tamper_count = self.tamper_count if tamper_count is None else tamper_count
            body.update(
                message_version=self.version, meter_wh=meter_wh, tamper_count=tamper_count
            )

        body["signature"] = attestation.sign_reading(
            self.private_key, self.device_id, sequence, timestamp, delta_kwh,
            self.version, meter_wh, tamper_count,
        )
        return body
