"""Take-photo button for Dreame Vacuum Camera Capture."""
from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity
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

    async_add_entities(DreameCaptureSnapshotButton(client, entry, device) for device in data[CONF_DEVICES])


class DreameCaptureSnapshotButton(ButtonEntity):
    """Runs a one-shot activation -> grab one frame -> tear down cycle."""

    _attr_has_entity_name = True
    _attr_name = "Take Photo"

    def __init__(self, client: DreameCaptureClient, entry: ConfigEntry, device: dict) -> None:
        self._client = client
        self._entry = entry
        self._did = device["did"]
        self._attr_unique_id = f"{entry.entry_id}_{self._did}_snapshot"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, self._did)},
            "name": device["name"],
            "manufacturer": "Dreame",
        }

    async def async_press(self) -> None:
        entry = self._entry
        try:
            await self._client.capture(
                entry.data[CONF_ACCOUNT_USERNAME],
                entry.data[CONF_ACCOUNT_PASSWORD],
                entry.data[CONF_ACCOUNT_COUNTRY],
                entry.data[CONF_FOUR_DIGIT_CODE],
                self._did,
            )
        except DreameCaptureApiError:
            _LOGGER.warning("Failed to capture snapshot for %s", self._did)
