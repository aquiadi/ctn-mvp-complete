"""
Helpers for normalising externally supplied generation data.
"""

from typing import Iterable


def to_discrete_readings(raw: Iterable[dict]) -> list[dict]:
    """
    Convert cumulative meter readings into per-interval deltas.

    The published seed dataset reports running totals per device, so consecutive
    entries must be differenced before they can be accumulated into credits —
    otherwise every reading would be counted again from zero.

    Readings are grouped by device and ordered by timestamp. Deltas are clamped
    at zero so a meter reset or an out-of-order sample cannot subtract from the
    running total.
    """
    by_device: dict[str, list[dict]] = {}
    for entry in raw or []:
        by_device.setdefault(entry.get("device_id", "unknown"), []).append(entry)

    readings: list[dict] = []
    for device_entries in by_device.values():
        device_entries.sort(key=lambda e: e.get("timestamp", ""))

        previous_kwh = 0.0
        previous_co2 = 0.0

        for entry in device_entries:
            cumulative_kwh = float(entry.get("total_kwh", 0) or 0)
            cumulative_co2 = float(entry.get("co2_avoided_kg", 0) or 0)

            reading = dict(entry)
            reading["total_kwh"] = max(0.0, cumulative_kwh - previous_kwh)
            reading["co2_avoided_kg"] = max(0.0, cumulative_co2 - previous_co2)
            readings.append(reading)

            previous_kwh = cumulative_kwh
            previous_co2 = cumulative_co2

    readings.sort(key=lambda r: (r.get("device_id", ""), r.get("timestamp", "")))
    return readings
