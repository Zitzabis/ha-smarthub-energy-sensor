"""Test file for SmartHub sensor (statistics)"""
import pytest
from unittest.mock import Mock, patch, AsyncMock
from collections.abc import Generator
from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.smarthub import async_setup_entry
from custom_components.smarthub.api import SmartHubAPI, SmartHubAPIError, SmartHubDataError, SmartHubLocation
from custom_components.smarthub.const import DOMAIN, ELECTRIC_SERVICE, WATER_SENSOR_KEY

from custom_components.smarthub.sensor import SmartHubDataUpdateCoordinator, history_window_start
from homeassistant.components.recorder import Recorder
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
    statistics_during_period,
)
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from homeassistant.util import dt as dt_util
from homeassistant.const import UnitOfVolume

from custom_components.smarthub.api import Aggregation
from custom_components.smarthub.const import CONF_HISTORY_START, HISTORICAL_IMPORT_DAYS


from homeassistant.components.recorder import get_instance

@pytest.fixture(autouse=True)
def mock_smarthub_api(hass) -> Generator[AsyncMock]:
    """Mock the config entry ..."""

    api = SmartHubAPI(
        email="test@example.com",
        password="testpass",
        account_id="123456",
        timezone="UTC",
        mfa_totp="",
        host="test.smarthub.coop"
    )

    with patch(
        "custom_components.smarthub.api.SmartHubAPI", autospec=True
    ) as mock_api:

        mock_api.timezone="UTC"
        mock_api.parse_usage = api.parse_usage
        mock_api.get_service_locations.return_value = []
        mock_api.get_energy_data.return_value = {}
        yield mock_api


@pytest.fixture()
def mock_config_entry(hass) -> MockConfigEntry:
    """Create a mock config entry."""
    return MockConfigEntry(
        version=1,
        domain=DOMAIN,
        title="SmartHub Test",
        data={
            "email": "test@example.com",
            "password": "testpass",
            "account_id": "123456",
            "location_id": "789012",
            "host": "test.smarthub.coop",
            "poll_interval": 60,
            "timezone": "UTC",
            "mfa_totp": "",
        },
        unique_id="test@example.com_test.smarthub.coop_123456",
    )

async def test_coordinator_first_run_forward_meter(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_smarthub_api: AsyncMock,
) -> None:
    """Test the coordinator on its first run with no existing statistics."""
    mock_smarthub_api.get_service_locations.return_value = [
      SmartHubLocation(
        id="11111",
        service=ELECTRIC_SERVICE,
        description="test location",
        provider="test provider",
      )
    ]

    test_data = {
        "data": {
            "ELECTRIC": [
                {
                    "type": "USAGE",
                    "meters": [
                     {'meterNumber': '1ND91111111', 'seriesId': '1ND91111111', 'flowDirection': 'FORWARD', 'isNetMeter': False}, # Forward meter is full consumption.
                    ],
                    "series": [
                        {
                            "meterNumber": "1ND91111111", "name": "1ND91111111",
                            "data": [
                                {"x": 1762215300000, "y":   1},
                                {"x": 1762216200000, "y":  10},
                                {"x": 1762217100000, "y": 100},
                                {"x": 1762218900000, "y": 1},
                            ]
                        },
                    ]
                }
            ]
        }
    }

    mock_smarthub_api.get_energy_data.return_value = mock_smarthub_api.parse_usage(test_data)

    coordinator = SmartHubDataUpdateCoordinator(hass, api=mock_smarthub_api, update_interval=timedelta(minutes=720), config_entry=mock_config_entry)

    await coordinator._async_update_data()

    await async_wait_recording_done(hass)

    # Check stats for electric account '111111'
    stats = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        dt_util.utc_from_timestamp(0),
        None,
        {
            "smarthub:smarthub_energy_sensor_daily_123456_11111",
        },
        "hour",
        None,
        {"state", "sum"},
    )

    # The first hour's statistics summary is...
    assert stats["smarthub:smarthub_energy_sensor_daily_123456_11111"][0]["sum"] == 111.0


