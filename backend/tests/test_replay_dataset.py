"""
The dataset replay tool: interval energy from a resetting daily counter.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))

import replay_dataset  # noqa: E402

HEADER = "DATE_TIME,PLANT_ID,SOURCE_KEY,DC_POWER,AC_POWER,DAILY_YIELD,TOTAL_YIELD\n"


def _csv(tmp_path, rows):
    path = tmp_path / "plant.csv"
    path.write_text(HEADER + "".join(
        f"{t},4135001,{key},0,0,{daily},{total}\n" for t, key, daily, total in rows
    ))
    return str(path)


def test_energy_is_differenced_within_each_day(tmp_path):
    path = _csv(tmp_path, [
        ("15-05-2020 06:00", "INV", 0, 1000),
        ("15-05-2020 12:00", "INV", 20, 1020),
        ("15-05-2020 18:00", "INV", 30, 1030),
        ("15-05-2020 23:45", "INV", 30, 1030),
        ("16-05-2020 00:00", "INV", 0, 1030),   # reset at midnight
        ("16-05-2020 12:00", "INV", 25, 1055),
        ("15-05-2020 12:00", "OTHER", 999, 9999),
    ])
    rows = replay_dataset.load_rows(path, "INV")
    summary = replay_dataset.summarise(replay_dataset.interval_energy(rows), 0.82)

    assert summary["days"] == 2
    assert summary["energy_kwh"] == 55.0
    assert summary["total_yield_advance_kwh"] == 55.0
    assert summary["daily_kwh"] == {"2020-05-15": 30.0, "2020-05-16": 25.0}
    assert summary["co2_avoided_kg"] == 45.1


def test_the_old_notebook_formula_is_not_what_is_computed(tmp_path):
    """DAILY_YIELD / 96 per row sums a running total; it is not interval energy."""
    rows = [("15-05-2020 %02d:00" % h, "INV", h * 2, 1000 + h * 2) for h in range(6, 19)]
    path = _csv(tmp_path, rows)
    readings = replay_dataset.interval_energy(replay_dataset.load_rows(path, "INV"))

    notebook = sum(daily / 96 for _, _, daily, _ in rows)
    measured = sum(r["kwh"] for r in readings)
    assert measured == 36.0          # the day's final DAILY_YIELD
    assert abs(notebook - measured) > 1


def test_a_glitch_within_a_day_is_not_negative_energy(tmp_path):
    path = _csv(tmp_path, [
        ("15-05-2020 10:00", "INV", 10, 1010),
        ("15-05-2020 10:15", "INV", 8, 1010),
        ("15-05-2020 10:30", "INV", 12, 1012),
    ])
    readings = replay_dataset.interval_energy(replay_dataset.load_rows(path, "INV"))
    assert [r["kwh"] for r in readings] == [10, 0.0, 4]


def test_both_date_formats_are_read():
    assert replay_dataset.parse_time("15-05-2020 06:15").day == 15
    assert replay_dataset.parse_time("2020-05-15 06:15:00").month == 5
