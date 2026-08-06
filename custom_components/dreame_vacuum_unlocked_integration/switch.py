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
    async_add_entities([DreameStreamSwitch(coordinator), DreameSpeakSwitch(coordinator)])


class DreameStreamSwitch(DreameEntity, SwitchEntity):
    _attr_name = "Stream"
    _attr_icon = "mdi:video"
    _attr_should_poll = True

    def __init__(self, coordinator: DreameCoordinator) -> None:
        super().__init__(coordinator, "stream")
        self._running: bool | None = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        # Polling only starts one SCAN_INTERVAL after the entity is added, so
        # without this the switch sits unavailable for 30s after every restart.
        await self.async_update()
        self.async_write_ha_state()

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

        # Logged unconditionally: when the toggle springs back, the first
        # thing to establish is whether this handler ran at all. The add-on's
        # log can't answer that - a call that fails here never reaches it.
        _LOGGER.info("Stream switch: starting the stream for %s", c.device_name)

        missing = [k for k in (CONF_USERNAME, CONF_PASSWORD) if not cfg.get(k)]
        if missing:
            _LOGGER.error(
                "Cannot start the stream for %s: the config entry has no %s. "
                "Re-add the integration to restore it",
                c.device_name,
                " or ".join(missing),
            )
            return

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
        _LOGGER.info("Stream switch: stopping the stream for %s", self.coordinator.device_name)
        if await self.coordinator.companion.async_stream_stop(self.coordinator.did):
            self._running = False
            self.async_write_ha_state()
        else:
            # Silence here is what makes this look like "the toggle springs
            # back on by itself": HA re-reads the state after the call and
            # finds the stream still running, with nothing in the log.
            _LOGGER.warning(
                "Could not stop the stream for %s - the add-on did not confirm it",
                self.coordinator.device_name,
            )


class DreameSpeakSwitch(DreameEntity, SwitchEntity):
    """The vacuum-mic (intercom) audio layer on a RUNNING stream.

    This is the audio-out toggle: turning it on ensures the video stream is
    running (starts it if not), then arms the vacuum's mic so its live mic
    audio flows into the stream's RTSP and `play_audio_clip` (talk) is
    enabled. Turning it off disarms the mic (audio leaves the RTSP, talk
    disabled) but the video stream KEEPS running - mirroring the app's camera
    view where tap-to-talk is a separate control on top of the live feed."""

    _attr_name = "Intercom"
    _attr_icon = "mdi:microphone"
    _attr_should_poll = True

    def __init__(self, coordinator: DreameCoordinator) -> None:
        super().__init__(coordinator, "speak")
        self._running: bool | None = None
        self._rtsp_url: str | None = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        await self.async_update()
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        return self._running is not None

    @property
    def is_on(self) -> bool:
        return bool(self._running)

    @property
    def extra_state_attributes(self) -> dict[str, str | None]:
        return {"rtsp_url": self._rtsp_url}

    async def async_update(self) -> None:
        state = await self.coordinator.companion.async_stream_state(
            self.coordinator.did
        )
        if state is None:
            return
        self._running = bool(state.get("intercom_armed"))
        self._rtsp_url = state.get("rtsp_url")

    async def async_turn_on(self, **kwargs) -> None:
        c = self.coordinator
        cfg = c.config

        _LOGGER.info("Intercom switch: arming vacuum mic for %s", c.device_name)

        missing = [k for k in (CONF_USERNAME, CONF_PASSWORD) if not cfg.get(k)]
        if missing:
            _LOGGER.error(
                "Cannot arm the intercom for %s: the config entry has no %s. "
                "Re-add the integration to restore it",
                c.device_name,
                " or ".join(missing),
            )
            return

        # Intercom is a layer on the video stream - make sure the stream is up
        # first (auto-start video per the user's model).
        running = await c.companion.async_stream_status(c.did)
        if not running:
            _LOGGER.info(
                "Intercom switch: starting video stream for %s first",
                c.device_name,
            )
            self._rtsp_url = await c.companion.async_stream_start(
                cfg[CONF_USERNAME],
                cfg[CONF_PASSWORD],
                cfg.get(CONF_COUNTRY, "eu"),
                cfg.get(CONF_CAMERA_PIN, ""),
                c.did,
            )

        armed = await c.companion.async_stream_intercom(c.did, True)
        if armed is None:
            _LOGGER.warning("Could not arm the intercom for %s", c.device_name)
            return
        self._running = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs) -> None:
        _LOGGER.info("Intercom switch: disarming vacuum mic for %s", self.coordinator.device_name)
        # Only remove the audio layer - the video stream keeps running.
        await self.coordinator.companion.async_stream_intercom(self.coordinator.did, False)
        self._running = False
        self.async_write_ha_state()