async def test_coordinator_first_run_net_meter(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_smarthub_api: AsyncMock,
) -> None:
    """Test the coordinator on its first run with no existing statistics."""
    mock_smarthub_api.get_service_locations.return_value = [
      SmartHubLocation(
        id="11111",
        service=ELECTRIC_SERVICE,
        description="test location",
        provider="test provider",
      )
    ]

    test_data = {
        "data": {
            "ELECTRIC": [
                {
                    "type": "USAGE",
                    "meters": [
                     {'meterNumber': '1ND81111111', 'seriesId': '1ND81111111', 'flowDirection': 'NET', 'isNetMeter': True}, # Includes in and out bound flows (posirive/negative)
                     {'meterNumber': '1ND91111111', 'seriesId': '1ND91111111', 'flowDirection': 'FORWARD', 'isNetMeter': False}, # Forward meter is full consumption.
                    ],
                    "series": [
                        {
                            "meterNumber": "1ND91111111", "name": "1ND91111111",
                            "data": [
                                {"x": 1762215300000, "y":   1},
                                {"x": 1762216200000, "y":  10},
                                {"x": 1762217100000, "y": 100},
                                {"x": 1762218900000, "y": 1},
                                {"x": 1762219800000, "y": 1},
                            ]
                        },
                        {
                            "meterNumber": "1ND81111111", "name": "1ND81111111",
                            "data": [
                                {"x": 1762215300000, "y":   1},
                                {"x": 1762216200000, "y":  -5}, # generated 15 KW of power this hour - returned 5 to the grid
                                {"x": 1762217100000, "y": 100},
                                {"x": 1762218900000, "y": 1},
                                {"x": 1762219800000, "y":  -1}, # generated 2 KW of power this hour - returned 1 to the grid
                            ]
                        },
                    ]
                }
            ]
        }
    }

    mock_smarthub_api.get_energy_data.return_value = mock_smarthub_api.parse_usage(test_data)
    assert mock_smarthub_api.get_energy_data.return_value["USAGE_RETURN"][0]['consumption'] == 5
    assert mock_smarthub_api.get_energy_data.return_value["USAGE_RETURN"][1]['consumption'] == 1

    coordinator = SmartHubDataUpdateCoordinator(hass, api=mock_smarthub_api, update_interval=timedelta(minutes=720), config_entry=mock_config_entry)

    await coordinator._async_update_data()

    await async_wait_recording_done(hass)

    # Check stats for electric account '111111'
    stats = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        dt_util.utc_from_timestamp(0),
        None,
        {
            "smarthub:smarthub_energy_sensor_daily_123456_11111",
            "smarthub:smarthub_energy_return_sensor_daily_123456_11111",
        },
        "hour",
        None,
        {"state", "sum"},
    )

    # The first hour's statistics summary is...
    assert stats["smarthub:smarthub_energy_sensor_daily_123456_11111"][0]["sum"] == 101.0
    assert stats["smarthub:smarthub_energy_sensor_daily_123456_11111"][1]["sum"] == 102.0
    assert stats["smarthub:smarthub_energy_return_sensor_daily_123456_11111"][0]["sum"] == 5
    assert stats["smarthub:smarthub_energy_return_sensor_daily_123456_11111"][1]["sum"] == 6

