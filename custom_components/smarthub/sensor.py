"""SmartHub energy sensor platform."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import Any, Dict, Optional

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy, UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
    UpdateFailed,
)
from homeassistant.util.unit_conversion import EnergyConverter, VolumeConverter
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
    statistics_during_period,
)
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMetaData,
)

try:
    from homeassistant.components.recorder.models import StatisticMeanType
except ImportError:
    from enum import Enum
    class StatisticMeanType(str, Enum):
        NONE = "none"
        MEAN = "mean"
        MAX = "max"
        MIN = "min"


from .api import Aggregation, SmartHubAPI, SmartHubLocation
from .exceptions import (
    SmartHubAuthenticationError,
    SmartHubError as SmartHubAPIError,
)
from .const import (
    DOMAIN,
    ENERGY_SENSOR_KEY,
    WATER_SENSOR_KEY,
    ATTR_LAST_READING_TIME,
    ATTR_WATER_LAST_READING_TIME,
    ATTR_ACCOUNT_ID,
    ATTR_LOCATION_ID,
    LOCATION_KEY,
    HISTORICAL_IMPORT_DAYS,
    WATER_HOURLY_REQUEST_DAYS,
    METER_NAME,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the SmartHub sensor platform."""
    _LOGGER.debug("Setting up SmartHub sensor platform")

    config: Dict[str, Any] = config_entry.data

    coordinator = config_entry.runtime_data

    # Ensure that it is the smartHub coordinator
    assert type(coordinator) is SmartHubDataUpdateCoordinator

    last_locations_consumption = coordinator.data.values()

    # Create sensor entities for each location
    entities = []
    for last_consumption in last_locations_consumption:
      location = last_consumption.get(LOCATION_KEY)
      entities.append(
          SmartHubEnergySensor(
              coordinator=coordinator,
              config_entry=config_entry,
              config=config,
              location=location,
          )
      )
      if getattr(location, "has_water", False):
          entities.append(
              SmartHubWaterSensor(
                  coordinator=coordinator,
                  config_entry=config_entry,
                  config=config,
                  location=location,
              )
          )

    async_add_entities(entities)
    _LOGGER.debug(f"{len(entities)} SmartHub sensor entities added successfully")


