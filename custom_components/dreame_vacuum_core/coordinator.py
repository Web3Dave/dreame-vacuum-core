"""State coordination: push-first, poll to reconcile.

Design notes worth keeping in mind when extending this:

* MQTT `properties_changed` is the primary state path. Polling exists to
  reconcile missed pushes, not as the main channel - so the interval adapts
  rather than sitting at a fixed rate.
* The device keep-alive (siid 14/piid 4) is mandatory. The device stops
  sending non-essential data when it lapses, so it runs on its own ~25s timer
  independent of polling.
* Property presence is discovered by probing, because no shipped manifest
  lists it (see profile.py). Probe results are cached for the session.
* All blocking transport work goes through the executor - `transport/` is
  synchronous, vendored code and must never run on the event loop.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import random
import time
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryNotReady,
    HomeAssistantError,
)
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from datetime import timedelta

from .camera_session import CameraSession
from .companion import CompanionClient
from .const import (
    CONF_CAMERA_PIN,
    CONF_COMPANION_HOST,
    CONF_COMPANION_PORT,
    CONF_COMPANION_TOKEN,
    CONF_COUNTRY,
    CONF_DID,
    CONF_ENABLE_CAMERA,
    CONF_MODEL,
    CONF_NAME,
    CONF_PASSWORD,
    CONF_USERNAME,
    DEFAULT_COMPANION_PORT,
    DOMAIN,
    KEEP_ALIVE_INTERVAL,
    PIID_DEVICE_KEEP_ALIVE,
    POLL_ACTIVE,
    POLL_FAIL_LONG,
    POLL_FAIL_SHORT,
    POLL_IDLE,
    PROPERTY_BATCH_SIZE,
    SIID_DEVICE_KEEP_ALIVE,
)
from .map_data import decode_position
from .profile import DeviceProfile, load_profile
from .transport import DreameVacuumProtocol

_LOGGER = logging.getLogger(__name__)

# The device's cruise-to-point work mode. Driving to a location is expressed
# as "start a cleaning task of this kind", not as a movement command.
CRUISE_POINT_MODE = 23

# The values the app's live view sends: 45 / -45 to turn, 180 to spin round,
# 200 forward. It holds them while the button is down. This integration sends
# an angle instead - see async_turn_degrees for why.
TURN_RATE_DPS = 45

# The app resends the command every second for as long as the button is held.
# The device treats remote control as a lease: stop refreshing it and the
# device falls back to whatever task it was in, which is what brings the
# brushes and mop back on mid-turn.
REMOTE_REFRESH_SECONDS = 1.0

# How long to let a camera session settle before moving. Measured by hand:
# turns became silent at about three seconds.
CAMERA_SETTLE_SECONDS = 3.5

# Lowest settings the device offers. Driving under remote control is a
# cleaning mode as far as the firmware is concerned - Dreame document it as
# "Remote Control Cleaning" - so the brushes cannot be switched off outright,
# only turned down. Despite the generated names, siid 4 piid 4 is the suction
# level and piid 5 is the water volume.
SUCTION_QUIET = 0
WATER_LOW = 1
QUIET_PROPERTIES = (
    ("VacuumExtend", "PropCleaningMode", SUCTION_QUIET),
    ("VacuumExtend", "PropMopMode", WATER_LOW),
)

# Work modes that mean "no task is running". Seeing one of these while still
# short of the target means the device abandoned the trip rather than that it
# is still on its way. Values mirror vacuum.py's status constants.
TASK_ENDED_MODES = {0, 6, 14, 17}

# Properties we try to read every cycle, expressed in vocabulary terms so the
# numeric ids come from the generated profile rather than being hardcoded.
CORE_PROPERTIES: list[tuple[str, str]] = [
    ("Vacuum", "PropVacuumStatus"),     # device state enum
    ("Vacuum", "PropVacuumFault"),      # error code
    ("Battery", "PropBatteryLevel"),
    ("Battery", "PropChargingState"),
    ("VacuumExtend", "PropWorkMode"),   # idle/cleaning/paused/returning/...
    ("VacuumExtend", "PropCleaningTime"),
    ("VacuumExtend", "PropCleaningArea"),
    ("VacuumExtend", "PropCleaningMode"),
    ("VacuumExtend", "PropMopMode"),
    ("VacuumExtend", "PropWaterboxStatus"),
    ("Audio", "PropVolume"),
]


def result_code(result: Any) -> Any:
    """Pull the response code out of whatever shape the transport returned.

    Calls come back as a dict for some methods and a list of dicts for
    others, and the cloud occasionally nests one inside the other. Assuming a
    dict raised "'list' object has no attribute 'get'" the first time an
    action answered in list form.
    """
    while isinstance(result, list) and result:
        result = result[0]
    return result.get("code") if isinstance(result, dict) else None


def device_display_name(record: dict) -> str:
    """The name to show the user, from a cloud device record.

    The name you set in the Dreamehome app is `customName`; a device you never
    renamed has an empty one and carries the app's own label in
    deviceInfo.displayName. Falling straight through to the model is what
    produced device names - and so entity ids - like `dreame_vacuum_r2579h`.
    """
    return (
        record.get("customName")
        or (record.get("deviceInfo") or {}).get("displayName")
        or record.get("model")
        or str(record.get("did"))
    )


class DreameCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Owns the device connection, state and profile."""

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, profile: DeviceProfile | None = None
    ) -> None:
        self.entry = entry
        # Options override data so the companion settings (host/port/token/PIN)
        # stay editable after setup without re-adding the integration.
        cfg = {**entry.data, **entry.options}
        self.did: str = cfg[CONF_DID]
        self.model: str = cfg.get(CONF_MODEL) or "unknown"
        self.device_name: str = cfg.get(CONF_NAME) or self.model

        # Passed in by async_setup_entry, which loads it in the executor -
        # load_profile reads JSON off disk and must not run on the event loop.
        self.profile: DeviceProfile = profile or load_profile(self.model)
        self._protocol: DreameVacuumProtocol | None = None

        # Robot pose, decoded from map frames. Kept out of `data` because map
        # frames arrive by push on their own schedule, not from the poll.
        self.position: dict | None = None
        self._position_at: float | None = None
        self._map_ids = self.profile.prop_id("CleanMap", "PropMapdata")

        # siid.piid -> bool; None until probed
        self._present: dict[str, bool] = {}
        self._last_change = time.time()
        self._last_failure: float | None = None
        self._keep_alive_cancel = None
        self._warned_no_pin = False
        self.last_rotation: dict | None = None

        self.companion: CompanionClient | None = None
        if cfg.get(CONF_ENABLE_CAMERA) and cfg.get(CONF_COMPANION_TOKEN):
            self.companion = CompanionClient(
                async_get_clientsession(hass),
                cfg.get(CONF_COMPANION_HOST, "localhost"),
                int(cfg.get(CONF_COMPANION_PORT, DEFAULT_COMPANION_PORT)),
                cfg[CONF_COMPANION_TOKEN],
            )

        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN} {self.device_name}",
            update_interval=timedelta(seconds=POLL_IDLE),
        )

    # -- lifecycle --------------------------------------------------------
    async def async_setup(self) -> None:
        """Log in and connect. Blocking work stays off the event loop."""
        self._protocol = await self.hass.async_add_executor_job(self._build_protocol)

        if not await self.hass.async_add_executor_job(self._protocol.cloud.login):
            raise ConfigEntryAuthFailed("Dreame login failed - credentials may have changed")

        await self._async_repair_name()

        try:
            await self.hass.async_add_executor_job(self._connect)
        except Exception as err:  # noqa: BLE001 - surfaced as a retryable setup failure
            raise ConfigEntryNotReady(f"Could not connect to {self.device_name}: {err}") from err

        # Mandatory: the device stops sending data if this lapses.
        self._keep_alive_cancel = async_track_time_interval(
            self.hass, self._async_keep_alive, timedelta(seconds=KEEP_ALIVE_INTERVAL)
        )

    async def _async_repair_name(self) -> None:
        """Recover the real device name for entries created before it was read
        correctly, which stored the model instead.

        Guarded on name == model so this costs nothing on a healthy entry, and
        runs before the platforms are set up so the device registry picks the
        new name up straight away.
        """
        if self.device_name != self.model:
            return

        record = await self.hass.async_add_executor_job(self._lookup_device_record)
        if not record:
            return
        name = device_display_name(record)
        if not name or name == self.device_name:
            return

        _LOGGER.info("Renaming %s to %s", self.device_name, name)
        self.device_name = name
        self.hass.config_entries.async_update_entry(
            self.entry, data={**self.entry.data, CONF_NAME: name}
        )

    def _lookup_device_record(self) -> dict | None:
        """Blocking - runs in the executor."""
        assert self._protocol is not None
        try:
            devices = self._protocol.cloud.get_devices() or {}
        except Exception as err:  # noqa: BLE001 - cosmetic, never fail setup for it
            _LOGGER.debug("Could not re-read the device list: %s", err)
            return None
        for rec in devices.get("page", {}).get("records", []):
            if str(rec.get("did")) == self.did:
                return rec
        return None

    def _build_protocol(self) -> DreameVacuumProtocol:
        return DreameVacuumProtocol(
            username=self.config[CONF_USERNAME],
            password=self.config[CONF_PASSWORD],
            country=self.config.get(CONF_COUNTRY, "eu"),
            prefer_cloud=True,
            account_type="dreame",
        )

    def _connect(self) -> None:
        assert self._protocol is not None
        self._protocol.cloud._did = self.did
        self._protocol.connect(self._on_push_message)

    async def async_shutdown_device(self) -> None:
        if self._keep_alive_cancel:
            self._keep_alive_cancel()
            self._keep_alive_cancel = None
        if self.companion:
            await self.companion.async_stream_stop(self.did)
        if self._protocol:
            await self.hass.async_add_executor_job(self._protocol.disconnect)

    # -- push -------------------------------------------------------------
    def _on_push_message(self, message: dict) -> None:
        """MQTT properties_changed - the primary state path."""
        if not isinstance(message, dict):
            return
        if message.get("method") != "properties_changed":
            return

        updates: dict[str, Any] = {}
        for param in message.get("params") or []:
            siid, piid = param.get("siid"), param.get("piid")
            if siid is None or piid is None:
                continue
            updates[f"{siid}.{piid}"] = param.get("value")

        self._absorb_map_frame(updates)

        if not updates:
            return

        self._last_change = time.time()
        merged = {**(self.data or {}), **updates}
        self.hass.loop.call_soon_threadsafe(self.async_set_updated_data, merged)

    # -- map / position ---------------------------------------------------
    def _absorb_map_frame(self, updates: dict[str, Any]) -> None:
        """Pull the pose out of any map frame in this batch, then drop it.

        Map payloads are tens of kilobytes of base64 and change constantly;
        keeping them in coordinator data would push that through every entity
        state write for no benefit.
        """
        if self._map_ids is None:
            return
        raw = updates.pop(f"{self._map_ids[0]}.{self._map_ids[1]}", None)
        if not isinstance(raw, str):
            return

        position = decode_position(raw, self.profile.flag("AES_IV"))
        if position is None:
            return
        self.position = position
        self._position_at = time.monotonic()
        self._last_change = time.time()

    async def async_request_map(self) -> bool:
        """Ask for a full map frame.

        The device pushes frames while it is moving but goes quiet when idle,
        so this is how the position gets refreshed on demand - at startup, or
        before anything that needs to know where the robot is.
        """
        ids = self.profile.prop_id("CleanMap", "PropFrameInfo")
        if ids is None:
            return False
        # force_type makes the device regenerate rather than hand back the
        # frame it already sent, which is the difference between a fresh pose
        # and the same stale one.
        params = json.dumps(
            {"req_type": 1, "frame_type": "I", "force_type": 1}, separators=(",", ":")
        )
        return await self.async_action("CleanMap", "mapReq", [{"piid": ids[1], "value": params}])

    def position_age(self) -> float:
        """Seconds since the pose was last updated, or inf if never."""
        if self._position_at is None:
            return float("inf")
        return time.monotonic() - self._position_at

    async def async_refresh_position(
        self, timeout: float = 20.0, max_age: float = 0.0
    ) -> float | None:
        """Force a map frame and wait for the pose it carries.

        Returns the heading in degrees, or None if no fresh frame could be
        obtained. Staleness matters here: reusing the previous frame's heading
        after a rotation step would make the loop think the robot hadn't moved
        and send the same correction again.
        """
        # A caller that only needs to know roughly where the robot is (rather
        # than measuring the result of a nudge) can accept a recent reading.
        # Forcing a frame is not always possible: on Dreame-cloud devices the
        # map property often reads back empty because the real map is fetched
        # from cloud storage, so frames only arrive by push.
        if max_age and self.position_age() <= max_age:
            angle = (self.position or {}).get("angle")
            if angle is not None:
                return float(angle)

        before = (self.position or {}).get("frame_id")
        await self.async_request_map()

        attempts = 0
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            # Fetch by whatever route works, then insist the frame is new.
            # A direct read is NOT fresh by definition: on the cloud route it
            # downloads the last file the robot uploaded, which does not
            # change until the robot uploads another. Trusting it meant the
            # rotation loop re-read the same heading after every turn,
            # computed the same correction, and nudged forever.
            await self.hass.async_add_executor_job(self._read_map_frame)
            pos = self.position
            if pos and pos.get("frame_id") != before and pos.get("angle") is not None:
                _LOGGER.debug(
                    "Fresh pose for %s from frame %s (was %s): %s deg at (%s, %s)",
                    self.device_name, pos.get("frame_id"), before,
                    pos.get("angle"), pos.get("x"), pos.get("y"),
                )
                return float(pos["angle"])

            # Prompt another upload - one request at the start is not enough
            # when the robot is stationary between nudges.
            attempts += 1
            if attempts % 3 == 0:
                await self.async_request_map()

            # Each attempt may be a full cloud round trip (resolve name, sign
            # a url, download), and the device needs a moment to upload the
            # frame it was just asked for, so don't hammer it.
            await asyncio.sleep(1.5)

        _LOGGER.debug(
            "No new map frame for %s within %.0fs (still frame %s)",
            self.device_name, timeout, before,
        )
        return None

    def _position_diagnosis(self) -> str:
        """A specific reason rather than a generic failure.

        There are several distinct causes and they need different fixes, so
        guessing "not localised" at the user is unhelpful.
        """
        if self._map_ids is None:
            return "This model's profile has no map property, so position is unavailable."
        if self.position is None:
            return "No map frame has ever been decoded for this vacuum."
        if self.position.get("angle") is None:
            return "The vacuum sent a map frame but could not place itself on the map."
        return "The last frame is stale and no newer one arrived."

    def _read_map_frame(self) -> bool:
        """Blocking. Get a map frame by whichever route this device uses.

        Two routes exist. Local/miio devices put the frame straight in the map
        property. Dreame-cloud devices leave that property empty and instead
        publish an object name pointing at a file in cloud storage, which has
        to be fetched over HTTP.
        """
        return self._read_map_property() or self._read_map_from_cloud()

    def _read_map_from_cloud(self) -> bool:
        """Blocking. Resolve the current object name and download the frame."""
        ids = self.profile.prop_id("CleanMap", "PropObjectName")
        if ids is None or self._protocol is None:
            return False

        # Deliberately the cloud REST API (cloud.get_properties, keyed "6.3")
        # rather than protocol.get_properties, which asks the device itself
        # over RPC. The object name is account-side state held by Dreame's
        # servers; the device does not answer for it, which is why reading it
        # the usual way came back empty.
        try:
            result = self._protocol.cloud.get_properties(f"{ids[0]}.{ids[1]}")
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Object name read failed: %s", err)
            return False

        if not (isinstance(result, list) and result):
            _LOGGER.debug("Object name query returned nothing")
            return False

        object_name = self._first_object_name(result[0].get("value"))
        if not object_name:
            _LOGGER.debug("No map object name available yet")
            return False

        try:
            # Interim first: that's the live map. The permanent url is for
            # saved maps and lags behind the robot's current position.
            url = self._protocol.cloud.get_interim_file_url(
                object_name
            ) or self._protocol.cloud.get_file_url(object_name)
            if not url:
                _LOGGER.debug("No download url for map object %s", object_name)
                return False
            raw = self._protocol.cloud.get_file(url)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Map download failed: %s", err)
            return False

        if not raw:
            return False

        position = decode_position(
            raw.decode() if isinstance(raw, bytes) else str(raw),
            self.profile.flag("AES_IV"),
        )
        if position is None:
            _LOGGER.warning("Downloaded a map for %s but could not decode it", self.device_name)
            return False

        self.position = position
        self._position_at = time.monotonic()
        return True

    @staticmethod
    def _first_object_name(value: Any) -> str | None:
        """The property carries a list of names, sometimes JSON-encoded."""
        if isinstance(value, list):
            return str(value[0]) if value else None
        if isinstance(value, str) and value:
            if value.startswith("["):
                try:
                    decoded = json.loads(value)
                    return str(decoded[0]) if decoded else None
                except (ValueError, IndexError):
                    return None
            return value
        return None

    def _read_map_property(self) -> bool:
        """Blocking. Fetch and decode the map property, updating self.position."""
        if self._map_ids is None or self._protocol is None:
            return False
        siid, piid = self._map_ids
        try:
            result = self._protocol.get_properties(
                [{"did": self.did, "siid": siid, "piid": piid}]
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Map property read failed: %s", err)
            return False

        if not (isinstance(result, list) and result):
            return False
        item = result[0]
        if item.get("code") != 0:
            _LOGGER.debug("Map property unavailable: code=%s", item.get("code"))
            return False

        raw = item.get("value")
        if not isinstance(raw, str) or not raw:
            _LOGGER.debug("Map property is empty - trying cloud storage instead")
            return False

        position = decode_position(raw, self.profile.flag("AES_IV"))
        if position is None:
            _LOGGER.warning(
                "Could not decode a map frame for %s (%d chars) - position is unavailable",
                self.device_name,
                len(raw),
            )
            return False
        self.position = position
        self._position_at = time.monotonic()
        return True

    async def _async_quieten(self) -> dict:
        """Turn suction and water down, returning what they were."""
        previous: dict[tuple[str, str], Any] = {}
        for service, prop, quiet in QUIET_PROPERTIES:
            current = self.value(service, prop)
            if current is None:
                continue
            try:
                if int(current) == quiet:
                    continue
            except (TypeError, ValueError):
                continue
            if await self.async_set(service, prop, quiet):
                previous[(service, prop)] = current
        if previous:
            _LOGGER.debug("Turned %s down for rotation: %s", self.device_name, previous)
        return previous

    async def _async_restore(self, previous: dict) -> None:
        """Put suction and water back. Runs even when the rotation failed."""
        for (service, prop), value in previous.items():
            if not await self.async_set(service, prop, value):
                _LOGGER.warning(
                    "Could not restore %s.%s to %s on %s", service, prop, value, self.device_name
                )

    async def async_turn_degrees(self, degrees: float) -> bool:
        """Turn by an angle, in one command.

        spdw is honoured as a rotation amount by this firmware: a single
        command of N degrees turns roughly N degrees. The app's live view uses
        fixed values (45, 120, 180) held while a button is down, which reads
        like a rate - but reproducing that, holding 45 for degrees/45 seconds,
        barely moved the robot, because most of a short burst is lost to round
        trip and spin-up. Sending the angle measured far closer, so that is
        what this does.

        No explicit stop: the device completes the rotation itself, and
        sending zero afterwards cut the turn short.
        """
        step = int(round(degrees))
        if not step:
            return True
        # The app never sends more than 180 in one press, and a larger value
        # would be an ambiguous way to express the long way round.
        step = max(-180, min(180, step))
        return await self.async_remote_control_step(rotation=step)

    async def async_remote_control(
        self, rotation: int = 0, velocity: int = 0, duration: float = 0.0,
        silent: bool = True,
    ) -> None:
        """Raw remote control, for experimenting.

        duration 0 sends a single command and leaves it running - the device
        keeps going until something sends zero, exactly like holding the app's
        button down. Any other duration holds it (resending at the app's 1Hz)
        and then releases.

        `silent` wraps the move in a camera session, which is what stops the
        firmware treating it as cleaning. It costs a few seconds of setup, so
        it can be turned off when driving repeatedly or when the noise does
        not matter.
        """
        camera = await self._async_open_camera_session() if silent else None
        try:
            if not await self.async_remote_control_step(rotation=rotation, velocity=velocity):
                raise HomeAssistantError(
                    f"{self.device_name} rejected the remote control command"
                )
            if duration <= 0:
                return

            deadline = time.monotonic() + duration
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                await asyncio.sleep(min(REMOTE_REFRESH_SECONDS, remaining))
                if remaining > REMOTE_REFRESH_SECONDS:
                    await self.async_remote_control_step(rotation=rotation, velocity=velocity)
                    if camera:
                        await self.hass.async_add_executor_job(camera.keep_alive)
            await self.async_remote_control_step(rotation=0, velocity=0)
        finally:
            if camera:
                await self.hass.async_add_executor_job(camera.stop)

    async def async_remote_control_step(self, rotation: int = 0, velocity: int = 0) -> bool:
        """Raw remote-control command. rotation is deg/s, velocity mm/s."""
        ids = self.profile.prop_id("VacuumExtend", "PropRemoteState")
        if ids is None:
            return False
        payload = json.dumps(
            {
                "spdv": int(velocity),
                "spdw": int(rotation),
                "audio": "false",
                # The device ignores a command identical to the last one; the
                # nonce is what makes repeated equal steps take effect.
                "random": random.randrange(1000),
                "timestamp": int(time.time() * 1000),
            },
            separators=(",", ":"),
        )
        # retry_count=1: a retried movement command could be applied twice.
        result = await self.hass.async_add_executor_job(
            self._protocol.set_property, ids[0], ids[1], payload, 1
        )
        return result_code(result) == 0

    async def async_go_to_point(
        self,
        x: int,
        y: int,
        heading: float | None = None,
        heading_tolerance: float = 1.0,
        arrival_tolerance: int = 250,
        timeout: float = 180.0,
        use_camera_session: bool = True,
    ) -> None:
        """Drive to a point on the current map, optionally facing a heading.

        There is no dedicated "go to" action. The app does this by starting a
        cleaning task in the cruise-to-point mode and passing the target in
        the task's parameters, so that is what this sends.
        """
        mode_ids = self.profile.prop_id("VacuumExtend", "PropWorkMode")
        extend_ids = self.profile.prop_id("VacuumExtend", "PropCleanExtendData")
        if mode_ids is None or extend_ids is None:
            raise HomeAssistantError(f"{self.device_name} does not support go-to-point")

        # Sanity-check against the frame the coordinates belong to. Numbers
        # from an older map put the robot somewhere arbitrary, and it will
        # happily drive there.
        start = await self.async_refresh_position(max_age=300)
        if start is None:
            raise HomeAssistantError(
                f"Could not read {self.device_name}'s position. {self._position_diagnosis()}"
            )

        params = json.dumps({"tpoint": [[int(x), int(y), 0, 0]]}, separators=(",", ":"))
        ok = await self.async_action(
            "VacuumExtend",
            "startClean",
            [
                {"piid": mode_ids[1], "value": CRUISE_POINT_MODE},
                {"piid": extend_ids[1], "value": params},
            ],
        )
        if not ok:
            raise HomeAssistantError(f"{self.device_name} rejected the go-to-point command")

        await self._async_wait_until_arrived(int(x), int(y), arrival_tolerance, timeout)

        if heading is not None:
            # End the cruise task before rotating: it keeps control of the
            # drive and would fight the nudges. Note stopClean, not
            # Vacuum.StopSweeping - despite the name the latter is pause, and
            # a paused mopping task sends the robot back to wash.
            await self.async_action("VacuumExtend", "stopClean")
            await self._async_wait_until_idle()
            self.last_rotation = await self.async_rotate_to_heading(
                heading, tolerance=heading_tolerance,
                use_camera_session=use_camera_session,
            )

    async def _async_wait_until_idle(self, timeout: float = 30.0) -> None:
        """Block until no task is running.

        Rotating while the cruise task is still winding down folds the drive
        command into that task, and the device starts cleaning. A fixed sleep
        was not enough - how long the task takes to end varies.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            await self.async_request_refresh()
            mode = self.value("VacuumExtend", "PropWorkMode")
            if mode in TASK_ENDED_MODES:
                return
            await asyncio.sleep(2)
        _LOGGER.warning(
            "%s still reports work_mode %s after being told to stop; rotating anyway",
            self.device_name,
            self.value("VacuumExtend", "PropWorkMode"),
        )

    async def async_inspect_point(
        self,
        x: int,
        y: int,
        heading: float | None = None,
        filename: str | None = None,
        arrival_tolerance: int = 250,
        heading_tolerance: float = 5.0,
        timeout: float = 180.0,
        return_to_dock: bool = True,
    ) -> dict:
        """Drive somewhere, face a heading, photograph it, come home.

        One camera stream is held open across the whole run. That solves two
        things at once: the stream keeps the device out of remote-control
        cleaning mode so turning is silent, and /capture pulls its frame from
        the running stream rather than negotiating a fresh session - which is
        what made snapshots come back holding a previous run's image.

        Returns what actually happened rather than raising, so a caller can
        report a partial success. Only an outright failure to move raises.
        """
        if not self.companion:
            raise HomeAssistantError(
                f"{self.device_name} has no companion add-on configured, "
                "which is needed to take a photo"
            )

        cfg = self.config
        creds = (
            cfg[CONF_USERNAME], cfg[CONF_PASSWORD],
            cfg.get(CONF_COUNTRY, "eu"), cfg.get(CONF_CAMERA_PIN, ""),
        )

        # Leave a stream that was already running alone - stopping someone
        # else's stream at the end would be a surprise.
        started_here = False
        if not await self.companion.async_stream_status(self.did):
            if await self.companion.async_stream_start(*creds, self.did):
                started_here = True
                _LOGGER.debug("Opened a stream for the inspection of %s", self.device_name)
            else:
                _LOGGER.warning(
                    "Could not start a stream for %s; the turn will run the brushes "
                    "and the photo may be stale", self.device_name,
                )

        result: dict[str, Any] = {"arrived": False, "photo": None}
        try:
            self.last_rotation = None
            await self.async_go_to_point(
                x, y, heading=heading,
                heading_tolerance=heading_tolerance,
                arrival_tolerance=arrival_tolerance,
                timeout=timeout,
                # The stream already holds a session; a second would be refused.
                use_camera_session=not started_here,
            )
            result["arrived"] = True
        except HomeAssistantError as err:
            # Photograph wherever it got to - that picture is often the point.
            result["error"] = str(err)
            _LOGGER.warning("Inspection of %s did not arrive: %s", self.device_name, err)

        if self.last_rotation:
            result["rotation"] = self.last_rotation.get("trace")

        result["photo"] = await self._async_capture_to(filename, creds)

        if self.position:
            result.update({k: self.position.get(v) for k, v in
                           (("x", "x"), ("y", "y"), ("heading", "angle"))})

        if started_here:
            await self.companion.async_stream_stop(self.did)

        await self._async_record_run("inspect_point", result)

        if return_to_dock:
            await self.async_action("Battery", "StartCharge")
        return result

    async def _async_record_run(self, command: str, result: dict) -> None:
        """Push the outcome to the add-on so the UI can show it."""
        if not self.companion:
            return
        if result.get("arrived"):
            summary = (
                f"Arrived at ({result.get('x')}, {result.get('y')}) "
                f"facing {result.get('heading')}\u00b0"
            )
        else:
            summary = (
                f"Did not arrive - ended at ({result.get('x')}, {result.get('y')}) "
                f"facing {result.get('heading')}\u00b0"
            )
        detail = {"trace": result.get("rotation") or [], "photo": result.get("photo")}
        if result.get("error"):
            detail["error"] = result["error"]
        try:
            await self.companion.async_log_run(
                self.did, command, bool(result.get("arrived")), summary, detail
            )
        except Exception as err:  # noqa: BLE001 - logging must not break the errand
            _LOGGER.debug("Could not record the run: %s", err)

    async def _async_capture_to(self, filename: str | None, creds: tuple) -> str | None:
        """Take a fresh photo and optionally copy it where the caller asked."""
        path = await self.companion.async_capture(*creds, self.did)
        if not path:
            _LOGGER.warning("Could not capture a photo of %s", self.device_name)
            return None
        if not filename:
            return path

        image = await self.companion.async_latest_image(self.did)
        if not image:
            return path
        try:
            await self.hass.async_add_executor_job(self._write_image, filename, image)
        except OSError as err:
            _LOGGER.error("Could not write the photo to %s: %s", filename, err)
            return path
        return filename

    @staticmethod
    def _write_image(filename: str, image: bytes) -> None:
        """Blocking. Creates the directory - a missing folder is the usual
        reason a snapshot path fails, and it is not worth an error."""
        from pathlib import Path

        target = Path(filename)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(image)

    async def _async_wait_until_arrived(
        self, x: int, y: int, arrival_tolerance: int, timeout: float
    ) -> None:
        """Poll position until the robot is near the target.

        startClean returns as soon as the command is accepted, not on arrival,
        so without this a following rotation would run while the robot is
        still driving.
        """
        deadline = time.monotonic() + timeout
        closest: float | None = None

        ticks = 0
        idle_polls = 0
        while time.monotonic() < deadline:
            await self.hass.async_add_executor_job(self._read_map_frame)
            # Frames arrive by push while the robot drives, but nudge the
            # device periodically in case they stop.
            ticks += 1
            if ticks % 5 == 0:
                await self.async_request_map()
            pos = self.position or {}
            if pos.get("x") is not None:
                distance = math.hypot(pos["x"] - x, pos["y"] - y)
                if distance <= arrival_tolerance:
                    _LOGGER.info(
                        "%s arrived at (%s, %s), %dmm from target",
                        self.device_name, pos["x"], pos["y"], distance,
                    )
                    return
                if closest is None or distance < closest:
                    closest = distance
                _LOGGER.debug(
                    "%s at (%s, %s), %dmm to go", self.device_name, pos["x"], pos["y"], distance
                )

            # The device gives up quietly - an unreachable or off-map target
            # ends the task and it just sits there. Waiting out the full
            # timeout tells the user nothing, so fail as soon as it has
            # clearly stopped trying.
            mode = self.value("VacuumExtend", "PropWorkMode")
            idle_polls = idle_polls + 1 if mode in TASK_ENDED_MODES else 0
            if idle_polls >= 2:
                fault = self.value("Vacuum", "PropVacuumFault")
                raise HomeAssistantError(
                    f"{self.device_name} stopped without reaching ({x}, {y}) - "
                    f"work_mode {mode}, fault {fault}"
                    + (f", {int(closest)}mm away, so try an arrival_tolerance "
                       f"above that" if closest is not None else "")
                    + ". The target may be outside the current map, or the "
                      "tolerance tighter than the robot parks."
                )
            await asyncio.sleep(3)

        raise HomeAssistantError(
            f"{self.device_name} did not reach ({x}, {y}) within {int(timeout)}s"
            + (f" - got within {int(closest)}mm, so try an arrival_tolerance "
               f"above that" if closest is not None else "")
        )

    async def async_rotate_to_heading(
        self,
        heading: float,
        tolerance: float = 1.0,
        max_attempts: int = 10,
        damping: float = 0.6,
        settle: float = 4.0,
        quiet: bool = True,
        camera_settle: float | None = None,
        use_camera_session: bool = True,
    ) -> dict:
        """Turn on the spot until the robot faces `heading`.

        Closed loop, because the device has no absolute "turn to" command -
        only relative nudges, and it under- or over-shoots them. Pose is only
        observable via map frames at roughly 0.4 Hz, so this is
        step -> settle -> measure rather than continuous control.
        """
        _LOGGER.debug(
            "rotate_to_heading %s: target %s, tolerance %s, camera PIN %s",
            self.device_name, heading, tolerance,
            "set" if (self.config.get(CONF_CAMERA_PIN) or "").strip() else "MISSING",
        )

        # Lenient for the opening read: it only needs to know roughly where the
        # robot is pointing. Insisting on a brand new frame here fails before
        # anything has moved, because a stationary robot may not have uploaded
        # one for a long time.
        current = await self.async_refresh_position(max_age=180)
        if current is None:
            raise HomeAssistantError(
                f"Could not read {self.device_name}'s heading. "
                f"{self._position_diagnosis()} "
                "Enable debug logging for dreame_vacuum_core for the details."
            )

        # A camera session stops the firmware promoting the drive into a
        # remote-control cleaning task, which is what runs the brushes. A
        # caller already holding one (a running stream, say) passes
        # use_camera_session=False - the device allows only one at a time, so
        # opening a second would fail and leave the turn noisy.
        camera = (
            await self._async_open_camera_session(camera_settle)
            if use_camera_session else None
        )
        # Collected rather than only logged, so a caller can report it without
        # anyone having to read the log.
        trace: list[str] = [
            f"start {current:.0f}deg, target {heading:.0f}deg"
            + (", camera session open" if camera else ", NO camera session")
        ]
        previous = await self._async_quieten() if quiet and not camera else {}
        try:
            await self._async_rotate_loop(
                heading, current, tolerance, max_attempts, damping, settle, camera, trace
            )
        finally:
            # Restore even if the rotation raised, or a failed turn would
            # silently leave the vacuum on its quietest setting.
            await self._async_restore(previous)
            if camera:
                await self.hass.async_add_executor_job(camera.stop)
        return {
            "heading": (self.position or {}).get("angle"),
            "trace": trace,
        }

    async def _async_open_camera_session(self, settle: float | None = None) -> CameraSession | None:
        """Open a video-less camera session, or None if we cannot.

        Optional on purpose: the PIN is only configured when the camera is set
        up, and a vacuum without it should still be able to turn - noisily.
        """
        pin = (self.config.get(CONF_CAMERA_PIN) or "").strip()
        if not pin:
            # Logged every time rather than once: a silent fallback to a noisy
            # turn is exactly the thing that is hard to diagnose from outside.
            _LOGGER.warning(
                "No camera PIN configured for %s - rotating without a camera session, "
                "so the brushes will run. Set the PIN in the integration's options "
                "(Settings > Devices & Services > Dreame Vacuum Core > Configure)",
                self.device_name,
            )
            return None

        session = CameraSession(self._protocol, self.did, pin)
        try:
            started = await self.hass.async_add_executor_job(session.start)
        except Exception as err:  # noqa: BLE001 - a noisy turn beats no turn
            _LOGGER.warning(
                "Could not open a camera session for %s, so the brushes will run: %s",
                self.device_name, err,
            )
            return None
        if not started:
            _LOGGER.warning(
                "%s refused the camera session, so the brushes will run. "
                "Check the camera PIN is correct", self.device_name,
            )
            return None

        wait = CAMERA_SETTLE_SECONDS if settle is None else settle
        _LOGGER.info("Camera session open for %s, settling %.1fs before turning",
                     self.device_name, wait)
        await asyncio.sleep(wait)
        return session

    async def _async_rotate_loop(
        self,
        heading: float,
        current: float,
        tolerance: float,
        max_attempts: int,
        damping: float,
        settle: float,
        camera: CameraSession | None = None,
        trace: list[str] | None = None,
    ) -> None:
        trace = trace if trace is not None else []
        for attempt in range(1, int(max_attempts) + 1):
            if camera:
                await self.hass.async_add_executor_job(camera.keep_alive)
            # Shortest signed turn, so it never takes the long way round.
            diff = (heading - current) % 360
            if diff > 180:
                diff -= 360

            if abs(diff) <= tolerance:
                _LOGGER.debug(
                    "Rotation done for %s: at %.0f deg, %.0f from target %.0f",
                    self.device_name, current, diff, heading,
                )
                trace.append(f"done at {current:.0f}deg, {diff:+.0f} off target")
                return

            step = diff * damping
            if abs(step) < 1:
                # Damping shrinks the last corrections to nothing, which would
                # burn every remaining attempt without moving.
                step = 1 if diff > 0 else -1

            _LOGGER.debug(
                "Rotation %d/%d for %s: at %.0f deg, %.0f to go, commanding %.0f",
                attempt, max_attempts, self.device_name, current, diff, step,
            )
            if not await self.async_turn_degrees(step):
                raise HomeAssistantError(f"{self.device_name} rejected the rotation command")

            # The nudge is accepted immediately but the robot turns at its own
            # pace. Measuring straight away reads a pose from part-way through
            # the turn, so the next correction is computed against a heading
            # the robot has already left - it chases itself and burns every
            # attempt. Wait longer for bigger turns.
            await asyncio.sleep(min(settle + abs(step) * 0.15, 20.0))

            # Strict, and with a longer window: this is the measurement the
            # next correction depends on, and it has to reflect the turn that
            # just happened rather than the frame from before it.
            measured = await self.async_refresh_position(timeout=35.0)
            if measured is None:
                trace.append(f"{attempt}: commanded {step:+.0f}, NO new map frame")
                raise HomeAssistantError(
                    f"{self.device_name} turned but did not report a new position, so "
                    "there is no way to tell how far it went. The robot uploads map "
                    "frames on its own schedule and none arrived in time"
                )
            _LOGGER.debug(
                "Rotation %d/%d for %s: commanded %.0f, measured %.0f -> %.0f "
                "(turned %.0f) from map frame %s",
                attempt, max_attempts, self.device_name, step, current, measured,
                ((measured - current + 180) % 360) - 180,
                (self.position or {}).get("frame_id"),
            )
            trace.append(
                f"{attempt}: at {current:.0f}, commanded {step:+.0f}, "
                f"now {measured:.0f} (turned {((measured - current + 180) % 360) - 180:+.0f}) "
                f"frame {(self.position or {}).get('frame_id')}"
            )
            current = measured

        final = (heading - current) % 360
        if final > 180:
            final -= 360
        if abs(final) <= tolerance:
            trace.append(f"done at {current:.0f}deg, {final:+.0f} off target")
            return

        trace.append(f"gave up at {current:.0f}deg, {final:+.0f} off target")
        raise HomeAssistantError(
            f"{self.device_name} did not reach {heading}° within {max_attempts} attempts "
            f"(stopped at {current}°)"
        )

    # -- keep-alive -------------------------------------------------------
    async def _async_keep_alive(self, _now) -> None:
        try:
            await self.hass.async_add_executor_job(self._keep_alive)
        except Exception as err:  # noqa: BLE001 - never let this kill the timer
            _LOGGER.debug("Keep-alive failed for %s: %s", self.device_name, err)

    def _keep_alive(self) -> None:
        assert self._protocol is not None
        result = self._protocol.get_properties(
            [{"did": self.did, "siid": SIID_DEVICE_KEEP_ALIVE, "piid": PIID_DEVICE_KEEP_ALIVE}]
        )
        # The app always writes 1; the device answers with its client count.
        # A zero/absent reading means our slot lapsed, so re-assert it.
        value = None
        if isinstance(result, list) and result and result[0].get("code") == 0:
            value = result[0].get("value")
        if not value:
            self._protocol.set_property(SIID_DEVICE_KEEP_ALIVE, PIID_DEVICE_KEEP_ALIVE, 1)

    # -- polling ----------------------------------------------------------
    async def _async_update_data(self) -> dict[str, Any]:
        try:
            data = await self.hass.async_add_executor_job(self._poll)
        except Exception as err:  # noqa: BLE001
            self._last_failure = time.time()
            self._retune_interval()
            raise UpdateFailed(str(err)) from err

        self._last_failure = None
        self._retune_interval()
        return data

    def _poll(self) -> dict[str, Any]:
        assert self._protocol is not None
        requests: list[dict] = []
        for service, prop in CORE_PROPERTIES:
            ids = self.profile.prop_id(service, prop)
            if ids is None:
                continue  # not in the vocabulary at all
            siid, piid = ids
            key = f"{siid}.{piid}"
            if self._present.get(key) is False:
                continue  # probed and confirmed absent on this unit
            requests.append({"did": self.did, "siid": siid, "piid": piid})

        merged: dict[str, Any] = dict(self.data or {})
        for i in range(0, len(requests), PROPERTY_BATCH_SIZE):
            batch = requests[i : i + PROPERTY_BATCH_SIZE]
            result = self._protocol.get_properties(batch)
            if not isinstance(result, list):
                continue
            for item in result:
                siid, piid = item.get("siid"), item.get("piid")
                if siid is None or piid is None:
                    continue
                key = f"{siid}.{piid}"
                # code -1 means this unit doesn't implement it; remember so we
                # stop asking. This is the only reliable presence signal.
                if item.get("code") == 0:
                    self._present[key] = True
                    merged[key] = item.get("value")
                else:
                    self._present.setdefault(key, False)
        return merged

    def _retune_interval(self) -> None:
        """Adaptive interval - push carries the load, polling just reconciles."""
        if self._last_failure:
            elapsed = time.time() - self._last_failure
            seconds = POLL_FAIL_SHORT if elapsed <= 60 else POLL_FAIL_LONG
        elif time.time() - self._last_change <= 60:
            seconds = POLL_ACTIVE
        else:
            seconds = POLL_IDLE
        new = timedelta(seconds=seconds)
        if new != self.update_interval:
            self.update_interval = new

    # -- reads / writes ---------------------------------------------------
    def value(self, service: str, prop: str) -> Any | None:
        ids = self.profile.prop_id(service, prop)
        if ids is None:
            return None
        return (self.data or {}).get(f"{ids[0]}.{ids[1]}")

    def is_present(self, service: str, prop: str) -> bool | None:
        """True/False once probed, None if never attempted."""
        ids = self.profile.prop_id(service, prop)
        if ids is None:
            return False
        return self._present.get(f"{ids[0]}.{ids[1]}")

    async def async_set(self, service: str, prop: str, value: Any) -> bool:
        ids = self.profile.prop_id(service, prop)
        if ids is None:
            _LOGGER.warning("Unknown property %s.%s for %s", service, prop, self.model)
            return False
        siid, piid = ids
        result = await self.hass.async_add_executor_job(
            self._protocol.set_property, siid, piid, value
        )
        ok = result_code(result) == 0
        if ok:
            self._last_change = time.time()
            await self.async_request_refresh()
        return bool(ok)

    async def async_action(self, service: str, action: str, params: list | None = None) -> bool:
        ids = self.profile.action_id(service, action)
        if ids is None:
            _LOGGER.warning("Unknown action %s.%s for %s", service, action, self.model)
            return False
        siid, aiid = ids
        result = await self.hass.async_add_executor_job(
            self._protocol.action, siid, aiid, params or []
        )
        ok = result_code(result) == 0
        if ok:
            self._last_change = time.time()
            await self.async_request_refresh()
        return ok

    # -- companion --------------------------------------------------------
    async def async_register_with_companion(self) -> None:
        """Push our device identity so the add-on's UI knows what's ours."""
        if not self.companion:
            return
        payload = [
            {
                "did": self.did,
                "name": self.device_name,
                "model": self.model,
                "entities": {
                    "vacuum": f"vacuum.{self._slug}",
                    "camera": f"camera.{self._slug}",
                    "battery": f"sensor.{self._slug}_battery",
                },
            }
        ]
        if await self.companion.async_register(self.entry.entry_id, payload):
            _LOGGER.debug("Registered %s with companion add-on", self.device_name)

    @property
    def config(self) -> dict:
        """Config entry data with options layered on top."""
        return {**self.entry.data, **self.entry.options}

    @property
    def _slug(self) -> str:
        return self.device_name.lower().replace(" ", "_").replace("-", "_")

    @property
    def device_info(self) -> dict:
        return {
            "identifiers": {(DOMAIN, self.did)},
            "name": self.device_name,
            "manufacturer": "Dreame",
            "model": self.model,
        }
