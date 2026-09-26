"""
The anomaly screen, tested as pure functions with fixed times and places.
"""

from datetime import datetime, timezone

import anomaly

# Patna, Bihar.
LAT, LON = 25.59, 85.14


def _epoch(text: str) -> float:
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()


def test_solar_elevation_is_high_at_noon_and_negative_at_midnight():
    # Local solar noon in Patna is about 06:25 UTC; midnight about 18:25 UTC.
    noon = anomaly.solar_elevation_deg(datetime(2026, 6, 21, 6, 25, tzinfo=timezone.utc), LAT, LON)
    midnight = anomaly.solar_elevation_deg(datetime(2026, 6, 21, 18, 25, tzinfo=timezone.utc), LAT, LON)

    # At the June solstice the sun at 25.6 N culminates near 90 - (25.6 - 23.4).
    assert 86 < noon < 90
    assert midnight < -40


def test_generation_at_night_is_flagged():
    flags = anomaly.screen(
        2.0, _epoch("2026-06-21T17:00:00Z"), _epoch("2026-06-21T17:15:00Z"), LAT, LON, []
    )
    assert [f["code"] for f in flags] == ["generation_at_night"]


def test_daytime_generation_is_not_flagged():
    flags = anomaly.screen(
        2.0, _epoch("2026-06-21T06:00:00Z"), _epoch("2026-06-21T06:15:00Z"), LAT, LON, []
    )
    assert flags == []


def test_an_interval_spanning_sunrise_is_not_flagged():
    flags = anomaly.screen(
        0.5, _epoch("2026-06-21T22:00:00Z"), _epoch("2026-06-22T00:30:00Z"), LAT, LON, []
    )
    assert flags == []


def test_the_night_check_needs_coordinates():
    flags = anomaly.screen(
        2.0, _epoch("2026-06-21T17:00:00Z"), _epoch("2026-06-21T17:15:00Z"), None, None, []
    )
    assert flags == []


def _day_history(days: int, kwh_at: dict) -> list:
    """Fifteen-minute intervals over several days with a fixed shape per hour."""
    history = []
    start = _epoch("2026-06-01T00:00:00Z")
    for step in range(days * 96):
        s = start + step * 900
        hour = datetime.fromtimestamp(s + 900 + LON / 15 * 3600, tz=timezone.utc).hour
        base = kwh_at.get(hour, 0.0)
        history.append((s, s + 900, base + (step % 7) * 0.01))
    return history


def test_an_output_far_above_history_is_flagged():
    history = _day_history(10, {h: 1.0 for h in range(8, 17)})
    at = _epoch("2026-06-11T06:30:00Z")  # about noon local solar time
    flags = anomaly.screen(9.0, at - 900, at, LAT, LON, history)

    outlier = [f for f in flags if f["code"] == "power_outlier"]
    assert outlier and outlier[0]["robust_z"] > 6


def test_normal_output_is_not_flagged():
    history = _day_history(10, {h: 1.0 for h in range(8, 17)})
    at = _epoch("2026-06-11T06:30:00Z")
    assert anomaly.screen(1.03, at - 900, at, LAT, LON, history) == []


def test_low_output_is_never_flagged():
    """Clouds make output drop; only the high side is suspicious."""
    history = _day_history(10, {h: 1.0 for h in range(8, 17)})
    at = _epoch("2026-06-11T06:30:00Z")
    assert anomaly.screen(0.01, at - 900, at, LAT, LON, history) == []


def test_a_flatline_is_flagged():
    history = [(i * 900.0, (i + 1) * 900.0, 0.15) for i in range(20)]
    flags = anomaly.screen(0.15, 21 * 900.0, 22 * 900.0, None, None, history)
    assert "flatline" in [f["code"] for f in flags]
