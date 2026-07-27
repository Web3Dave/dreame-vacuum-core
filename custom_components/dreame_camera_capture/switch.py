"""Stream on/off switch for Dreame Vacuum Camera Capture."""
from __future__ import annotations

import logging

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import DreameCaptureApiError, DreameCaptureClient
from .const import (
    CONF_ACCOUNT_COUNTRY,
    CONF_ACCOUNT_PASSWORD,
    CONF_ACCOUNT_USERNAME,
    CONF_API_TOKEN,
    CONF_DEVICES,
    CONF_FOUR_DIGIT_CODE,
    CONF_HOST,
    CONF_PORT,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    data = entry.data
    session = async_get_clientsession(hass)
    client = DreameCaptureClient(session, data[CONF_HOST], data[CONF_PORT], data[CONF_API_TOKEN])

    async_add_entities(DreameCaptureStreamSwitch(client, entry, device) for device in data[CONF_DEVICES])


class DreameCaptureStreamSwitch(SwitchEntity):
    """Explicit start/stop trigger for a device's RTSP stream.

    Distinct from opening the camera's live view (which starts the stream
    implicitly) - this gives an explicit, dashboard-controllable on/off.
    """

    _attr_has_entity_name = True
    _attr_name = "Stream"
    _attr_should_poll = True

    def __init__(self, client: DreameCaptureClient, entry: ConfigEntry, device: dict) -> None:
        self._client = client
        self._entry = entry
        self._did = device["did"]
        self._attr_unique_id = f"{entry.entry_id}_{self._did}_stream"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, self._did)},
            "name": device["name"],
            "manufacturer": "Dreame",
        }
        self._attr_is_on = False

    async def async_update(self) -> None:
        try:
            self._attr_is_on = await self._client.stream_status(self._did)
        except DreameCaptureApiError:
            _LOGGER.warning("Failed to fetch stream status for %s", self._did)

    async def async_turn_on(self, **kwargs) -> None:
        entry = self._entry
        await self._client.stream_start(
            entry.data[CONF_ACCOUNT_USERNAME],
            entry.data[CONF_ACCOUNT_PASSWORD],
            entry.data[CONF_ACCOUNT_COUNTRY],
            entry.data[CONF_FOUR_DIGIT_CODE],
            self._did,
        )
        self._attr_is_on = True

    async def async_turn_off(self, **kwargs) -> None:
        await self._client.stream_stop(self._did)
        self._attr_is_on = False
