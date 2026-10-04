"""Water parsing and discovery tests. Electricity statistic IDs are not involved."""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from custom_components.smarthub.api import Aggregation, SmartHubAPI, SmartHubDataError, SmartHubLocation
from custom_components.smarthub.const import ELECTRIC_INDUSTRY, ELECTRIC_SERVICE, METER_NAME, WATER_INDUSTRY
from custom_components.smarthub.sensor import water_hourly_windows
from custom_components.smarthub.utils import parse_epoch_set_timezone

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture
def api_instance():
    return SmartHubAPI(
        email="test@example.com",
        password="testpass",
        account_id="account-1",
        timezone="America/New_York",
        mfa_totp="",
        host="example.smarthub.coop",
    )


def _electric_usage(flow_direction: str) -> dict:
    return {
        "data": {
            "ELECTRIC": [
                {
                    "type": "USAGE",
                    "meters": [
                        {
                            "meterNumber": "meter-a",
                            "seriesId": "series-a",
                            "flowDirection": flow_direction,
                        }
                    ],
                    "series": [
                        {
                            "name": "series-a",
                            "data": [{"x": 1640995200000, "y": 12}],
                        }
                    ],
                }
            ]
        }
    }


@pytest.mark.parametrize("flow_direction", ["FORWARD", "TOTAL"])
def test_forward_and_total_select_consumption(api_instance, flow_direction):
    result = api_instance.parse_usage(_electric_usage(flow_direction))

    assert result[METER_NAME] == "series-a"
    assert len(result["USAGE"]) == 1
    assert result["USAGE"][0]["consumption"] == 12


def test_hourly_water_keeps_zero_and_ignores_chart_and_base_copies(api_instance):
    payload = load_fixture("water_hourly.json")

    water = api_instance.parse_water(payload)
    electric = api_instance.parse_usage(payload)

    assert water[METER_NAME] == "meter-water"
    assert [point["consumption"] for point in water["USAGE"]] == [3, 0, 1]
    assert [point["raw_timestamp"] for point in water["USAGE"]] == [
        1704067200000,
        1704070800000,
        1704074400000,
    ]
    assert water["USAGE"][1]["reading_time"] - water["USAGE"][0]["reading_time"] == timedelta(hours=1)
    assert electric["USAGE"][0]["consumption"] == 10


def test_hourly_water_windows_stay_within_30_days():
    start = datetime(2026, 7, 6, 0, 0)
    end = datetime(2026, 10, 4, 10, 0)

    windows = water_hourly_windows(start, end)

    assert windows[0][0] == start
    assert windows[-1][1] == end
    assert all(chunk_end - chunk_start <= timedelta(days=30) for chunk_start, chunk_end in windows)
    assert all(chunk_end - chunk_start == timedelta(days=30) for chunk_start, chunk_end in windows[:-1])
    assert all(windows[index][1] == windows[index + 1][0] for index in range(len(windows) - 1))

    short = water_hourly_windows(
        datetime(2026, 10, 2, tzinfo=timezone.utc),
        datetime(2026, 10, 4, 10, tzinfo=timezone.utc),
    )
    assert len(short) == 1


async def test_hourly_water_poll_asks_for_water_only(api_instance):
    captured = {}

    async def fake_poll(location, aggregation, start_datetime, end_datetime, industries):
        captured["aggregation"] = aggregation
        captured["industries"] = list(industries)
        return {"data": {}}

    api_instance._poll_usage = fake_poll
    location = SmartHubLocation("11111", ELECTRIC_SERVICE, "home", "provider", has_water=True)

    result = await api_instance.get_water_data(location, Aggregation.HOURLY)

    assert result == {}
    assert captured["aggregation"] == Aggregation.HOURLY
    assert captured["industries"] == [WATER_INDUSTRY]

    await api_instance.get_energy_data(location, Aggregation.HOURLY)
    assert captured["industries"] == [ELECTRIC_INDUSTRY]


def test_daily_water_keeps_zero_omits_missing_and_ignores_chart_copy(api_instance):
    result = api_instance.parse_water(load_fixture("water_daily.json"))

    assert result[METER_NAME] == "series-a"
    assert [point["consumption"] for point in result["USAGE"]] == [3, 0, 5]
    assert [point["raw_timestamp"] for point in result["USAGE"]] == [
        1696377600000,
        1696464000000,
        1696550400000,
    ]
    assert result["USAGE"][0]["reading_time"] == parse_epoch_set_timezone(
        1696377600000 / 1000.0, ZoneInfo("America/New_York")
    )


def test_mixed_response_keeps_electric_and_water_values_separate(api_instance):
    payload = load_fixture("water_daily.json")

    electric = api_instance.parse_usage(payload)
    water = api_instance.parse_water(payload)

    assert electric["USAGE"][0]["consumption"] == 10
    assert [point["consumption"] for point in water["USAGE"]] == [3, 0, 5]


