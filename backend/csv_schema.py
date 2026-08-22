"""
Work out what an uploaded CSV actually contains.

Meter and inverter exports have no common schema. Columns are named whatever the
vendor chose, energy arrives in Wh, kWh, or MWh, some files report a running
total while others report each interval, and many carry no device column at all
because the export came from a single machine.

Demanding one fixed layout rejects almost every real file. So the columns are
identified by what they look like, and the result is reported back for the
uploader to confirm before anything is written — a wrong guess applied silently
would corrupt the ledger, and the ledger is the product.
"""

import csv
import io
import re
import statistics
from datetime import datetime
from typing import Optional

# Ordered longest-first so "kwh" is never matched as "wh".
ENERGY_UNITS = (
    ("mwh", 1000.0),
    ("kwh", 1.0),
    ("wh", 0.001),
)
POWER_UNITS = (
    ("mw", 1000.0),
    ("kw", 1.0),
    ("w", 0.001),
)

TIME_HINTS = ("timestamp", "datetime", "date/time", "date_time", "time", "date",
              "period", "interval", "reading_time", "measured_at", "when")
ENERGY_HINTS = ("kwh", "mwh", "wh", "energy", "yield", "generation", "generated",
                "production", "produced", "export", "delta", "consumption")
POWER_HINTS = ("power", "pac", "ac_power", "kw", "mw", "output")
DEVICE_HINTS = ("device", "device_id", "inverter", "serial", "meter", "plant",
                "site", "station", "unit", "asset")

TIME_FORMATS = (
    "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
    "%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%d/%m/%Y",
    "%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M", "%m/%d/%Y",
    "%d-%m-%Y %H:%M:%S", "%d-%m-%Y %H:%M",
    "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M",
)


class SchemaError(ValueError):
    """The file could not be interpreted as generation data."""


# ── Parsing primitives ─────────────────────────────────────────────────────

def parse_time(value: str) -> Optional[datetime]:
    """Parse a timestamp in any of the layouts exports commonly use."""
    text = (value or "").strip()
    if not text:
        return None

    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        pass

    for fmt in TIME_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def parse_number(value: str) -> Optional[float]:
    """
    Read a number, tolerating the decoration exports add.

    Thousands separators, unit suffixes, and stray currency-style spacing all
    appear in the wild; a value that is genuinely not numeric returns None.
    """
    text = (value or "").strip().replace(",", "").replace(" ", "")
    if not text:
        return None

    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        return None
    try:
        return float(match.group())
    except ValueError:
        return None


def _normalise(header: str) -> str:
    """
    Reduce a heading to space-separated words for matching.

    Punctuation and underscores become spaces so that "delta_kwh" and
    "Total Yield (kWh)" both expose "kwh" as a standalone word. Without this,
    matching on substrings alone reads the "wh" inside "when" as an energy unit.
    """
    return re.sub(r"[^a-z0-9]+", " ", header.strip().lower()).strip()


def _score(header: str, hints: tuple) -> int:
    """
    How strongly a column name suggests a role. Longer hits score higher.

    Hits are on whole words, so a short unit token cannot match by appearing
    inside an unrelated word.
    """
    name = f" {_normalise(header)} "
    best = 0
    for hint in hints:
        phrase = f" {_normalise(hint)} "
        if phrase in name:
            best = max(best, len(hint))
    return best


def _unit_factor(header: str, units: tuple) -> Optional[float]:
    """Convert-to-kWh (or kW) factor implied by a column name."""
    name = f" {_normalise(header)} "
    for token, factor in units:
        if f" {token} " in name:
            return factor
    return None


# ── Detection ──────────────────────────────────────────────────────────────