async def test_coordinator_first_run_return_meter(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_smarthub_api: AsyncMock,
) -> None:
    """Test the coordinator on its first run with no existing statistics."""
    mock_smarthub_api.get_service_locations.return_value = [
      SmartHubLocation(
        id="11111",
        service=ELECTRIC_SERVICE,
        description="test location",
        provider="test provider",
      )
    ]

    test_data = {
        "data": {
            "ELECTRIC": [
                {
                    "type": "USAGE",
                    "meters": [
                     {'meterNumber': '1ND91111111', 'seriesId': '1ND87334444', 'flowDirection': 'RETURN', 'isNetMeter': False}, # Includes in and out bound flows (posirive/negative)
                     {'meterNumber': '1ND91111111', 'seriesId': '1ND86200137', 'flowDirection': 'FORWARD', 'isNetMeter': False}, # Forward meter is full consumption.
                    ],
                    "series": [
                        {
                            "meterNumber": "1ND86200137", "name": "1ND86200137",
                            "data": [
                                {"x": 1762215300000, "y":   1},
                                {"x": 1762216200000, "y":  10},
                                {"x": 1762217100000, "y": 100},
                                {"x": 1762218900000, "y": 1},
                                {"x": 1762219800000, "y": 1},
                            ]
                        },
                        {
                            "meterNumber": "1ND87334444", "name": "1ND87334444",
                            "data": [
                                {"x": 1762215300000, "y":   0},
                                {"x": 1762216200000, "y":  5}, # generated 15 KW of power this hour - returned 5 to the grid
                                {"x": 1762217100000, "y": 0},
                                {"x": 1762218900000, "y": 0},
                                {"x": 1762219800000, "y":  1}, # generated 2 KW of power this hour - returned 1 to the grid
                            ]
                        },
                    ]
                }
            ]
        }
    }

    mock_smarthub_api.get_energy_data.return_value = mock_smarthub_api.parse_usage(test_data)
    assert mock_smarthub_api.get_energy_data.return_value["USAGE_RETURN"][0]['consumption'] == 5
    assert mock_smarthub_api.get_energy_data.return_value["USAGE_RETURN"][1]['consumption'] == 1

    coordinator = SmartHubDataUpdateCoordinator(hass, api=mock_smarthub_api, update_interval=timedelta(minutes=720), config_entry=mock_config_entry)

    await coordinator._async_update_data()

    await async_wait_recording_done(hass)

    # Check stats for electric account '111111'
    stats = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        dt_util.utc_from_timestamp(0),
        None,
        {
            "smarthub:smarthub_energy_sensor_daily_123456_11111",
            "smarthub:smarthub_energy_return_sensor_daily_123456_11111",
        },
        "hour",
        None,
        {"state", "sum"},
    )

    # The first hour's statistics summary is...
    assert stats["smarthub:smarthub_energy_sensor_daily_123456_11111"][0]["sum"] == 111.0
    assert stats["smarthub:smarthub_energy_sensor_daily_123456_11111"][1]["sum"] == 113.0
    assert stats["smarthub:smarthub_energy_return_sensor_daily_123456_11111"][0]["sum"] == 5
    assert stats["smarthub:smarthub_energy_return_sensor_daily_123456_11111"][1]["sum"] == 6



def _usage_at(stamp: datetime, consumption: float, raw_timestamp: int | None = None) -> dict:
    return {
        "reading_time": stamp,
        "consumption": consumption,
        "raw_timestamp": raw_timestamp if raw_timestamp is not None else int(stamp.timestamp() * 1000),
    }


async def _seed_statistic(hass, statistic_id: str, start: datetime, unit: str, unit_class: str) -> None:
    async_add_external_statistics(
        hass,
        StatisticMetaData(
            mean_type=StatisticMeanType.NONE,
            has_sum=True,
            name=statistic_id,
            source=DOMAIN,
            statistic_id=statistic_id,
            unit_class=unit_class,
            unit_of_measurement=unit,
        ),
        [StatisticData(start=start, state=1.0, sum=1.0)],
    )
    await async_wait_recording_done(hass)


async def test_water_initial_import_is_90_days_when_electricity_already_exists(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_smarthub_api: AsyncMock,
) -> None:
    """Existing electricity statistics keep the two-day refresh. Water uses 90 days."""
    location = SmartHubLocation(
        id="11111",
        service=ELECTRIC_SERVICE,
        description="test location",
        provider="test provider",
        has_water=True,
    )
    mock_smarthub_api.get_service_locations.return_value = [location]
    expected_initial = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=HISTORICAL_IMPORT_DAYS)
    seeded = expected_initial.astimezone()
    await _seed_statistic(
        hass, "smarthub:smarthub_energy_sensor_123456_11111", seeded, "kWh", "energy"
    )
    await _seed_statistic(
        hass, "smarthub:smarthub_energy_sensor_daily_123456_11111", seeded, "kWh", "energy"
    )

    electric_calls = []
    water_calls = []

    async def get_energy_data(location, aggregation, start_datetime=None, end_datetime=None, **kwargs):
        electric_calls.append((aggregation, start_datetime, end_datetime))
        return {"USAGE": [_usage_at(seeded, 1)], "meter_name": "series-forward"}

    async def get_water_data(location, aggregation, start_datetime=None, end_datetime=None, **kwargs):
        water_calls.append((aggregation, start_datetime, end_datetime))
        return {"USAGE": [_usage_at(seeded, 2)], "meter_name": "series-a"}

    mock_smarthub_api.get_energy_data.side_effect = get_energy_data
    mock_smarthub_api.get_water_data.side_effect = get_water_data

    coordinator = SmartHubDataUpdateCoordinator(
        hass, api=mock_smarthub_api, update_interval=timedelta(minutes=720), config_entry=mock_config_entry
    )
    await coordinator._async_update_data()

    expected_refresh = seeded - timedelta(days=2)
    electric_hourly = [(start, end) for aggregation, start, end in electric_calls if aggregation == Aggregation.HOURLY]
    electric_daily = [start for aggregation, start, end in electric_calls if aggregation == Aggregation.DAILY]
    water_daily = [start for aggregation, start, end in water_calls if aggregation == Aggregation.DAILY]
    water_hourly = [(start, end) for aggregation, start, end in water_calls if aggregation == Aggregation.HOURLY]
    assert electric_hourly[0][0] == expected_refresh
    assert electric_daily == [expected_refresh]
    _assert_chunked_hourly_windows(electric_hourly)
    assert water_daily == [expected_initial]
    assert water_hourly[0][0] == expected_initial
    _assert_chunked_hourly_windows(water_hourly)
    assert water_hourly[-1][1] - water_hourly[0][0] >= timedelta(days=HISTORICAL_IMPORT_DAYS)


