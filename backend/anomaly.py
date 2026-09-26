"""
Statistical anomaly screening for attested readings.

The deterministic rules in plausibility.py reject what is physically impossible.
This module looks for what is possible but unlikely: generation while the sun is
down, a meter repeating the same value for hours, or an interval far above the
device's own history for that time of day.

It deliberately never rejects anything. A screen that could refuse data would
become a new trust boundary nobody can audit; one that holds a credit for a
person to review adds scrutiny without removing any. Every flag records what
was measured and the threshold it crossed, so the reviewer sees the reasoning
rather than a score.
"""

import math
import statistics
from datetime import datetime, timezone
from typing import Iterable, Optional

import config

# 0.6745 is the 75th percentile of the standard normal; scaling the median
# absolute deviation by it makes the robust z-score comparable to a z-score.
_MAD_SCALE = 0.6745

# Below this an interval is treated as no generation at all — meter resolution
# and inverter standby draw, not a claim worth screening.
_NEGLIGIBLE_KWH = 0.001


def solar_elevation_deg(at: datetime, latitude: float, longitude: float) -> float:
    """
    Sun elevation above the horizon, in degrees, at a UTC instant.

    NOAA's general solar position equations. Accurate to a fraction of a degree,
    which is far finer than the night test needs.
    """
    at = at.astimezone(timezone.utc)
    day_of_year = at.timetuple().tm_yday
    hour = at.hour + at.minute / 60 + at.second / 3600

    gamma = 2 * math.pi / 365 * (day_of_year - 1 + (hour - 12) / 24)
    eqtime = 229.18 * (
        0.000075
        + 0.001868 * math.cos(gamma)
        - 0.032077 * math.sin(gamma)
        - 0.014615 * math.cos(2 * gamma)
        - 0.040849 * math.sin(2 * gamma)
    )
    declination = (
        0.006918
        - 0.399912 * math.cos(gamma)
        + 0.070257 * math.sin(gamma)
        - 0.006758 * math.cos(2 * gamma)
        + 0.000907 * math.sin(2 * gamma)
        - 0.002697 * math.cos(3 * gamma)
        + 0.00148 * math.sin(3 * gamma)
    )

    true_solar_minutes = hour * 60 + eqtime + 4 * longitude
    hour_angle = math.radians(true_solar_minutes / 4 - 180)
    lat = math.radians(latitude)

    cos_zenith = (
        math.sin(lat) * math.sin(declination)
        + math.cos(lat) * math.cos(declination) * math.cos(hour_angle)
    )
    zenith = math.degrees(math.acos(max(-1.0, min(1.0, cos_zenith))))
    return 90.0 - zenith


def max_elevation_over(start: float, end: float, latitude: float, longitude: float) -> float:
    """Highest sun elevation across an interval, sampled every ten minutes."""
    step = 600.0
    samples = max(1, int((end - start) // step))
    points = [start + i * step for i in range(samples)] + [end]
    return max(
        solar_elevation_deg(datetime.fromtimestamp(p, tz=timezone.utc), latitude, longitude)
        for p in points
    )


def _solar_hour(epoch: float, longitude: Optional[float]) -> int:
    """Hour of local mean solar time, or UTC when the site is unknown."""
    offset = (longitude or 0.0) / 15.0 * 3600
    return datetime.fromtimestamp(epoch + offset, tz=timezone.utc).hour


def _flag(code: str, detail: str, **measured) -> dict:
    return {"code": code, "detail": detail, **measured}


def screen(
    delta_kwh: float,
    start: float,
    end: float,
    latitude: Optional[float],
    longitude: Optional[float],
    history: Iterable[tuple[float, float, float]],
) -> list[dict]:
    """
    Screen one interval. Returns a list of flags, empty when nothing stands out.

    `history` is the device's recent accepted intervals as (start, end,
    delta_kwh), oldest first. The interval under test must not be in it.
    """
    if not config.ANOMALY_SCREENING:
        return []

    flags: list[dict] = []
    history = list(history)
    seconds = max(end - start, 1.0)

    # Generation with the sun below the horizon for the entire interval.
    if latitude is not None and longitude is not None and delta_kwh > _NEGLIGIBLE_KWH:
        peak = max_elevation_over(start, end, latitude, longitude)
        if peak < config.ANOMALY_NIGHT_ELEVATION_DEG:
            flags.append(_flag(
                "generation_at_night",
                f"{delta_kwh:.3f} kWh reported while the sun never rose above "
                f"{peak:.1f}° at the registered site.",
                sun_elevation_max_deg=round(peak, 2),
            ))

    # A healthy meter never repeats itself exactly for hours; a stuck one, or a
    # placeholder value in firmware, does.
    run = config.ANOMALY_FLATLINE_RUN
    recent = [kwh for _, _, kwh in history[-(run - 1):]] + [delta_kwh]
    if len(recent) >= run and delta_kwh > _NEGLIGIBLE_KWH and len(set(recent)) == 1:
        flags.append(_flag(
            "flatline",
            f"The last {run} intervals all reported exactly {delta_kwh:.6f} kWh.",
            run_length=run,
        ))

    # Far above this device's own history for the same hour of the solar day.
    # Only the high side is screened: low output is what clouds look like.
    hour = _solar_hour(end, longitude)
    peers = [
        kwh / max((e - s) / 3600.0, 1e-9)
        for s, e, kwh in history[-config.ANOMALY_HISTORY_WINDOW:]
        if _solar_hour(e, longitude) in {(hour - 1) % 24, hour, (hour + 1) % 24}
    ]
    if len(peers) >= config.ANOMALY_MIN_HISTORY:
        median = statistics.median(peers)
        mad = statistics.median(abs(p - median) for p in peers)
        power_kw = delta_kwh / (seconds / 3600.0)
        if mad > 0:
            z = _MAD_SCALE * (power_kw - median) / mad
            if z > config.ANOMALY_ROBUST_Z:
                flags.append(_flag(
                    "power_outlier",
                    f"Average power {power_kw:.3f} kW is {z:.1f} robust standard deviations "
                    f"above this device's median of {median:.3f} kW for this time of day.",
                    power_kw=round(power_kw, 4),
                    median_kw=round(median, 4),
                    robust_z=round(z, 2),
                    history_size=len(peers),
                ))

    return flags