class Detection:
    """What was found in a file, and what will be done with it."""

    def __init__(self):
        self.time_column = None
        self.value_column = None
        self.device_column = None
        self.value_kind = None        # "energy" or "power"
        self.unit_factor = 1.0
        self.unit_label = "kWh"
        self.cumulative = False
        self.interval_minutes = None
        self.row_count = 0
        self.total_kwh = 0.0
        self.warnings: list[str] = []
        self.sample: list[dict] = []

    def as_dict(self) -> dict:
        return {
            "time_column": self.time_column,
            "value_column": self.value_column,
            "device_column": self.device_column,
            "value_kind": self.value_kind,
            "unit": self.unit_label,
            "cumulative": self.cumulative,
            "interval_minutes": self.interval_minutes,
            "rows": self.row_count,
            "total_kwh": round(self.total_kwh, 3),
            "warnings": self.warnings,
            "sample": self.sample,
        }

    def describe(self) -> str:
        """A sentence an uploader can check without reading the code."""
        parts = [f"Time from “{self.time_column}”"]

        if self.value_kind == "power":
            parts.append(
                f"power from “{self.value_column}” in {self.unit_label}, "
                f"converted using a {self.interval_minutes}-minute interval"
            )
        else:
            reading = "a running total" if self.cumulative else "per-interval values"
            parts.append(f"energy from “{self.value_column}” in {self.unit_label}, as {reading}")

        parts.append(
            f"device from “{self.device_column}”" if self.device_column
            else "no device column, so one must be chosen"
        )
        return "; ".join(parts) + "."


def _read_rows(content: bytes) -> tuple[list[str], list[dict]]:
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            text = content.decode("latin-1")
        except UnicodeDecodeError:
            raise SchemaError("The file is not readable as text. Export it as CSV.")

    # Exports use commas, semicolons, or tabs depending on locale.
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    except csv.Error:
        reader = csv.DictReader(io.StringIO(text))

    headers = [h for h in (reader.fieldnames or []) if h and h.strip()]
    if not headers:
        raise SchemaError("No column headings were found in the first row.")

    return headers, list(reader)


