"""Camera entity backed by the companion add-on.

`async_camera_image` only serves whatever snapshot the add-on already has, so
rendering a thumbnail never triggers a device-side stream. `stream_source` is
what actually starts one - Home Assistant calls it lazily when someone opens
the live view, and the add-on keeps the feed alive from there.
"""
from __future__ import annotations

import logging

from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_CAMERA_PIN, CONF_COUNTRY, CONF_PASSWORD, CONF_USERNAME, DOMAIN
from .coordinator import DreameCoordinator
from .entity import DreameEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: DreameCoordinator = hass.data[DOMAIN][entry.entry_id]
    if not coordinator.companion:
        return  # camera is opt-in and needs the add-on
    async_add_entities([DreameCamera(coordinator)])


class DreameCamera(DreameEntity, Camera):
    _attr_name = "Camera"
    _attr_supported_features = CameraEntityFeature.STREAM

    def __init__(self, coordinator: DreameCoordinator) -> None:
        DreameEntity.__init__(self, coordinator, "camera")
        Camera.__init__(self)

    async def async_camera_image(self, width: int | None = None, height: int | None = None) -> bytes | None:
        return await self.coordinator.companion.async_latest_image(self.coordinator.did)

    async def stream_source(self) -> str | None:
        c = self.coordinator
        data = c.entry.data
        return await c.companion.async_stream_start(
            data[CONF_USERNAME],
            data[CONF_PASSWORD],
            data.get(CONF_COUNTRY, "eu"),
            data.get(CONF_CAMERA_PIN, ""),
            c.did,
        )
