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


# ── CSV ingestion ──────────────────────────────────────────────────────────

CSV_REQUIRED_COLUMNS = {"device_id", "timestamp", "delta_kwh"}
CSV_MAX_BYTES = 5 * 1024 * 1024
CSV_MAX_REPORTED_ERRORS = 20


class CsvError(ValueError):
    """A CSV upload could not be accepted. Carries per-line detail."""

    def __init__(self, message: str, errors: list[str] = None):
        super().__init__(message)
        self.message = message
        self.errors = errors or []

    def as_detail(self) -> dict:
        return {
            "message": self.message,
            "errors": self.errors[:CSV_MAX_REPORTED_ERRORS],
            "truncated": len(self.errors) > CSV_MAX_REPORTED_ERRORS,
        }


def parse_reading_csv(content: bytes, devices: dict, emission_factor: float) -> list[dict]:
    """
    Turn an uploaded CSV into readings, or raise with per-line errors.

    `devices` maps device_id to its owner and location, and doubles as the
    permission check: a row naming a device absent from that mapping is refused,
    so an uploader can only ever add readings for devices they were given.

    Every row is validated before any is returned. A partially applied import
    would leave a gap indistinguishable from missing generation.
    """
    import csv
    import io

    if len(content) > CSV_MAX_BYTES:
        raise CsvError(f"CSV exceeds the {CSV_MAX_BYTES // (1024 * 1024)}MB limit.")

    try:
        decoded = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise CsvError("CSV must be UTF-8 encoded.")

    reader = csv.DictReader(io.StringIO(decoded))
    missing = CSV_REQUIRED_COLUMNS - set(reader.fieldnames or [])
    if missing:
        raise CsvError(f"CSV is missing required columns: {', '.join(sorted(missing))}")

    readings, errors = [], []
    for line_number, row in enumerate(reader, start=2):  # row 1 is the header
        device_id = (row.get("device_id") or "").strip()
        timestamp = (row.get("timestamp") or "").strip()
        raw_kwh = (row.get("delta_kwh") or "").strip()

        if not (device_id and timestamp and raw_kwh):
            errors.append(f"Line {line_number}: missing a required value.")
            continue
        if device_id not in devices:
            errors.append(
                f"Line {line_number}: '{device_id}' is not one of your devices."
            )
            continue

        try:
            delta_kwh = float(raw_kwh)
        except ValueError:
            errors.append(f"Line {line_number}: delta_kwh must be numeric, got '{raw_kwh}'.")
            continue

        if delta_kwh < 0:
            errors.append(f"Line {line_number}: delta_kwh cannot be negative.")
            continue

        device = devices[device_id]
        readings.append(
            {
                "device_id": device_id,
                "timestamp": timestamp,
                # For delta ingestion this is the interval's own generation,
                # not a running meter total.
                "total_kwh": delta_kwh,
                "co2_avoided_kg": delta_kwh * emission_factor,
                "location": device["location"],
                "owner_user_id": device["owner_user_id"],
            }
        )

    if errors:
        raise CsvError(
            f"CSV validation failed with {len(errors)} error(s). Nothing was imported.",
            errors,
        )
    if not readings:
        raise CsvError("CSV contains no data rows.")

    return readings
