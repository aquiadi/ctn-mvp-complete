#!/usr/bin/env python3
"""
Replay a public inverter dataset into CTN readings, reproducibly.

The demo data behind the deployed site is not from CTN hardware. It comes from
the public "Solar Power Generation Data" set on Kaggle (Ani Kannal, 2020):
34 days, 15 May - 17 June 2020, of 15-minute inverter readings from two plants
in India. The demo device id 1BY6WEcLGh8j5v7 is an inverter SOURCE_KEY from
Plant 1. Any evaluation built on it is a replay of that dataset and should be
described and cited as one.

The original Colab notebook derived energy as DAILY_YIELD / 96 on every row.
DAILY_YIELD is a running total that resets each day, so that sums a cumulative
figure 96 times a day and does not measure anything. This script derives each
interval's energy by differencing DAILY_YIELD within a day, cross-checks it
against the inverter's lifetime TOTAL_YIELD, and prints every number a paper
would need to state, so a reviewer can regenerate them from the public file.

    python tools/replay_dataset.py Plant_1_Generation_Data.csv \\
        --source-key 1BY6WEcLGh8j5v7 --out readings.json
"""

import argparse
import csv
import json
import sys
from collections import OrderedDict
from datetime import datetime

EMISSION_FACTOR_KG_PER_KWH = 0.82
KG_CO2_PER_CREDIT = 1000.0


def parse_time(value: str) -> datetime:
    """Plant 1 uses 15-05-2020 00:00; Plant 2 uses 2020-05-15 00:00:00."""
    for fmt in ("%d-%m-%Y %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(value.strip(), fmt)
        except ValueError:
            continue
    raise ValueError(f"Unrecognised DATE_TIME '{value}'")


def load_rows(path: str, source_key: str) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as handle:
        rows = [
            {
                "time": parse_time(row["DATE_TIME"]),
                "daily_yield": float(row["DAILY_YIELD"]),
                "total_yield": float(row["TOTAL_YIELD"]),
            }
            for row in csv.DictReader(handle)
            if row["SOURCE_KEY"] == source_key
        ]
    rows.sort(key=lambda r: r["time"])
    return rows


def interval_energy(rows: list[dict]) -> list[dict]:
    """
    Energy per interval from DAILY_YIELD, which counts up from zero each day.

    Within a day the interval's energy is the increase since the previous
    sample. The first sample of a day is that day's yield so far. A decrease
    within a day is a logger glitch, not negative generation, and is counted
    as zero rather than subtracted.
    """
    readings = []
    previous = None
    for row in rows:
        if previous is None or row["time"].date() != previous["time"].date():
            delta = row["daily_yield"]
        else:
            delta = max(0.0, row["daily_yield"] - previous["daily_yield"])
        readings.append({"time": row["time"], "kwh": delta, "total_yield": row["total_yield"]})
        previous = row
    return readings


def summarise(readings: list[dict], factor: float) -> dict:
    per_day: "OrderedDict[str, float]" = OrderedDict()
    for r in readings:
        day = r["time"].date().isoformat()
        per_day[day] = per_day.get(day, 0.0) + r["kwh"]

    energy = sum(r["kwh"] for r in readings)
    lifetime_delta = readings[-1]["total_yield"] - readings[0]["total_yield"] if readings else 0.0
    co2 = energy * factor
    return {
        "samples": len(readings),
        "first": readings[0]["time"].isoformat() if readings else None,
        "last": readings[-1]["time"].isoformat() if readings else None,
        "days": len(per_day),
        "energy_kwh": round(energy, 3),
        "mean_daily_kwh": round(energy / len(per_day), 3) if per_day else 0.0,
        # TOTAL_YIELD is independent of DAILY_YIELD; the two should agree to
        # within the first day's pre-window generation.
        "total_yield_advance_kwh": round(lifetime_delta, 3),
        "emission_factor_kg_per_kwh": factor,
        "co2_avoided_kg": round(co2, 3),
        "whole_credits_at_1t": int(co2 // KG_CO2_PER_CREDIT),
        "remainder_kg": round(co2 % KG_CO2_PER_CREDIT, 3),
        "daily_kwh": {day: round(kwh, 3) for day, kwh in per_day.items()},
    }


def to_ctn_readings(readings: list[dict], device_id: str, factor: float) -> list[dict]:
    """The shape POST /api/installer/upload-readings and the seed loader accept."""
    return [
        {
            "device_id": device_id,
            "timestamp": r["time"].strftime("%Y-%m-%d %H:%M:%S"),
            "total_kwh": round(r["kwh"], 6),
            "co2_avoided_kg": round(r["kwh"] * factor, 6),
            "source": "kaggle:anikannal/solar-power-generation-data",
        }
        for r in readings
        if r["kwh"] > 0
    ]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("csv", help="Plant_1_Generation_Data.csv or Plant_2_Generation_Data.csv")
    parser.add_argument("--source-key", default="1BY6WEcLGh8j5v7")
    parser.add_argument("--factor", type=float, default=EMISSION_FACTOR_KG_PER_KWH)
    parser.add_argument("--out", help="write CTN readings JSON here")
    args = parser.parse_args(argv)

    rows = load_rows(args.csv, args.source_key)
    if not rows:
        print(f"No rows for SOURCE_KEY {args.source_key}", file=sys.stderr)
        return 1

    readings = interval_energy(rows)
    summary = summarise(readings, args.factor)
    print(json.dumps({k: v for k, v in summary.items() if k != "daily_kwh"}, indent=2))

    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            json.dump(to_ctn_readings(readings, args.source_key, args.factor), handle, indent=1)
        print(f"wrote {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