async def test_existing_water_statistics_use_two_day_lookback(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_smarthub_api: AsyncMock,
) -> None:
    location = SmartHubLocation(
        id="11111",
        service=ELECTRIC_SERVICE,
        description="test location",
        provider="test provider",
        has_water=True,
    )
    mock_smarthub_api.get_service_locations.return_value = [location]
    expected_initial = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=HISTORICAL_IMPORT_DAYS)
    seeded = expected_initial.astimezone()
    await _seed_statistic(
        hass,
        "smarthub:smarthub_water_sensor_daily_123456_11111",
        seeded,
        UnitOfVolume.CUBIC_FEET,
        "volume",
    )
    water_calls = []

    async def get_energy_data(location, aggregation, start_datetime=None, **kwargs):
        return {"USAGE": [_usage_at(seeded, 1)]}

    async def get_water_data(location, aggregation, start_datetime=None, end_datetime=None, **kwargs):
        water_calls.append((aggregation, start_datetime, end_datetime))
        return {"USAGE": [_usage_at(seeded, 2)], "meter_name": "series-a"}

    mock_smarthub_api.get_energy_data.side_effect = get_energy_data
    mock_smarthub_api.get_water_data.side_effect = get_water_data

    coordinator = SmartHubDataUpdateCoordinator(
        hass, api=mock_smarthub_api, update_interval=timedelta(minutes=720), config_entry=mock_config_entry
    )
    await coordinator._async_update_data()

    water_daily = [start for aggregation, start, end in water_calls if aggregation == Aggregation.DAILY]
    water_hourly = [(start, end) for aggregation, start, end in water_calls if aggregation == Aggregation.HOURLY]
    assert water_daily == [seeded - timedelta(days=2)]
    assert water_hourly[0][0] == expected_initial
    _assert_chunked_hourly_windows(water_hourly)


async def test_duplicate_water_readings_are_summed_once(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_smarthub_api: AsyncMock,
) -> None:
    location = SmartHubLocation(
        id="11111",
        service=ELECTRIC_SERVICE,
        description="test location",
        provider="test provider",
        has_water=True,
    )
    mock_smarthub_api.get_service_locations.return_value = [location]
    stamp = datetime(2026, 9, 1, tzinfo=timezone.utc)
    misaligned = stamp.replace(minute=15)

    async def get_energy_data(location, aggregation, start_datetime=None, **kwargs):
        return {"USAGE": [_usage_at(stamp, 1)]}

    async def get_water_data(location, aggregation, start_datetime=None, **kwargs):
        if aggregation == Aggregation.MONTHLY:
            return {"USAGE": [_usage_at(stamp, 2)]}
        return {
            "USAGE": [
                _usage_at(stamp, 3, raw_timestamp=111),
                _usage_at(misaligned, 100, raw_timestamp=222),
                _usage_at(stamp, 4, raw_timestamp=111),
            ]
        }

    mock_smarthub_api.get_energy_data.side_effect = get_energy_data
    mock_smarthub_api.get_water_data.side_effect = get_water_data

    coordinator = SmartHubDataUpdateCoordinator(
        hass, api=mock_smarthub_api, update_interval=timedelta(minutes=720), config_entry=mock_config_entry
    )
    result = await coordinator._async_update_data()
    await async_wait_recording_done(hass)

    stats = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        dt_util.utc_from_timestamp(0),
        None,
        {
            "smarthub:smarthub_water_sensor_daily_123456_11111",
            "smarthub:smarthub_water_sensor_123456_11111",
        },
        "hour",
        None,
        {"state", "sum"},
    )
    rows = stats["smarthub:smarthub_water_sensor_daily_123456_11111"]
    assert len(rows) == 1
    assert rows[0]["state"] == 4
    assert rows[0]["sum"] == 4
    hourly_rows = stats["smarthub:smarthub_water_sensor_123456_11111"]
    assert len(hourly_rows) == 1
    assert hourly_rows[0]["state"] == 4
    assert hourly_rows[0]["sum"] == 4
    assert result["11111"]["current_water_usage"] == 2


