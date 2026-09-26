"""
Deterministic physical-plausibility rules for attested readings.

A valid signature establishes who produced a reading. It says nothing about
whether the number could be true: a compromised or badly wired device signs a
50 MWh interval as happily as a 0.2 kWh one. These rules reject what no real
installation could have produced, before anything reaches credit issuance.

Each rule is a pure function of the reading, the device's stored state, and the
current time, so the same inputs always produce the same verdict and every
rejection can be reproduced from the request alone.
"""

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import config

# The one timestamp form devices may sign. A fixed shape means the signed text
# and the stored value are the same string, and ordering is lexical as well as
# chronological.
_TIMESTAMP_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")


class PlausibilityError(ValueError):
    """A reading is well formed and authentic but physically impossible."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class DeviceState:
    """What the server already knows about a device, for evaluating its next reading."""

    device_id: str
    registered_at: float
    rated_capacity_kw: Optional[float]
    last_reading_at: Optional[str]
    last_meter_wh: Optional[int]
    tamper_count: int

    @property
    def capacity_kw(self) -> float:
        return self.rated_capacity_kw or config.DEFAULT_RATED_CAPACITY_KW


def parse_device_timestamp(value: str) -> datetime:
    """Parse a signed timestamp, accepting only YYYY-MM-DDTHH:MM:SSZ."""
    if not _TIMESTAMP_PATTERN.fullmatch(value or ""):
        raise PlausibilityError(
            "timestamp_format",
            f"Timestamp '{value}' must be UTC in the form YYYY-MM-DDTHH:MM:SSZ.",
        )
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        raise PlausibilityError("timestamp_format", f"Timestamp '{value}' is not a real date.")


def check_timestamp(timestamp: str, previous: Optional[str], state: DeviceState, now: float) -> float:
    """
    Validate a reading's time against the server clock and the device's history.

    Returns the timestamp as epoch seconds. Rejects readings from the future,
    from before the device existed, older than the backfill window, or not
    strictly after the previous accepted reading.
    """
    at = parse_device_timestamp(timestamp).timestamp()
    skew = config.MAX_CLOCK_SKEW_SECONDS

    if at > now + skew:
        raise PlausibilityError(
            "timestamp_future",
            f"Timestamp {timestamp} is {int(at - now)} s ahead of server time "
            f"(limit {skew} s). Check the device clock.",
        )
    if at < state.registered_at - skew:
        raise PlausibilityError(
            "timestamp_before_registration",
            f"Timestamp {timestamp} predates the device's registration. "
            "A key cannot attest energy produced before it was enrolled.",
        )
    max_age = config.MAX_READING_AGE_HOURS * 3600
    if at < now - max_age:
        raise PlausibilityError(
            "timestamp_stale",
            f"Timestamp {timestamp} is older than the {config.MAX_READING_AGE_HOURS} h "
            "backfill window. Older data has to be imported for review.",
        )
    if previous and timestamp <= previous:
        raise PlausibilityError(
            "timestamp_not_monotonic",
            f"Timestamp {timestamp} is not after the previous accepted reading ({previous}).",
        )
    return at


def interval_seconds(at: float, previous: Optional[str], state: DeviceState) -> float:
    """
    The span of generation a reading covers.

    Measured from the previous accepted reading, or from registration for a
    device's first one. Never shorter than the configured floor, so a device
    reporting every few seconds is not held to a bound finer than its meter.
    """
    start = (
        parse_device_timestamp(previous).timestamp() if previous else state.registered_at
    )
    return max(at - start, float(config.MIN_PLAUSIBILITY_INTERVAL_SECONDS))


def max_energy_kwh(capacity_kw: float, seconds: float) -> float:
    """The most a system of this nameplate could export over the interval."""
    return capacity_kw * (seconds / 3600.0) * config.CAPACITY_TOLERANCE


def check_capacity(delta_kwh: float, seconds: float, state: DeviceState) -> None:
    """Reject an interval that exceeds what the installation could physically export."""
    ceiling = max_energy_kwh(state.capacity_kw, seconds)
    if delta_kwh > ceiling:
        declared = "declared" if state.rated_capacity_kw else "default"
        raise PlausibilityError(
            "exceeds_capacity",
            f"{delta_kwh:.3f} kWh over {seconds / 3600:.2f} h exceeds the "
            f"{ceiling:.3f} kWh a {state.capacity_kw:g} kW ({declared}) system can export.",
        )


def check_meter_continuity(delta_kwh: float, meter_wh: int, state: DeviceState) -> None:
    """
    Check a V2 reading's energy against the device's lifetime counter.

    The counter is monotonic by construction on the device, so it may never go
    backwards, and the interval's energy must equal how far it advanced. A
    device's first V2 reading establishes the baseline and is not compared.
    """
    if meter_wh < 0:
        raise PlausibilityError("meter_negative", "meter_wh cannot be negative.")
    if state.last_meter_wh is None:
        return
    if meter_wh < state.last_meter_wh:
        raise PlausibilityError(
            "meter_regressed",
            f"meter_wh went backwards ({state.last_meter_wh} → {meter_wh}). The device's "
            "counter was reset; it must be re-enrolled rather than resume silently.",
        )

    advanced_wh = meter_wh - state.last_meter_wh
    claimed_wh = delta_kwh * 1000.0
    if abs(claimed_wh - advanced_wh) > config.METER_CONTINUITY_TOLERANCE_WH:
        raise PlausibilityError(
            "meter_discontinuity",
            f"delta_kwh claims {claimed_wh:.0f} Wh but the meter advanced {advanced_wh} Wh.",
        )


def check_tamper_counter(tamper_count: int, state: DeviceState) -> bool:
    """
    Validate the enclosure tamper counter. Returns True if it advanced.

    The counter only ever increases on the device. A decrease means its storage
    was wiped or the firmware replaced, which is itself a tamper event that
    must be resolved by re-enrolment, not accepted.
    """
    if tamper_count < state.tamper_count:
        raise PlausibilityError(
            "tamper_counter_regressed",
            f"tamper_count went backwards ({state.tamper_count} → {tamper_count}). "
            "Device storage was reset; re-enrol it.",
        )
    return tamper_count > state.tamper_count
