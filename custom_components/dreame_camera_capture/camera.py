"""Camera entities for Dreame Vacuum Camera Capture."""
from __future__ import annotations

import logging

from homeassistant.components.camera import Camera, CameraEntityFeature
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

    async_add_entities(DreameCaptureCamera(client, entry, device) for device in data[CONF_DEVICES])


class DreameCaptureCamera(Camera):
    """Camera entity backed by the Dreame Vacuum Camera Capture add-on.

    `async_camera_image` only serves the last snapshot the add-on already has
    (cheap - no activation sequence). `stream_source` is what actually starts
    the real device-side stream, triggered lazily by HA when a client opens
    the live view.
    """

    _attr_has_entity_name = True
    _attr_name = "Camera"
    _attr_supported_features = CameraEntityFeature.STREAM

    def __init__(self, client: DreameCaptureClient, entry: ConfigEntry, device: dict) -> None:
        super().__init__()
        self._client = client
        self._entry = entry
        self._did = device["did"]
        self._attr_unique_id = f"{entry.entry_id}_{self._did}_camera"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, self._did)},
            "name": device["name"],
            "manufacturer": "Dreame",
        }

    async def async_camera_image(self, width: int | None = None, height: int | None = None) -> bytes | None:
        try:
            return await self._client.get_latest_jpg(self._did)
        except DreameCaptureApiError:
            _LOGGER.warning("Failed to fetch latest snapshot for %s", self._did)
            return None

    async def stream_source(self) -> str | None:
        entry = self._entry
        try:
            return await self._client.stream_start(
                entry.data[CONF_ACCOUNT_USERNAME],
                entry.data[CONF_ACCOUNT_PASSWORD],
                entry.data[CONF_ACCOUNT_COUNTRY],
                entry.data[CONF_FOUR_DIGIT_CODE],
                self._did,
            )
        except DreameCaptureApiError:
            _LOGGER.warning("Failed to start stream for %s", self._did)
            return None