async def test_existing_hourly_water_statistics_use_two_day_lookback(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_smarthub_api: AsyncMock,
) -> None:
    location = SmartHubLocation(
        id="11111",
        service=ELECTRIC_SERVICE,
        description="test location",
        provider="test provider",
        has_water=True,
    )
    mock_smarthub_api.get_service_locations.return_value = [location]
    expected_initial = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=HISTORICAL_IMPORT_DAYS)
    seeded = expected_initial.astimezone()
    await _seed_statistic(
        hass,
        "smarthub:smarthub_water_sensor_123456_11111",
        seeded,
        UnitOfVolume.CUBIC_FEET,
        "volume",
    )
    water_calls = []

    async def get_energy_data(location, aggregation, start_datetime=None, **kwargs):
        return {"USAGE": [_usage_at(seeded, 1)]}

    async def get_water_data(location, aggregation, start_datetime=None, end_datetime=None, **kwargs):
        water_calls.append((aggregation, start_datetime, end_datetime))
        return {"USAGE": [_usage_at(seeded, 2)], "meter_name": "series-a"}

    mock_smarthub_api.get_energy_data.side_effect = get_energy_data
    mock_smarthub_api.get_water_data.side_effect = get_water_data

    coordinator = SmartHubDataUpdateCoordinator(
        hass, api=mock_smarthub_api, update_interval=timedelta(minutes=720), config_entry=mock_config_entry
    )
    await coordinator._async_update_data()

    water_hourly = [(start, end) for aggregation, start, end in water_calls if aggregation == Aggregation.HOURLY]
    water_daily = [start for aggregation, start, end in water_calls if aggregation == Aggregation.DAILY]
    assert water_hourly[0][0] == seeded - timedelta(days=2)
    _assert_chunked_hourly_windows(water_hourly)
    assert water_daily == [expected_initial]


async def test_hourly_water_failure_still_updates_electricity(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_smarthub_api: AsyncMock,
) -> None:
    location = SmartHubLocation(
        id="11111",
        service=ELECTRIC_SERVICE,
        description="test location",
        provider="test provider",
        has_water=True,
    )
    mock_smarthub_api.get_service_locations.return_value = [location]
    stamp = datetime(2026, 9, 1, tzinfo=timezone.utc)
    electric_calls = []

    async def get_energy_data(location, aggregation, start_datetime=None, **kwargs):
        electric_calls.append(aggregation)
        assert kwargs.get("industries") in (None, ["ELECTRIC"])
        return {"USAGE": [_usage_at(stamp, 5)]}

    async def get_water_data(location, aggregation, start_datetime=None, **kwargs):
        if aggregation == Aggregation.HOURLY:
            raise SmartHubDataError("interval rejected")
        return {"USAGE": [_usage_at(stamp, 2)]}

    mock_smarthub_api.get_energy_data.side_effect = get_energy_data
    mock_smarthub_api.get_water_data.side_effect = get_water_data

    coordinator = SmartHubDataUpdateCoordinator(
        hass, api=mock_smarthub_api, update_interval=timedelta(minutes=720), config_entry=mock_config_entry
    )
    result = await coordinator._async_update_data()

    assert Aggregation.HOURLY in electric_calls
    assert Aggregation.DAILY in electric_calls
    assert result["11111"]["current_energy_usage"] == 5
    assert result["11111"]["current_water_usage"] == 2