def detect(content: bytes) -> Detection:
    """
    Identify the columns and how to read them.

    Raises SchemaError when the file has no usable time or quantity column,
    which is the one case where no amount of guessing helps.
    """
    headers, rows = _read_rows(content)
    if not rows:
        raise SchemaError("The file has headings but no data rows.")

    found = Detection()
    found.row_count = len(rows)

    # ── Columns ──
    time_candidates = sorted(headers, key=lambda h: _score(h, TIME_HINTS), reverse=True)
    if _score(time_candidates[0], TIME_HINTS):
        found.time_column = time_candidates[0]
    else:
        # Fall back to whichever column actually parses as a time.
        for header in headers:
            if sum(1 for r in rows[:20] if parse_time(r.get(header, ""))) >= max(1, min(10, len(rows)) // 2):
                found.time_column = header
                break

    if not found.time_column:
        raise SchemaError(
            "No column of dates or times was found. Generation has to be placed in "
            "time before it can be turned into credits."
        )

    # The time column is already spoken for; letting it win here would read a
    # timestamp as a quantity and silently produce zero generation.
    value_headers = [h for h in headers if h != found.time_column] or headers
    energy_header = max(value_headers, key=lambda h: _score(h, ENERGY_HINTS))
    power_header = max(value_headers, key=lambda h: _score(h, POWER_HINTS))

    if _score(energy_header, ENERGY_HINTS) >= _score(power_header, POWER_HINTS) and _score(energy_header, ENERGY_HINTS):
        found.value_column, found.value_kind = energy_header, "energy"
        found.unit_factor = _unit_factor(energy_header, ENERGY_UNITS) or 1.0
    elif _score(power_header, POWER_HINTS):
        found.value_column, found.value_kind = power_header, "power"
        found.unit_factor = _unit_factor(power_header, POWER_UNITS) or 1.0
    else:
        # Nothing named helpfully: take the numeric column that varies most,
        # which is the one carrying the measurement rather than an index.
        numeric = {}
        for header in value_headers:
            values = [parse_number(r.get(header, "")) for r in rows[:50]]
            values = [v for v in values if v is not None]
            if len(values) >= max(2, len(rows[:50]) // 2):
                numeric[header] = statistics.pstdev(values) if len(values) > 1 else 0
        if not numeric:
            raise SchemaError(
                "No column of numbers was found alongside the timestamps, so there "
                "is no generation to read."
            )
        found.value_column = max(numeric, key=numeric.get)
        found.value_kind = "energy"
        found.warnings.append(
            f"No column was clearly labelled as energy, so “{found.value_column}” "
            "was used. Check this is the generation figure."
        )

    if found.unit_factor == 1.0 and not _unit_factor(found.value_column, ENERGY_UNITS + POWER_UNITS):
        found.warnings.append(
            f"No unit was given in “{found.value_column}”, so kWh was assumed."
        )
    found.unit_label = {1000.0: "MWh", 1.0: "kWh", 0.001: "Wh"}.get(found.unit_factor, "kWh")
    if found.value_kind == "power":
        found.unit_label = {1000.0: "MW", 1.0: "kW", 0.001: "W"}.get(found.unit_factor, "kW")

    device_header = max(headers, key=lambda h: _score(h, DEVICE_HINTS))
    if _score(device_header, DEVICE_HINTS):
        found.device_column = device_header

    # ── Shape of the series ──
    points = []
    for row in rows:
        when = parse_time(row.get(found.time_column, ""))
        value = parse_number(row.get(found.value_column, ""))
        if when is not None and value is not None:
            points.append((when, value))

    if not points:
        raise SchemaError(
            f"“{found.time_column}” and “{found.value_column}” were found, but no row "
            "had a readable value in both."
        )
    points.sort(key=lambda p: p[0])

    gaps = [
        (points[i][0] - points[i - 1][0]).total_seconds() / 60
        for i in range(1, len(points))
        if (points[i][0] - points[i - 1][0]).total_seconds() > 0
    ]
    found.interval_minutes = round(statistics.median(gaps)) if gaps else 15

    if found.value_kind == "energy":
        # A running meter total only ever climbs; per-interval output rises and
        # falls with the sun. That difference is what tells them apart.
        rises = sum(1 for i in range(1, len(points)) if points[i][1] >= points[i - 1][1])
        found.cumulative = len(points) > 2 and rises / (len(points) - 1) >= 0.95

    found.total_kwh = sum(v for _, v in _to_interval_values(points, found))
    found.sample = [
        {
            "timestamp": when.isoformat(sep=" "),
            "kwh": round(kwh, 4),
        }
        for when, kwh in _to_interval_values(points, found)[:5]
    ]
    return found


def _to_interval_values(points: list[tuple], found: Detection) -> list[tuple]:
    """Convert raw readings into per-interval kWh."""
    scaled = [(when, value * found.unit_factor) for when, value in points]

    if found.value_kind == "power":
        hours = (found.interval_minutes or 15) / 60
        return [(when, power * hours) for when, power in scaled]

    if not found.cumulative:
        return scaled

    # Difference a running total, clamping at zero so a meter reset or an
    # out-of-order row cannot subtract from the running figure.
    out, previous = [], None
    for when, total in scaled:
        out.append((when, 0.0 if previous is None else max(0.0, total - previous)))
        previous = total
    return out


def to_readings(content: bytes, found: Detection, default_device: str) -> list[dict]:
    """Produce readings using an already-confirmed interpretation."""
    _, rows = _read_rows(content)

    grouped: dict[str, list[tuple]] = {}
    for row in rows:
        when = parse_time(row.get(found.time_column, ""))
        value = parse_number(row.get(found.value_column, ""))
        if when is None or value is None:
            continue

        device = default_device
        if found.device_column:
            named = (row.get(found.device_column) or "").strip()
            if named:
                device = named
        grouped.setdefault(device, []).append((when, value))

    readings = []
    for device, points in grouped.items():
        # Cumulative totals are per device, so each is differenced on its own.
        points.sort(key=lambda p: p[0])
        for when, kwh in _to_interval_values(points, found):
            readings.append(
                {
                    "device_id": device,
                    "timestamp": when.isoformat(sep=" "),
                    "delta_kwh": round(kwh, 6),
                }
            )
    return readings
