"""Config flow for Dreame Vacuum Camera Capture."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResult
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import DreameCaptureApiError, DreameCaptureAuthError, DreameCaptureClient
from .const import (
    CONF_ACCOUNT_COUNTRY,
    CONF_ACCOUNT_PASSWORD,
    CONF_ACCOUNT_USERNAME,
    CONF_API_TOKEN,
    CONF_DEVICES,
    CONF_FOUR_DIGIT_CODE,
    CONF_HOST,
    CONF_PORT,
    COUNTRY_LIST,
    DEFAULT_PORT,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)


class DreameCaptureConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Dreame Vacuum Camera Capture."""

    VERSION = 1

    def __init__(self) -> None:
        self._account_data: dict[str, Any] = {}
        self._devices: list[dict[str, Any]] = []

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            session = async_get_clientsession(self.hass)
            client = DreameCaptureClient(
                session, user_input[CONF_HOST], user_input[CONF_PORT], user_input[CONF_API_TOKEN],
            )
            try:
                devices = await client.list_devices(
                    user_input[CONF_ACCOUNT_USERNAME],
                    user_input[CONF_ACCOUNT_PASSWORD],
                    user_input[CONF_ACCOUNT_COUNTRY],
                )
            except DreameCaptureAuthError:
                errors["base"] = "invalid_token"
            except DreameCaptureApiError:
                _LOGGER.exception("Failed to reach the add-on or log in")
                errors["base"] = "cannot_connect"
            else:
                if not devices:
                    errors["base"] = "no_devices"
                else:
                    self._account_data = dict(user_input)
                    self._devices = devices
                    return await self.async_step_devices()

        data_schema = vol.Schema(
            {
                vol.Required(CONF_HOST, default=(user_input or {}).get(CONF_HOST, "localhost")): str,
                vol.Required(CONF_PORT, default=(user_input or {}).get(CONF_PORT, DEFAULT_PORT)): int,
                vol.Required(CONF_API_TOKEN, default=(user_input or {}).get(CONF_API_TOKEN, "")): str,
                vol.Required(
                    CONF_ACCOUNT_USERNAME, default=(user_input or {}).get(CONF_ACCOUNT_USERNAME, "")
                ): str,
                vol.Required(CONF_ACCOUNT_PASSWORD): str,
                vol.Required(
                    CONF_ACCOUNT_COUNTRY, default=(user_input or {}).get(CONF_ACCOUNT_COUNTRY, "eu")
                ): vol.In(COUNTRY_LIST),
                vol.Required(CONF_FOUR_DIGIT_CODE): str,
            }
        )
        return self.async_show_form(step_id="user", data_schema=data_schema, errors=errors)

    async def async_step_devices(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        if user_input is not None:
            selected_dids = set(user_input[CONF_DEVICES])
            selected = [d for d in self._devices if d["did"] in selected_dids]
            data = {**self._account_data, CONF_DEVICES: selected}
            return self.async_create_entry(title="Dreame Vacuum Camera Capture", data=data)

        options = {d["did"]: f"{d['name']} ({d['mac']})" for d in self._devices}
        data_schema = vol.Schema(
            {vol.Required(CONF_DEVICES, default=list(options)): cv.multi_select(options)}
        )
        return self.async_show_form(step_id="devices", data_schema=data_schema)
