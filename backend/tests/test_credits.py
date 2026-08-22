"""Reading ingestion, credit issuance, and the public read endpoints."""

import config
import database
from data_utils import to_discrete_readings


# ── Cumulative-to-delta conversion ─────────────────────────────────────────

def test_cumulative_readings_become_deltas():
    readings = to_discrete_readings([
        {"device_id": "A", "timestamp": "2024-01-01 00:00", "total_kwh": 100, "co2_avoided_kg": 82},
        {"device_id": "A", "timestamp": "2024-01-01 01:00", "total_kwh": 250, "co2_avoided_kg": 205},
    ])
    assert [r["total_kwh"] for r in readings] == [100, 150]
    assert [r["co2_avoided_kg"] for r in readings] == [82, 123]


def test_devices_are_differenced_independently():
    readings = to_discrete_readings([
        {"device_id": "A", "timestamp": "2024-01-01 00:00", "total_kwh": 100, "co2_avoided_kg": 82},
        {"device_id": "B", "timestamp": "2024-01-01 00:00", "total_kwh": 500, "co2_avoided_kg": 410},
    ])
    by_device = {r["device_id"]: r["total_kwh"] for r in readings}
    assert by_device == {"A": 100, "B": 500}


def test_out_of_order_input_is_sorted_before_differencing():
    readings = to_discrete_readings([
        {"device_id": "A", "timestamp": "2024-01-01 02:00", "total_kwh": 300, "co2_avoided_kg": 246},
        {"device_id": "A", "timestamp": "2024-01-01 01:00", "total_kwh": 100, "co2_avoided_kg": 82},
    ])
    assert [r["total_kwh"] for r in readings] == [100, 200]


def test_a_meter_reset_cannot_produce_a_negative_delta():
    readings = to_discrete_readings([
        {"device_id": "A", "timestamp": "2024-01-01 00:00", "total_kwh": 500, "co2_avoided_kg": 410},
        {"device_id": "A", "timestamp": "2024-01-01 01:00", "total_kwh": 10, "co2_avoided_kg": 8},
    ])
    assert readings[1]["total_kwh"] == 0
    assert readings[1]["co2_avoided_kg"] == 0


def test_empty_input_is_handled():
    assert to_discrete_readings([]) == []
    assert to_discrete_readings(None) == []


# ── Credit issuance ────────────────────────────────────────────────────────

def _readings(device: str, owner_id: int, count: int, kwh_each: float, hour_offset: int = 0):
    return [
        {
            "device_id": device,
            "timestamp": f"2025-01-01 {hour_offset + i:02d}:00:00",
            "total_kwh": kwh_each,
            "co2_avoided_kg": kwh_each * config.EMISSION_FACTOR_KG_PER_KWH,
            "owner_user_id": owner_id,
            "location": "Test City",
        }
        for i in range(count)
    ]


async def test_a_credit_is_issued_per_tonne_of_co2(app_client, make_user):
    _, user = await make_user("installer")
    # 2 x 610 kWh -> 1000.4 kg CO2 -> exactly one credit, with a small remainder.
    inserted, issued = await database.process_raw_readings(
        _readings("ISSUE-1", user["id"], 2, 610), user["id"]
    )
    assert inserted == 2
    assert issued == 1


async def test_partial_generation_issues_nothing(app_client, make_user):
    _, user = await make_user("installer")
    inserted, issued = await database.process_raw_readings(
        _readings("ISSUE-2", user["id"], 1, 100), user["id"]
    )
    assert inserted == 1
    assert issued == 0


async def test_a_remainder_carries_into_the_next_ingestion(app_client, make_user):
    """Generation below the threshold must not be discarded between batches."""
    _, user = await make_user("installer")

    _, first = await database.process_raw_readings(
        _readings("ISSUE-3", user["id"], 1, 700, hour_offset=0), user["id"]
    )
    assert first == 0

    _, second = await database.process_raw_readings(
        _readings("ISSUE-3", user["id"], 1, 700, hour_offset=1), user["id"]
    )
    assert second == 1, "the first batch's 574 kg should count toward the credit"


async def test_reingesting_the_same_readings_is_a_no_op(app_client, make_user):
    _, user = await make_user("installer")
    batch = _readings("ISSUE-4", user["id"], 2, 610)

    first_inserted, first_issued = await database.process_raw_readings(batch, user["id"])
    second_inserted, second_issued = await database.process_raw_readings(batch, user["id"])

    assert (first_inserted, first_issued) == (2, 1)
    assert (second_inserted, second_issued) == (0, 0)


