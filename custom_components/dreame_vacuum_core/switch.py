"""Stream switch.

Explicit start/stop for the RTSP feed, independent of the camera entity's
lazy behaviour. The camera only streams while someone has the live view open;
this keeps it running - which is what automations need ("start streaming when
motion is detected", "stream while I'm away").

Turning it off releases the device: the vacuum only supports one camera
session at a time, so leaving a stream running blocks the phone app.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_CAMERA_PIN, CONF_COUNTRY, CONF_PASSWORD, CONF_USERNAME, DOMAIN
from .coordinator import DreameCoordinator
from .entity import DreameEntity

_LOGGER = logging.getLogger(__name__)

# Stream state lives in the add-on, not the device, so it isn't part of the
# coordinator's property poll and needs its own (gentle) refresh.
SCAN_INTERVAL = timedelta(seconds=30)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: DreameCoordinator = hass.data[DOMAIN][entry.entry_id]
    if not coordinator.companion:
        return  # needs the add-on
    async_add_entities([DreameStreamSwitch(coordinator)])


class DreameStreamSwitch(DreameEntity, SwitchEntity):
    _attr_name = "Stream"
    _attr_icon = "mdi:video"
    _attr_should_poll = True

    def __init__(self, coordinator: DreameCoordinator) -> None:
        super().__init__(coordinator, "stream")
        self._running: bool | None = None

    @property
    def available(self) -> bool:
        # Unknown means the add-on didn't answer - better to show unavailable
        # than to imply the stream is off.
        return self._running is not None

    @property
    def is_on(self) -> bool:
        return bool(self._running)

    async def async_update(self) -> None:
        self._running = await self.coordinator.companion.async_stream_status(
            self.coordinator.did
        )

    async def async_turn_on(self, **kwargs) -> None:
        c = self.coordinator
        cfg = c.config
        # Blocks until the feed is actually publishing, so the switch doesn't
        # report on before anything can read the stream.
        url = await c.companion.async_stream_start(
            cfg[CONF_USERNAME],
            cfg[CONF_PASSWORD],
            cfg.get(CONF_COUNTRY, "eu"),
            cfg.get(CONF_CAMERA_PIN, ""),
            c.did,
        )
        if url:
            self._running = True
            self.async_write_ha_state()
        else:
            _LOGGER.warning("Could not start the stream for %s", c.device_name)

    async def async_turn_off(self, **kwargs) -> None:
        if await self.coordinator.companion.async_stream_stop(self.coordinator.did):
            self._running = False
            self.async_write_ha_state()