class SmartHubDataUpdateCoordinator(DataUpdateCoordinator):
    """Class to manage fetching SmartHub data."""

    def __init__(
        self,
        hass: HomeAssistant,
        api: SmartHubAPI,
        update_interval: timedelta,
        config_entry: str,
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{config_entry.entry_id}",
            update_interval=update_interval,
        )
        self.api = api
        self.account_id = config_entry.data.get('account_id','unknown')

    async def _async_update_data(self) -> Dict[str, Any]:
        """Fetch data from the SmartHub API."""
        try:
            _LOGGER.debug("Fetching data from SmartHub API")

            # force a logout of the session
            self.api.token = None

            locations = await self.api.get_service_locations()

            entity_response = {}

            for location in locations:
              # Because SmartHub provides historical usage/cost with delay of a
              # number of hours we need to insert data into statistics.
              await self._insert_statistics(location, Aggregation.HOURLY)
              await self._insert_statistics(location, Aggregation.DAILY)
              if getattr(location, "has_water", False):
                  for water_aggregation in (Aggregation.HOURLY, Aggregation.DAILY):
                      try:
                          await self._insert_water_statistics(location, water_aggregation)
                      except SmartHubAuthenticationError:
                          raise
                      except SmartHubAPIError:
                          _LOGGER.exception(
                              "Water %s import failed for location %s",
                              water_aggregation.label,
                              location.id,
                          )

              # Fetch monthly information for entity value
              first_day_of_current_month = datetime.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)

              data = await self.api.get_energy_data(location=location, start_datetime=first_day_of_current_month, aggregation=Aggregation.MONTHLY)

              if data.get("USAGE", None) is None or len(data.get("USAGE", None)) == 0:
                  _LOGGER.warning("No data received from SmartHub API for location %s", location)
                  # Return previous data if available, otherwise empty dict
                  entity = {
                    ENERGY_SENSOR_KEY: 0, # no data - no energy usage for the entity.
                    ATTR_LAST_READING_TIME: first_day_of_current_month.replace(tzinfo=ZoneInfo(self.api.timezone)), # use the TZ from the entity so it has consistent formating like 2026-02-01T00:00:00-05:00
                    LOCATION_KEY: location,
                    METER_NAME: data.get(METER_NAME, None)
                  }
              else:
                  last_reading = data.get("USAGE")[-1]
                  _LOGGER.debug("Successfully fetched data: %s for location: %s", last_reading, location)

                  entity = {
                    ENERGY_SENSOR_KEY: last_reading['consumption'],
                    ATTR_LAST_READING_TIME: last_reading['reading_time'],
                    LOCATION_KEY: location,
                    METER_NAME: data.get(METER_NAME, None)
                  }

              if getattr(location, "has_water", False):
                  try:
                      water_data = await self.api.get_water_data(
                          location=location,
                          start_datetime=first_day_of_current_month,
                          aggregation=Aggregation.MONTHLY,
                      )
                      entity.update(self._monthly_water_state(water_data, first_day_of_current_month))
                  except SmartHubAuthenticationError:
                      raise
                  except SmartHubAPIError:
                      _LOGGER.exception(
                          "Water monthly update failed for location %s",
                          location.id,
                      )

              entity_response[location.id] = entity

            return entity_response

        except SmartHubAuthenticationError as e:
            _LOGGER.error("Authentication error fetching SmartHub data: %s", e)
            # For auth errors, we want to raise UpdateFailed to trigger retry
            # but also ensure the API will refresh authentication on next attempt
            raise UpdateFailed(f"Authentication failed: {e}") from e
        except SmartHubAPIError as e:
            _LOGGER.error("Error fetching data from SmartHub API: %s", e)
            raise UpdateFailed(f"Error communicating with SmartHub API: {e}") from e
        except Exception as e:
            _LOGGER.exception("Unexpected error fetching SmartHub data: %s", e)
            raise UpdateFailed(f"Unexpected error: {e}") from e


    # https://github.com/tronikos/opower/ was used as a model for how to populate
    # hourly metrics when access to realtime information is not possible via
    # utility dashboards.
    async def _insert_statistics(self, location, aggregation: Aggregation):
        """Retrieve energy usage data asynchronously with retry logic. Always backfills the data overwriting the history based on the collection window."""
        consumption_statistic_id = f"{DOMAIN}:smarthub_energy_sensor{aggregation.suffix}_{self.account_id}_{location.id}"
        return_statistic_id = f"{DOMAIN}:smarthub_energy_return_sensor{aggregation.suffix}_{self.account_id}_{location.id}"

        consumption_unit_class = (
            EnergyConverter.UNIT_CLASS
        )
        consumption_unit = (
            UnitOfEnergy.KILO_WATT_HOUR
        )
        consumption_metadata = StatisticMetaData(
            mean_type=StatisticMeanType.NONE,
            has_sum=True,
            name=f"{location.provider} SmartHub Energy {aggregation.label} Usage - {self.account_id} - {location.description}",
            source=DOMAIN,
            statistic_id=consumption_statistic_id,
            unit_class=consumption_unit_class, # required in 2025.11
            unit_of_measurement=consumption_unit,
        )

        return_metadata = StatisticMetaData(
            mean_type=StatisticMeanType.NONE,
            has_sum=True,
            name=f"{location.provider} SmartHub Energy {aggregation.label} Return - {self.account_id} - {location.description}",
            source=DOMAIN,
            statistic_id=return_statistic_id,
            unit_class=consumption_unit_class, # required in 2025.11
            unit_of_measurement=consumption_unit,
        )

        last_stat = await get_instance(self.hass).async_add_executor_job(
            get_last_statistics, self.hass, 1, consumption_statistic_id, True, set()
        )
        _LOGGER.debug("last_stat for %s: %s", aggregation.label, last_stat)

        smarthub_data = {}
        if not last_stat:
            _LOGGER.debug("Updating %s statistic for the first time", aggregation.label)
            consumption_sum = 0.0
            return_sum      = 0.0
            last_stats_time = None

            # Initialize with last HISTORICAL_IMPORT_DAYS (usually 90) days of data
            start_datetime = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=HISTORICAL_IMPORT_DAYS)

            # Load read data for use in populating statistics
            smarthub_data = await self.api.get_energy_data(location=location, aggregation=aggregation, start_datetime=start_datetime)
        else:
            _LOGGER.debug("Checking if data migration is needed for %s...", aggregation.label)
            migrated = False
            # SmartHub doesn't hvae any current migrations - this sample code was left
            # from the opower version
            #migrated = await self._async_maybe_migrate_statistics(
            #    account.utility_account_id,
            #    {
            #        cost_statistic_id: compensation_statistic_id,
            #        consumption_statistic_id: return_statistic_id,
            #    },
            #    {
            #        cost_statistic_id: cost_metadata,
            #        compensation_statistic_id: compensation_metadata,
            #        consumption_statistic_id: consumption_metadata,
            #        return_metadata: return_metadata,
            #    },
            #)
            if migrated:
                # Skip update to avoid working on old data since the migration is done
                # asynchronously. Update the statistics in the next refresh in 12h.
                _LOGGER.debug(
                    "Statistics migration completed. Skipping update for now"
                )
                return

            # Update reads...
            # Load read data for use in populating statistics
            start_datetime = datetime.fromtimestamp(last_stat[consumption_statistic_id][0]["start"], tz=timezone.utc)

            # always backdate the start_datetime to ensure no gaps in recorded data
            start_datetime = start_datetime - timedelta(days=2)

            _LOGGER.debug("Fetching %s statistics from %s", aggregation.label, start_datetime)
            smarthub_data = await self.api.get_energy_data(location=location, start_datetime=start_datetime, aggregation=aggregation)

            if not smarthub_data or not smarthub_data.get("USAGE"):
              _LOGGER.warning("No data received from SmartHub API for location %s to populate historical %s stats", location, aggregation.label)
              # No new data to record in statatistics
              return

            start = smarthub_data.get("USAGE")[0].get("reading_time")
            _LOGGER.debug("Getting %s statistics at: %s", aggregation.label, start)

            # In the common case there should be a previous statistic at start time
            # so we only need to fetch one statistic. If there isn't any, fetch all.
            # Counterintutitively - but consistent with opower - this aligns the
            # last Stats collection with the data collected form the server - then imports
            # and overrights all the data after that point. The opower logic is that the
            # data might be refreshed, or have collection gaps that are fixed.
            # Its duplicated for SmartHub as it seems reasonable.
            for end in (start + timedelta(seconds=1), None):
                stats = await get_instance(self.hass).async_add_executor_job(
                    statistics_during_period,
                    self.hass,
                    start,
                    end,
                    {
                        consumption_statistic_id,
                        return_statistic_id,
                    },
                    aggregation.period,
                    None,
                    {"sum"},
                )
                if stats:
                    break
                if end:
                    _LOGGER.debug(
                        "Not found. Trying to find the oldest statistic after %s",
                        start,
                    )
            # We are in this code path only if get_last_statistics found a stat
            # so statistics_during_period should also have found at least one.
            assert stats

            def _safe_get_sum(records: list[Any]) -> float:
                if records and "sum" in records[0]:
                    return float(records[0]["sum"])
                return 0.0

            consumption_sum = _safe_get_sum(stats.get(consumption_statistic_id, []))
            return_sum    = _safe_get_sum(stats.get(return_statistic_id, []))
            last_stats_time = stats[consumption_statistic_id][0]["start"]

            _LOGGER.info(f"Updating %s statistics since %s", aggregation.label, last_stats_time)

        consumption_statistics = []
        return_statistics      = []

        for cost_read in smarthub_data.get("USAGE", []):
            start = cost_read.get("reading_time")
            if last_stats_time is not None and start.timestamp() <= last_stats_time:
                continue

            consumption_state = max(0, cost_read.get("consumption"))
            consumption_sum += consumption_state

            consumption_statistics.append(
                StatisticData(
                    start=start, state=consumption_state, sum=consumption_sum
                )
            )

        for return_read in smarthub_data.get("USAGE_RETURN", []):
            start = return_read.get("reading_time")
            if last_stats_time is not None and start.timestamp() <= last_stats_time:
                continue

            return_state = max(0, return_read.get("consumption"))
            return_sum += return_state

            return_statistics.append(
                StatisticData(
                    start=start, state=return_state, sum=return_sum
                )
            )

        # If the location description is blank, use the meter name instead.
        if location.description == "":
          consumption_metadata["name"]=f"{location.provider} SmartHub Energy {aggregation.label} Usage - {self.account_id} - {smarthub_data.get(METER_NAME, None)}"
          return_metadata["name"]=f"{location.provider} SmartHub Energy {aggregation.label} Return - {self.account_id} - {smarthub_data.get(METER_NAME, None)}"

        _LOGGER.info(
            "Adding %s statistics for %s",
            len(consumption_statistics),
            consumption_statistic_id,
        )
        async_add_external_statistics(
            self.hass, consumption_metadata, consumption_statistics
        )

        if "USAGE_RETURN" in smarthub_data:
          _LOGGER.info(
            "Adding %s return statistics for %s",
            len(return_statistics),
            return_statistic_id,
          )
          async_add_external_statistics(
            self.hass, return_metadata, return_statistics
          )

    def _monthly_water_state(self, water_data, first_day: datetime) -> Dict[str, Any]:
        """Monthly water sensor state, matching the empty-month behavior of electricity."""
        if not water_data or not water_data.get("USAGE"):
            return {
                WATER_SENSOR_KEY: 0,
                ATTR_WATER_LAST_READING_TIME: first_day.replace(tzinfo=ZoneInfo(self.api.timezone)),
            }
        last_reading = water_data["USAGE"][-1]
        state = {
            WATER_SENSOR_KEY: last_reading["consumption"],
            ATTR_WATER_LAST_READING_TIME: last_reading["reading_time"],
        }
        meter_name = water_data.get(METER_NAME)
        if meter_name:
            state["water_meter_name"] = meter_name
        return state

    async def _insert_water_statistics(self, location, aggregation: Aggregation):
        """Import water with the same 90-day first run and two-day refresh as electricity.

        Hourly water is requested in 30-day windows. Daily water stays one request.
        """
        statistic_id = f"{DOMAIN}:smarthub_water_sensor{aggregation.suffix}_{self.account_id}_{location.id}"
        metadata = StatisticMetaData(
            mean_type=StatisticMeanType.NONE,
            has_sum=True,
            name=f"{location.provider} SmartHub Water {aggregation.label} Usage - {self.account_id} - {location.description}",
            source=DOMAIN,
            statistic_id=statistic_id,
            unit_class=VolumeConverter.UNIT_CLASS,
            unit_of_measurement=UnitOfVolume.CUBIC_FEET,
        )

        last_stat = await get_instance(self.hass).async_add_executor_job(
            get_last_statistics, self.hass, 1, statistic_id, True, set()
        )
        _LOGGER.debug("last water stat: %s", last_stat)

        if not last_stat:
            _LOGGER.debug("Updating water statistic for the first time")
            consumption_sum = 0.0
            last_stats_time = None
            start_datetime = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=HISTORICAL_IMPORT_DAYS)
            smarthub_data = await self._fetch_water_usage(location, aggregation, start_datetime)
        else:
            start_datetime = datetime.fromtimestamp(last_stat[statistic_id][0]["start"], tz=timezone.utc)
            start_datetime = start_datetime - timedelta(days=2)
            _LOGGER.debug("Fetching water statistics from %s", start_datetime)
            smarthub_data = await self._fetch_water_usage(location, aggregation, start_datetime)
            if not smarthub_data or not smarthub_data.get("USAGE"):
                _LOGGER.warning("No water data received for location %s", location)
                return

            start = smarthub_data.get("USAGE")[0].get("reading_time")
            for end in (start + timedelta(seconds=1), None):
                stats = await get_instance(self.hass).async_add_executor_job(
                    statistics_during_period,
                    self.hass,
                    start,
                    end,
                    {statistic_id},
                    aggregation.period,
                    None,
                    {"sum"},
                )
                if stats:
                    break
            assert stats

            records = stats.get(statistic_id, [])
            consumption_sum = float(records[0]["sum"]) if records and "sum" in records[0] else 0.0
            last_stats_time = stats[statistic_id][0]["start"]

        usage = dedupe_water_readings((smarthub_data or {}).get("USAGE", []))
        statistics = []
        for reading in usage:
            start = reading.get("reading_time")
            if last_stats_time is not None and start.timestamp() <= last_stats_time:
                continue
            consumption_state = max(0, reading.get("consumption"))
            consumption_sum += consumption_state
            statistics.append(
                StatisticData(start=start, state=consumption_state, sum=consumption_sum)
            )

        if location.description == "":
            metadata["name"] = (
                f"{location.provider} SmartHub Water {aggregation.label} Usage - "
                f"{self.account_id} - {(smarthub_data or {}).get(METER_NAME, None)}"
            )

        _LOGGER.info("Adding %s water statistics for %s", len(statistics), statistic_id)
        async_add_external_statistics(self.hass, metadata, statistics)

    async def _fetch_water_usage(self, location, aggregation: Aggregation, start_datetime: datetime):
        """Load water usage. Hourly requests are split into portal-sized windows."""
        if aggregation != Aggregation.HOURLY:
            return await self.api.get_water_data(
                location=location, aggregation=aggregation, start_datetime=start_datetime
            )

        usage = []
        meter_name = None
        end_datetime = _water_request_end(start_datetime)
        for window_start, window_end in water_hourly_windows(start_datetime, end_datetime):
            chunk = await self.api.get_water_data(
                location=location,
                aggregation=aggregation,
                start_datetime=window_start,
                end_datetime=window_end,
            )
            if not chunk:
                continue
            if chunk.get(METER_NAME):
                meter_name = chunk[METER_NAME]
            usage.extend(chunk.get("USAGE") or [])
        usage.sort(key=lambda reading: reading.get("reading_time"))

        result = {"USAGE": usage}
        if meter_name:
            result[METER_NAME] = meter_name
        return result


