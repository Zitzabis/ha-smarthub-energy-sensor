"""Simple config flow for SmartHub integration."""
import voluptuous as vol
from homeassistant import config_entries
from .const import (
  DOMAIN,
  DEFAULT_POLL_INTERVAL,
  HISTORICAL_IMPORT_DAYS,
  CONF_EMAIL,
  CONF_PASSWORD,
  CONF_ACCOUNT_ID,
  CONF_LOCATION_ID,
  CONF_HOST,
  CONF_POLL_INTERVAL,
  CONF_TIMEZONE,
  CONF_MFA_TOTP,
  CONF_HISTORY_START,
  MIN_POLL_INTERVAL,
  MAX_POLL_INTERVAL
)
from .api import SmartHubAPI
from .exceptions import SmartHubAuthenticationError, SmartHubConnectionError

from typing import Any
from types import MappingProxyType
from datetime import date, datetime, timedelta
import zoneinfo
import logging
from homeassistant.helpers.selector import (
    DateSelector,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,

    TextSelector,
    TextSelectorConfig,
    TextSelectorType
)

_LOGGER = logging.getLogger(__name__)


def _history_start_error(value: Any) -> str | None:
    """Return a config-flow error key when the earliest import day is unusable."""
    if isinstance(value, datetime):
        selected = value.date()
    elif isinstance(value, date):
        selected = value
    elif isinstance(value, str):
        try:
            selected = date.fromisoformat(value)
        except ValueError:
            return "invalid_history_start"
    else:
        return "invalid_history_start"
    if selected > date.today():
        return "future_history_start"
    return None


def _history_start_iso(value: Any) -> str:
    """Store the earliest import day as YYYY-MM-DD."""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return date.fromisoformat(str(value)).isoformat()


class SmartHubConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for SmartHub."""

    VERSION = 1
    MINOR_VERSION = 1 # Updated to handle addition of TOTP, and TimeZone

    async def async_step_user(self, user_input=None) -> config_entries.ConfigFlowResult:
        """Handle the initial step."""
        errors = {}
        if user_input is not None:
            history_error = _history_start_error(user_input.get(CONF_HISTORY_START))
            if history_error:
                errors[CONF_HISTORY_START] = history_error
            else:
                user_input[CONF_HISTORY_START] = _history_start_iso(user_input[CONF_HISTORY_START])
                try:
                    await self._validate_input(user_input)
                except SmartHubAuthenticationError:
                    errors["base"] = "invalid_auth"
                except SmartHubConnectionError:
                    errors["base"] = "cannot_connect"
                except Exception:  # pylint: disable=broad-except
                    _LOGGER.exception("Unexpected exception")
                    errors["base"] = "unknown"
                else:
                    if self.source == config_entries.SOURCE_RECONFIGURE:
                        return self.async_update_reload_and_abort(
                            self._get_reconfigure_entry(), data_updates=user_input
                        )
                    # else - create a new entry
                    return self.async_create_entry(
                        title="SmartHub",
                        data=user_input,
                    )

        schema_values: dict[str, Any] | MappingProxyType[str, Any] = {}
        if user_input is not None and errors:
            schema_values = user_input
        elif self.source == config_entries.SOURCE_RECONFIGURE:
            schema_values = self._get_reconfigure_entry().data

        timezones = await self.hass.async_add_executor_job(
                zoneinfo.available_timezones
            )
        suggested_history_start = (date.today() - timedelta(days=HISTORICAL_IMPORT_DAYS)).isoformat()
        schema = vol.Schema(
            {
               vol.Required(CONF_EMAIL): str,
               vol.Required(CONF_PASSWORD): str,
               vol.Required(CONF_ACCOUNT_ID): str,
               vol.Required(CONF_HOST): str,
               vol.Required(CONF_TIMEZONE, default="GMT"): SelectSelector(
                SelectSelectorConfig(
                  options=list(timezones), mode=SelectSelectorMode.DROPDOWN, sort=True
                )
               ),
               vol.Optional(CONF_MFA_TOTP): TextSelector(
                 TextSelectorConfig(
                   type=TextSelectorType.PASSWORD
                 )
               ),
               vol.Required(CONF_POLL_INTERVAL, default=DEFAULT_POLL_INTERVAL): vol.All(vol.Coerce(int), vol.Range(min=MIN_POLL_INTERVAL, max=MAX_POLL_INTERVAL)),
               vol.Required(CONF_HISTORY_START, default=suggested_history_start): DateSelector(),
            }
        )

        # Show basic form
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                schema,
                schema_values,
            ),
            errors=errors
        )

    async def _validate_input(self, data: dict[str, Any]) -> None:
        """Validate the user input allows us to connect.

        Data has the keys from the schema with values provided by the user.
        """
        hub = SmartHubAPI(
            email=data["email"],
            password=data["password"],
            account_id=data["account_id"],
            timezone=data["timezone"],
            mfa_totp=data.get("mfa_totp", ""),
            host=data["host"],
        )

        try:
            await hub.get_token()
        finally:
            await hub.close()


    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> config_entries.ConfigFlowResult:
        """Handle a reconfiguration config flow initialized by the user."""
        return await self.async_step_user(user_input)