async def test_monthly_water_failure_still_updates_electricity(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_smarthub_api: AsyncMock,
) -> None:
    location = SmartHubLocation(
        id="11111",
        service=ELECTRIC_SERVICE,
        description="test location",
        provider="test provider",
        has_water=True,
    )
    mock_smarthub_api.get_service_locations.return_value = [location]
    stamp = datetime(2026, 9, 1, tzinfo=timezone.utc)

    async def get_energy_data(location, aggregation, start_datetime=None, **kwargs):
        return {"USAGE": [_usage_at(stamp, 5)]}

    async def get_water_data(location, aggregation, start_datetime=None, **kwargs):
        if aggregation == Aggregation.MONTHLY:
            raise SmartHubDataError("monthly water rejected")
        return {"USAGE": [_usage_at(stamp, 2)]}

    mock_smarthub_api.get_energy_data.side_effect = get_energy_data
    mock_smarthub_api.get_water_data.side_effect = get_water_data

    coordinator = SmartHubDataUpdateCoordinator(
        hass, api=mock_smarthub_api, update_interval=timedelta(minutes=720), config_entry=mock_config_entry
    )
    result = await coordinator._async_update_data()

    assert result["11111"]["current_energy_usage"] == 5
    assert WATER_SENSOR_KEY not in result["11111"]


def _config_with_history(mock_config_entry: MockConfigEntry, history_start: str) -> MockConfigEntry:
    data = dict(mock_config_entry.data)
    data[CONF_HISTORY_START] = history_start
    data["timezone"] = "UTC"
    return MockConfigEntry(
        version=1,
        domain=DOMAIN,
        title="SmartHub Test",
        data=data,
        unique_id=f"{mock_config_entry.unique_id}_{history_start}",
    )


def test_history_window_start_uses_utility_midnight() -> None:
    """The saved date is midnight in the utility timezone and does not roll forward."""
    start = history_window_start("2026-01-15", "America/New_York")
    assert start == datetime(2026, 1, 15, tzinfo=ZoneInfo("America/New_York"))
    rolling = history_window_start(None, "UTC")
    assert rolling.tzinfo is None
    assert rolling.hour == 0 and rolling.minute == 0 and rolling.second == 0


async def test_chosen_history_start_is_requested_on_first_import(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_smarthub_api: AsyncMock,
) -> None:
    """A configured date replaces the rolling 90-day first import."""
    entry = _config_with_history(mock_config_entry, "2026-06-01")
    location = SmartHubLocation(
        id="11111",
        service=ELECTRIC_SERVICE,
        description="test location",
        provider="test provider",
    )
    mock_smarthub_api.get_service_locations.return_value = [location]
    calls = []
    expected = history_window_start("2026-06-01", "UTC")

    async def get_energy_data(location, aggregation, start_datetime=None, end_datetime=None, **kwargs):
        calls.append((aggregation, start_datetime, end_datetime))
        return {"USAGE": [_usage_at(expected, 1)]}

    mock_smarthub_api.get_energy_data.side_effect = get_energy_data
    coordinator = SmartHubDataUpdateCoordinator(
        hass, api=mock_smarthub_api, update_interval=timedelta(minutes=720), config_entry=entry
    )
    await coordinator._async_update_data()

    hourly = [(start, end) for aggregation, start, end in calls if aggregation == Aggregation.HOURLY]
    daily = [start for aggregation, start, end in calls if aggregation == Aggregation.DAILY]
    assert hourly[0][0] == expected
    assert daily == [expected]
    _assert_chunked_hourly_windows(hourly)