def _water_request_end(start: datetime) -> datetime:
    """End of a water request, truncated to the minute like the usage poll."""
    if start.tzinfo is None:
        return datetime.now().replace(minute=0, second=0, microsecond=0)
    return datetime.now(start.tzinfo).replace(minute=0, second=0, microsecond=0)


def water_hourly_windows(start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
    """Split an hourly water span into windows of at most 30 days."""
    windows = []
    cursor = start
    while cursor < end:
        chunk_end = cursor + timedelta(days=WATER_HOURLY_REQUEST_DAYS)
        if chunk_end > end:
            chunk_end = end
        if chunk_end <= cursor:
            break
        windows.append((cursor, chunk_end))
        cursor = chunk_end
    return windows


def dedupe_water_readings(usage: list) -> list:
    """Drop misaligned points and keep one reading per source timestamp.

    A repeated timestamp uses the later value so it is not added into the sum twice.
    """
    deduped = []
    index = {}
    for reading in usage:
        start = reading.get("reading_time")
        if (
            start is None
            or getattr(start, "tzinfo", None) is None
            or start.utcoffset() is None
            or start.minute != 0
            or start.second != 0
            or start.microsecond != 0
        ):
            _LOGGER.warning("Skipping water reading that is not on an hour boundary")
            continue
        key = reading.get("raw_timestamp", int(start.timestamp() * 1000))
        if key in index:
            deduped[index[key]] = reading
        else:
            index[key] = len(deduped)
            deduped.append(reading)
    return deduped


class SmartHubEnergySensor(CoordinatorEntity, SensorEntity):
    """Representation of a SmartHub energy sensor."""

    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_icon = "mdi:lightning-bolt"

    def __init__(
        self,
        coordinator: SmartHubDataUpdateCoordinator,
        config_entry: ConfigEntry,
        config: Dict[str, Any],
        location: SmartHubLocation,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)

        self._config_entry = config_entry
        self._config = config
        self._attr_unique_id = f"{config_entry.unique_id}_{location.id}_energy"
        self.location = location

        # Extract account info for naming
        account_id = config.get("account_id", "Unknown")

        self._attr_name = f"{self.location.provider} SmartHub Energy Monthly Usage - {account_id} {self.location.description}"

        _LOGGER.debug("Initialized SmartHub energy sensor with unique_id: %s", self._attr_unique_id)

    @property
    def available(self) -> bool:
        """Return True if entity is available."""
        return self.coordinator.last_update_success and self.native_value is not None

    @property
    def native_value(self) -> Optional[float]:
        """Return the state of the sensor."""
        if not self.coordinator.data:
            return None

        value = self.coordinator.data.get(self.location.id, {}).get(ENERGY_SENSOR_KEY, None)
        if value is None:
            _LOGGER.debug("No energy usage value found in coordinator data")
            return None

        try:
            return float(value)
        except (ValueError, TypeError) as e:
            _LOGGER.warning("Could not convert energy value '%s' to float: %s", value, e)
            return None

    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        """Return additional state attributes."""
        attributes = {
            ATTR_ACCOUNT_ID: self._config.get("account_id"),
            ATTR_LOCATION_ID: self.location.id,
        }

        # Add last reading time & meter name if available
        if self.coordinator.data:
            last_reading = self.coordinator.data.get(self.location.id).get(ATTR_LAST_READING_TIME)
            if last_reading:
                attributes[ATTR_LAST_READING_TIME] = last_reading

            meter_name = self.coordinator.data.get(self.location.id).get(METER_NAME)
            if meter_name:
                attributes[METER_NAME] = meter_name

        return attributes

    @property
    def device_info(self) -> Dict[str, Any]:
        """Return device information."""
        account_id = self._config.get("account_id", "Unknown")
        host = self._config.get("host", "Unknown")

        return {
            "identifiers": {(DOMAIN, self._config_entry.unique_id or self._config_entry.entry_id)},
            "name": f"{self.location.provider} SmartHub Energy Monthly Usage ({account_id} - {self.location.description})",
            "manufacturer": "SmartHub Coop",
            "model": "Energy Monitor",
            "configuration_url": f"https://{host}",
        }


class SmartHubWaterSensor(CoordinatorEntity, SensorEntity):
    """Monthly water consumption for a location that also has electricity."""

    _attr_device_class = SensorDeviceClass.WATER
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = UnitOfVolume.CUBIC_FEET
    _attr_icon = "mdi:water"

    def __init__(
        self,
        coordinator: SmartHubDataUpdateCoordinator,
        config_entry: ConfigEntry,
        config: Dict[str, Any],
        location: SmartHubLocation,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._config_entry = config_entry
        self._config = config
        self.location = location
        self._attr_unique_id = f"{config_entry.unique_id}_{location.id}_water"
        account_id = config.get("account_id", "Unknown")
        self._attr_name = (
            f"{location.provider} SmartHub Water Monthly Usage - {account_id} {location.description}"
        )

    @property
    def available(self) -> bool:
        """Return True if entity is available."""
        return self.coordinator.last_update_success and self.native_value is not None

    @property
    def native_value(self) -> Optional[float]:
        """Return the current monthly water consumption."""
        if not self.coordinator.data:
            return None
        value = self.coordinator.data.get(self.location.id, {}).get(WATER_SENSOR_KEY)
        if value is None:
            return None
        try:
            return float(value)
        except (ValueError, TypeError):
            return None

    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        """Return additional state attributes."""
        attributes = {
            ATTR_ACCOUNT_ID: self._config.get("account_id"),
            ATTR_LOCATION_ID: self.location.id,
        }
        location_data = (self.coordinator.data or {}).get(self.location.id) or {}
        if location_data.get(ATTR_WATER_LAST_READING_TIME):
            attributes[ATTR_WATER_LAST_READING_TIME] = location_data[ATTR_WATER_LAST_READING_TIME]
        if location_data.get("water_meter_name"):
            attributes[METER_NAME] = location_data["water_meter_name"]
        return attributes

    @property
    def device_info(self) -> Dict[str, Any]:
        """Return device information for the shared service location."""
        account_id = self._config.get("account_id", "Unknown")
        host = self._config.get("host", "Unknown")
        return {
            "identifiers": {(DOMAIN, self._config_entry.unique_id or self._config_entry.entry_id)},
            "name": f"{self.location.provider} SmartHub Energy Monthly Usage ({account_id} - {self.location.description})",
            "manufacturer": "SmartHub Coop",
            "model": "Energy Monitor",
            "configuration_url": f"https://{host}",
        }