def test_meter_number_matches_series_name(api_instance):
    payload = {
        "data": {
            "WATER": [
                {
                    "type": "USAGE",
                    "timeFrame": "DAILY",
                    "unitOfMeasure": "FT3",
                    "meters": [
                        {"meterNumber": "meter-a", "seriesId": "series-a", "flowDirection": "TOTAL"}
                    ],
                    "series": [
                        {
                            "name": "meter-a",
                            "data": [{"x": 1704067200000, "y": 7}],
                        }
                    ],
                    "meterToChartData": {"meter-a": [{"x": 1704067200000, "y": 999}]},
                }
            ]
        }
    }

    result = api_instance.parse_water(payload)

    assert result["USAGE"][0]["consumption"] == 7
    assert result[METER_NAME] == "meter-a"


def test_unidentified_series_are_not_guessed(api_instance):
    payload = {
        "data": {
            "WATER": [
                {
                    "type": "USAGE",
                    "series": [
                        {"data": [{"x": 1704067200000, "y": 3}]},
                        {"data": [{"x": 1704067200000, "y": 9}]},
                    ],
                }
            ]
        }
    }

    assert api_instance.parse_water(payload) == {}


def test_single_unnamed_series_is_used(api_instance):
    payload = {
        "data": {
            "WATER": [
                {
                    "type": "USAGE",
                    "series": [
                        {"data": [{"x": 1704067200000, "y": 4}, {"x": 1704153600000, "y": 0}]}
                    ],
                }
            ]
        }
    }

    result = api_instance.parse_water(payload)

    assert [point["consumption"] for point in result["USAGE"]] == [4, 0]


def test_water_timestamps_use_the_electric_timezone_stamp(api_instance):
    """Midnight UTC stays midnight local, including the 2026 DST boundaries."""
    points = []
    for stamp in (
        datetime(2026, 1, 15, tzinfo=timezone.utc),
        datetime(2026, 3, 8, tzinfo=timezone.utc),
        datetime(2026, 11, 1, tzinfo=timezone.utc),
    ):
        points.append({"x": int(stamp.timestamp() * 1000), "y": 1})
    points.append({"x": int(datetime(2026, 3, 8, 2, 30, tzinfo=timezone.utc).timestamp() * 1000), "y": 8})

    result = api_instance.parse_water(
        {"data": {"WATER": [{"type": "USAGE", "series": [{"data": points}]}]}}
    )

    assert len(result["USAGE"]) == 3
    for source, parsed in zip(points[:3], result["USAGE"]):
        expected = parse_epoch_set_timezone(source["x"] / 1000.0, ZoneInfo("America/New_York"))
        assert parsed["reading_time"] == expected
        assert parsed["reading_time"].hour == 0
        assert parsed["reading_time"].minute == 0
    assert all(point["consumption"] != 8 for point in result["USAGE"])


def test_parse_water_rejects_non_dict(api_instance):
    with pytest.raises(SmartHubDataError):
        api_instance.parse_water(["not", "a", "dict"])


def test_combined_service_discovers_one_electric_location_with_water(api_instance):
    locations = api_instance.parse_locations(
        [
            {
                "inactive": False,
                "services": ["WATER|NGAS|ELEC|SEWER|TRASH"],
                "serviceToServiceDescription": {
                    "WATER|NGAS|ELEC|SEWER|TRASH": "City Utilities"
                },
                "serviceToProviders": {"WATER|NGAS|ELEC|SEWER|TRASH": ["ECU"]},
                "providerToDescription": {"ECU": "Combined Utilities"},
                "serviceLocationToIndustries": {"LOC1": ["ELECTRIC", "WATER"]},
                "serviceLocationToUserDataServiceLocationSummaries": {
                    "LOC1": [
                        {
                            "services": ["WATER|NGAS|ELEC|SEWER|TRASH"],
                            "description": "Home",
                        }
                    ]
                },
            }
        ]
    )

    assert len(locations) == 1
    assert locations[0].id == "LOC1"
    assert locations[0].service == ELECTRIC_SERVICE
    assert locations[0].has_water is True
    assert locations[0].description == "Home"


def test_electric_only_location_is_not_marked_as_water(api_instance):
    locations = api_instance.parse_locations(
        [
            {
                "inactive": False,
                "services": ["ELEC"],
                "serviceToServiceDescription": {"ELEC": "Electric Service"},
                "serviceToProviders": {"ELEC": ["ECU"]},
                "providerToDescription": {"ECU": "Combined Utilities"},
                "serviceLocationToIndustries": {"LOC1": ["ELECTRIC"]},
                "serviceLocationToUserDataServiceLocationSummaries": {
                    "LOC1": [{"services": ["ELEC"], "description": "Home"}]
                },
            }
        ]
    )

    assert len(locations) == 1
    assert locations[0].has_water is False