async def test_issued_credits_carry_the_device_location(app_client, make_user):
    _, user = await make_user("installer")
    await database.process_raw_readings(_readings("ISSUE-5", user["id"], 2, 610), user["id"])

    row = await database.database.fetch_one(
        "SELECT location, co2_avoided_kg FROM credits WHERE device_id = 'ISSUE-5'"
    )
    assert row["location"] == "Test City"
    assert row["co2_avoided_kg"] == config.KG_CO2_PER_CREDIT


async def test_credit_ids_survive_a_deletion(app_client, make_user):
    """
    Regression test.

    Ids were derived from a row count, so deleting any credit made the next
    issuance collide with an existing id and violate the unique constraint.
    """
    _, user = await make_user("installer")
    await database.process_raw_readings(_readings("ISSUE-6", user["id"], 2, 610), user["id"])

    highest = await database.database.fetch_one("SELECT MAX(credit_id) AS m FROM credits")
    await database.database.execute(
        "DELETE FROM credits WHERE credit_id = :id", {"id": highest["m"] - 1}
    )

    _, issued = await database.process_raw_readings(
        _readings("ISSUE-7", user["id"], 2, 610), user["id"]
    )
    assert issued == 1


# ── Public endpoints ───────────────────────────────────────────────────────

async def test_config_publishes_the_values_the_frontend_needs(app_client):
    body = (await app_client.get("/config")).json()
    assert body["price_per_credit_inr"] == config.CREDIT_VALUE_INR
    assert body["kg_co2_per_credit"] == config.KG_CO2_PER_CREDIT
    assert body["sell_threshold"] == config.SELL_THRESHOLD
    assert body["chain_writes_enabled"] is False


async def test_value_uses_one_tonne_per_credit(app_client):
    """
    Regression test.

    The endpoint kept a 50 kg-per-credit formula after the platform moved to
    one-tonne credits, understating the CO2 figure by twentyfold.
    """
    body = (await app_client.get("/value/10")).json()
    assert body["co2_kg"] == 10 * config.KG_CO2_PER_CREDIT
    assert body["usd"] == 10 * config.CREDIT_VALUE_USD
    assert body["inr"] == 10 * config.CREDIT_VALUE_INR


async def test_value_rejects_a_negative_count(app_client):
    assert (await app_client.get("/value/-5")).status_code == 400


async def test_daily_averages_use_the_real_period(app_client, make_user):
    """
    Regression test.

    Averages were divided by a hardcoded 34-day period regardless of the data,
    which scaled every per-day figure by an arbitrary factor.
    """
    stats = (await app_client.get("/stats")).json()
    daily = (await app_client.get("/daily")).json()

    assert stats["period_days"] >= 1
    assert daily["period_days"] == stats["period_days"]
    if stats["total_credits"]:
        expected = round(stats["total_kwh"] / stats["period_days"], 2)
        assert daily["kwh_per_day"] == expected


def test_mixed_timestamp_formats_do_not_crash_the_period_calculation():
    """
    Regression test.

    Signed readings carry an ISO offset and imported CSV rows do not. Both land
    in the same columns, so MIN/MAX can return one of each, and subtracting an
    aware datetime from a naive one raised — taking /stats down for everyone.
    """
    from main import _period_days

    assert _period_days("2026-05-01T06:00:00Z", "2026-08-01 06:00:00") == 92
    assert _period_days("2026-05-01 06:00:00", "2026-08-01T06:00:00Z") == 92
    assert _period_days("2026-05-01T06:00:00+05:30", "2026-05-02T06:00:00Z") == 1


async def test_compare_translates_co2_into_equivalents(app_client):
    body = (await app_client.get("/compare/1000")).json()
    assert body["kg_co2"] == 1000
    assert body["equivalent_to"]["trees_planted_10yr"] > 0


async def test_an_unknown_credit_returns_404(app_client):
    assert (await app_client.get("/credits/99999999")).status_code == 404
    assert (await app_client.get("/verify/99999999")).status_code == 404


async def test_credit_pagination_is_bounded(app_client):
    """An oversized limit must not let a caller pull the whole table."""
    body = (await app_client.get("/credits?page=1&limit=100000")).json()
    assert len(body["credits"]) <= 200