async def test_earlier_history_start_extends_existing_statistics(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_smarthub_api: AsyncMock,
) -> None:
    """An earlier date is imported once. The following poll returns to the two-day refresh."""
    entry = _config_with_history(mock_config_entry, "2026-01-01")
    location = SmartHubLocation(
        id="11111",
        service=ELECTRIC_SERVICE,
        description="test location",
        provider="test provider",
    )
    mock_smarthub_api.get_service_locations.return_value = [location]
    seeded = datetime(2026, 7, 15, tzinfo=timezone.utc)
    await _seed_statistic(hass, "smarthub:smarthub_energy_sensor_123456_11111", seeded, "kWh", "energy")
    await _seed_statistic(hass, "smarthub:smarthub_energy_sensor_daily_123456_11111", seeded, "kWh", "energy")
    calls = []

    async def get_energy_data(location, aggregation, start_datetime=None, end_datetime=None, **kwargs):
        calls.append((aggregation, start_datetime, end_datetime))
        return {"USAGE": [_usage_at(seeded, 1)]}

    mock_smarthub_api.get_energy_data.side_effect = get_energy_data
    coordinator = SmartHubDataUpdateCoordinator(
        hass, api=mock_smarthub_api, update_interval=timedelta(minutes=720), config_entry=entry
    )
    await coordinator._async_update_data()

    expected = history_window_start("2026-01-01", "UTC")
    hourly = [(start, end) for aggregation, start, end in calls if aggregation == Aggregation.HOURLY]
    daily = [start for aggregation, start, end in calls if aggregation == Aggregation.DAILY]
    assert hourly[0][0] == expected
    assert daily == [expected]
    _assert_chunked_hourly_windows(hourly)

    calls.clear()
    await coordinator._async_update_data()
    refresh = seeded - timedelta(days=2)
    hourly = [(start, end) for aggregation, start, end in calls if aggregation == Aggregation.HOURLY]
    daily = [start for aggregation, start, end in calls if aggregation == Aggregation.DAILY]
    assert hourly[0][0] == refresh
    assert daily == [refresh]
    _assert_chunked_hourly_windows(hourly)


async def test_later_history_start_keeps_older_rows(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_smarthub_api: AsyncMock,
) -> None:
    """Choosing a later date refreshes recent rows and leaves older statistics in place."""
    entry = _config_with_history(mock_config_entry, "2026-09-01")
    location = SmartHubLocation(
        id="11111",
        service=ELECTRIC_SERVICE,
        description="test location",
        provider="test provider",
    )
    mock_smarthub_api.get_service_locations.return_value = [location]
    seeded = datetime(2026, 1, 1, tzinfo=timezone.utc)
    statistic_id = "smarthub:smarthub_energy_sensor_daily_123456_11111"
    await _seed_statistic(hass, "smarthub:smarthub_energy_sensor_123456_11111", seeded, "kWh", "energy")
    await _seed_statistic(hass, statistic_id, seeded, "kWh", "energy")
    calls = []

    async def get_energy_data(location, aggregation, start_datetime=None, end_datetime=None, **kwargs):
        calls.append((aggregation, start_datetime))
        if aggregation == Aggregation.MONTHLY:
            return {"USAGE": [_usage_at(seeded, 4)]}
        return {"USAGE": []}

    mock_smarthub_api.get_energy_data.side_effect = get_energy_data
    coordinator = SmartHubDataUpdateCoordinator(
        hass, api=mock_smarthub_api, update_interval=timedelta(minutes=720), config_entry=entry
    )
    await coordinator._async_update_data()
    await async_wait_recording_done(hass)

    refresh = seeded - timedelta(days=2)
    daily = [start for aggregation, start in calls if aggregation == Aggregation.DAILY]
    assert daily == [refresh]
    assert history_window_start("2026-09-01", "UTC") not in daily

    stats = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        dt_util.utc_from_timestamp(0),
        None,
        {statistic_id},
        "hour",
        None,
        {"state", "sum"},
    )
    rows = stats[statistic_id]
    assert len(rows) == 1
    assert rows[0]["start"] == pytest.approx(seeded.timestamp())
    assert rows[0]["sum"] == 1.0


def _assert_chunked_hourly_windows(hourly) -> None:
    assert hourly
    for index, (start, end) in enumerate(hourly):
        assert end is not None
        assert timedelta(0) < end - start <= timedelta(days=30)
        if index < len(hourly) - 1:
            assert end - start == timedelta(days=30)
            assert hourly[index + 1][0] == end


async def async_wait_recording_done(hass) -> None:
    """Async wait until recording is done."""
    await hass.async_block_till_done()
    get_instance(hass)._async_commit(dt_util.utcnow())
    await hass.async_block_till_done()
    await hass.async_add_executor_job(get_instance(hass).block_till_done)
    await hass.async_block_till_done()
