"""Camera entity backed by the companion add-on.

Read-only, by design. `async_camera_image` serves whatever snapshot the add-on
already has and `stream_source` attaches to a stream only if one is already
running, so nothing here can open a camera session on the vacuum. Starting one
is always an explicit act: the Stream switch, or the snapshot button.
"""
from __future__ import annotations

import logging

from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
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
        """Attach to a running stream. Never starts one.

        Home Assistant calls this whenever it wants the feed - opening the
        live view, rendering a dashboard card, camera.record, a WebRTC
        negotiation - none of which is an explicit request to point the camera
        at your house. Starting a session here also meant the vacuum could be
        occupied for `stream_timeout_minutes` after a card scrolled past,
        locking the phone app out.

        The Stream switch is the only thing that opens a session.
        """
        return await self.coordinator.companion.async_stream_url(self.coordinator.did)
