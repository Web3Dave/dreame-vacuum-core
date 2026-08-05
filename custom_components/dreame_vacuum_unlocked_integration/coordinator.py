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
import uuid
from typing import Any

import requests

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryNotReady,
    HomeAssistantError,
)
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from datetime import timedelta

from .camera_session import CameraSession
from .classify_registry import get_registry
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
from .map_data import decode_frame, decode_position, decode_room_names
from .map_render import map_document, metadata as map_metadata, render_png
from .profile import DeviceProfile, load_profile
from .transport import DreameVacuumProtocol

_LOGGER = logging.getLogger(__name__)

# The device's cruise-to-point work mode. Driving to a location is expressed
# as "start a cleaning task of this kind", not as a movement command.
CRUISE_POINT_MODE = 23

# "Clean a chosen set of rooms" work mode. The app starts it through the same
# VacuumExtend.startClean action as every other kind of cleaning; what makes
# it room-specific is the payload in PropCleanExtendData listing the room ids
# in the order they should be cleaned.
AREA_CLEAN_MODE = 18

# The values the app's live view sends: 45 / -45 to turn, 180 to spin round,
# 200 forward. It holds them while the button is down. This integration sends
# an angle instead - see async_turn_degrees for why.
TURN_RATE_DPS = 45

# The robot turns further than commanded, by a strikingly consistent factor:
# measured 68->89, 44->57 and 10->13 across two runs, all 1.30. Commands are
# divided by this, and it is refined from what each turn actually achieved -
# an earlier attempt at learning this was abandoned because measurements were
# being taken mid-rotation, which made the ratios meaningless.
TURN_OVERSHOOT_DEFAULT = 1.3
TURN_OVERSHOOT_MIN, TURN_OVERSHOOT_MAX = 0.5, 3.0

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
# The robot pushes map frames several times a second while it moves. This is
# how often that is allowed to rewrite entity state, which is what a live map
# on a dashboard follows.
POSE_NOTIFY_INTERVAL = 0.5

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


class ActiveTask:
    """What this vacuum is doing right now, if anything.

    Held by the integration because it is the thing performing the errand -
    anything else would be inferring. Lives in memory only: a Home Assistant
    restart ends the errand too, so there is no stale state to clean up.
    """

    def __init__(self, run_id: str, task: str | None, command: str, total: int = 0) -> None:
        self.run_id = run_id
        self.task = task
        self.command = command
        self.total = total
        self.step = 0
        self.detail = ""
        self.started = time.time()

    def as_attributes(self) -> dict[str, Any]:
        return {
            "task_running": True,
            "task_run_id": self.run_id,
            "task_id": self.task,
            "task_command": self.command,
            "task_step": self.step,
            "task_steps": self.total or None,
            "task_detail": self.detail or None,
            "task_started": int(self.started),
        }


class RunReporter:
    """Narrates an errand to the add-on's Activity page, step by step.

    Every call is best-effort and swallows its own failures: a missing log
    entry must never change what the robot does. When no add-on is configured
    this is a no-op, so callers need no conditionals.
    """

    def __init__(
        self, coordinator: "DreameCoordinator", command: str, run_id: str | None = None
    ) -> None:
        self._coordinator = coordinator
        self.command = command
        # Minted here so the entity attribute and the Activity entry refer to
        # the same run; the add-on stores it alongside its own row id.
        self.run_id = run_id or uuid.uuid4().hex[:8]
        self._id: int | None = None

    async def start(self) -> None:
        companion = self._coordinator.companion
        if not companion:
            return
        try:
            self._id = await companion.async_start_run(
                self._coordinator.did, self.command, self.run_id
            )
        except Exception as err:  # noqa: BLE001
            self._id = None
        if self._id is None:
            # Warn rather than whisper: when this fails, every step is dropped
            # and the errand looks like it never happened.
            _LOGGER.warning(
                "Could not open an Activity record for %s - the errand will run "
                "but will not appear in the add-on's Activity tab",
                self._coordinator.device_name,
            )

    async def step(self, text: str) -> None:
        _LOGGER.debug("%s: %s", self.command, text)
        if self._id is None or not self._coordinator.companion:
            return
        try:
            await self._coordinator.companion.async_run_step(self._id, text)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Could not record a run step: %s", err)

    async def finish(self, ok: bool, summary: str, detail: dict) -> None:
        if self._id is None or not self._coordinator.companion:
            return
        try:
            await self._coordinator.companion.async_finish_run(self._id, ok, summary, detail)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Could not close a run record: %s", err)


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
        self._pose_notified_at: float = 0.0
        self._map_ids = self.profile.prop_id("CleanMap", "PropMapdata")

        # siid.piid -> bool; None until probed
        self._present: dict[str, bool] = {}
        self._last_change = time.time()
        self._last_failure: float | None = None
        self._keep_alive_cancel = None
        self._warned_no_pin = False
        self.last_rotation: dict | None = None
        self._run: RunReporter | None = None
        self.active_task: ActiveTask | None = None
        self._turn_overshoot = TURN_OVERSHOOT_DEFAULT
        self._last_frame: str | None = None

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

        # A restart ended any errand that was in progress, but the add-on's
        # history cannot know that on its own.
        if self.companion:
            closed = await self.companion.async_close_orphaned_runs(self.did)
            if closed:
                _LOGGER.info("Closed %s abandoned run(s) for %s", closed, self.device_name)

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

        moved = self._absorb_map_frame(updates)

        if not updates:
            # A push carrying only a map frame still moved the robot, and the
            # pose lives outside coordinator data - so without this the
            # entity's position attributes went stale until some unrelated
            # property happened to change. A live map is the thing that made
            # that visible: the marker only advanced every few seconds, in
            # steps, whenever cleaning time ticked over.
            #
            # Throttled because the robot pushes frames several times a second
            # while cleaning, and every notification rewrites the state of
            # every entity on this device. Skipping one costs at most half a
            # second of staleness, which the next frame or poll corrects.
            if moved and self._pose_notify_due():
                self.hass.loop.call_soon_threadsafe(self.async_update_listeners)
            return

        self._last_change = time.time()
        merged = {**(self.data or {}), **updates}
        self.hass.loop.call_soon_threadsafe(self.async_set_updated_data, merged)

    # -- map / position ---------------------------------------------------
    def _pose_notify_due(self) -> bool:
        """Rate-limit pose-only state writes. Called from the MQTT thread."""
        now = time.monotonic()
        if now - self._pose_notified_at < POSE_NOTIFY_INTERVAL:
            return False
        self._pose_notified_at = now
        return True

    def _absorb_map_frame(self, updates: dict[str, Any]) -> bool:
        """Pull the pose out of any map frame in this batch, then drop it.

        Map payloads are tens of kilobytes of base64 and change constantly;
        keeping them in coordinator data would push that through every entity
        state write for no benefit.

        Returns whether a pose was taken, so the caller knows there is
        something to tell listeners about even when nothing else changed.
        """
        if self._map_ids is None:
            return False
        raw = updates.pop(f"{self._map_ids[0]}.{self._map_ids[1]}", None)
        if not isinstance(raw, str):
            return False

        position = decode_position(raw, self.profile.flag("AES_IV"))
        if position is None:
            return False
        self.position = position
        self._position_at = time.monotonic()
        self._last_change = time.time()
        # Kept so a dashboard card can be served the full map without asking
        # the device for another frame - this one is as fresh as it gets.
        self._last_frame = raw
        return True

    async def async_map_document(self, scale: int = 5, refresh: bool = False) -> dict | None:
        """The current map as data, for whoever is drawing it.

        Serves the frame already in hand by default. The robot pushes frames
        while it moves and goes quiet when it stops, so `refresh` is for the
        case where the last one is old - it costs a round trip to the device.
        """
        if refresh or not self._last_frame:
            await self.hass.async_add_executor_job(self._read_map_frame)
        raw = self._last_frame
        if not raw:
            return None
        return await self.hass.async_add_executor_job(self._build_document, raw, scale)

    def _build_document(self, raw: str, scale: int) -> dict | None:
        """Blocking: decrypt, decompress and re-encode a frame as a document."""
        frame = decode_frame(raw, self.profile.flag("AES_IV"))
        if frame is None:
            return None
        room_names = decode_room_names(frame["trailer"])
        return map_document(frame, scale, room_names=room_names)

    async def async_list_maps(self) -> dict:
        """Every map the cloud has a backup history for, and which one (if
        any) is currently active.

        Listing only - this mirrors the phone app's own "recover map" flow and
        does not download or restore anything. PropBackupMapInfo points at the
        most recent backup-manifest *file* on Dreame's cloud storage (its value
        is the object's name, on some firmwares wrapped as ``{"object_name": ...}``);
        the manifest is that file, fetched through the file-bridge
        (``getDownloadUrl``) and parsed for the per-map backup timestamps.

        Each backup's own map payload is an opaque blob (the file carries a
        compressed thumbnail and the map archive is not parsed here) - so this
        deliberately stops at metadata rather than guessing at an undocumented
        archive layout.
        """
        return await self.hass.async_add_executor_job(self._list_maps_blocking)

    def _list_maps_blocking(self) -> dict:
        current_map_id: int | None = None
        if self._last_frame:
            frame = decode_frame(self._last_frame, self.profile.flag("AES_IV"))
            if frame:
                try:
                    current_map_id = int(frame["trailer"].get("curid"))
                except (TypeError, ValueError):
                    current_map_id = None

        ids = self.profile.prop_id("CleanMap", "PropBackupMapInfo")
        if ids is None or self._protocol is None:
            return {"current_map_id": current_map_id, "maps": []}

        manifest = self._fetch_backup_manifest()
        if not manifest:
            return {"current_map_id": current_map_id, "maps": []}

        maps = []
        for entry in manifest:
            try:
                map_id = int(entry["id"])
            except (KeyError, TypeError, ValueError):
                continue
            backups = []
            for info in entry.get("info") or []:
                if not isinstance(info, dict) or "time" not in info:
                    continue
                backups.append({
                    "time": info["time"],
                    "first": bool(info.get("first")),
                })
            backups.sort(key=lambda b: b["time"], reverse=True)
            maps.append({
                "id": map_id,
                "is_current": map_id == current_map_id,
                "backups": backups,
            })
        maps.sort(key=lambda m: (not m["is_current"], -(m["backups"][0]["time"] if m["backups"] else 0)))
        return {"current_map_id": current_map_id, "maps": maps}

    def _fetch_backup_manifest(self) -> list[dict] | None:
        """The raw backup-map manifest from Dreame's cloud, or None.

        PropBackupMapInfo does not carry the manifest inline. Its value is
        the cloud object *name* of the newest backup-manifest file - on a
        bare-object-name firmware the value is the path directly, on the MIoT
        route it arrives wrapped as ``{"object_name": ...}``. Either way the
        manifest body is that file, downloaded through the file-bridge
        (the same getDownloadUrl the phone app's recovery screen calls).
        """
        if self._protocol is None:
            return None
        ids = self.profile.prop_id("CleanMap", "PropBackupMapInfo")
        if ids is None:
            return None
        try:
            result = self._protocol.cloud.get_properties(f"{ids[0]}.{ids[1]}")
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Backup map manifest read failed: %s", err)
            return None

        if not (isinstance(result, list) and result and result[0].get("value")):
            return None

        value = result[0]["value"]
        object_name = value if isinstance(value, str) else ""
        try:
            parsed = json.loads(object_name)
            if isinstance(parsed, dict) and isinstance(parsed.get("object_name"), str):
                object_name = parsed["object_name"]
        except (TypeError, ValueError):
            pass

        if not object_name:
            return None

        try:
            url = self._protocol.cloud.get_interim_file_url(object_name)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Backup map manifest URL fetch failed: %s", err)
            return None

        if not isinstance(url, str) or not url:
            return None

        try:
            resp = requests.get(url, timeout=25)
            resp.raise_for_status()
            manifest = resp.json()
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Backup map manifest download failed: %s", err)
            return None

        return manifest if isinstance(manifest, list) else None

    async def async_backup_map_document(self, map_id: int, time: int, scale: int = 5) -> dict | None:
        """Render one historical backup map as a document.

        Each manifest entry also carries a ``thb`` field - the map frame
        itself as url-safe base64 of a zlib-compressed blob, in the exact
        layout `decode_frame` already reads (map_id, frame_id, frame_type='I',
        robot/charger pose, grid_size, width/height, then the grid and a JSON
        trailer). It is not a thumbnail image; it *is* the backup map, so a
        backup renders through the same `map.js` component as the live map.
        """
        return await self.hass.async_add_executor_job(self._backup_map_blocking, map_id, time, scale)

    def _backup_map_blocking(self, map_id: int, time: int, scale: int) -> dict | None:
        manifest = self._fetch_backup_manifest()
        if not manifest:
            return None
        for entry in manifest:
            try:
                entry_id = int(entry.get("id"))
            except (TypeError, ValueError):
                continue
            if entry_id != map_id:
                continue
            for info in entry.get("info") or []:
                if not isinstance(info, dict) or int(info.get("time", -1)) != time:
                    continue
                thb = info.get("thb")
                if not isinstance(thb, str) or not thb:
                    continue
                # Backup frames are stored unencrypted - no comma-suffixed AES
                # key, so no IV is needed (matches decode_room_names' rism path).
                frame = decode_frame(thb)
                if frame is None:
                    _LOGGER.debug("Backup %s/%s did not decode", map_id, time)
                    return None
                room_names = decode_room_names(frame.get("trailer") or {})
                return map_document(frame, scale, room_names=room_names)
        return None

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

    async def async_settled_heading(
        self, timeout: float = 45.0, tolerance: float = 2.0
    ) -> float | None:
        """Wait until the heading stops changing, then return it.

        The first frame after a turn is often captured mid-rotation, so the
        robot is still moving when it is measured - the value then changes
        again seconds later. Correcting against that reading makes the loop
        chase a robot that has already moved on. Two consecutive frames
        agreeing means it has actually stopped.
        """
        deadline = time.monotonic() + timeout
        previous: float | None = None
        first: float | None = None
        reads = 0
        while time.monotonic() < deadline:
            reading = await self.async_refresh_position(
                timeout=max(5.0, deadline - time.monotonic())
            )
            if reading is None:
                return previous
            reads += 1
            if previous is not None:
                drift = ((reading - previous + 180) % 360) - 180
                if abs(drift) <= tolerance:
                    # Reported so the value of this check is measurable: if the
                    # first two frames always agree, the second is redundant
                    # and a single frame would halve the time a turn costs.
                    await self._async_step(
                        f"  settled after {reads} frames"
                        + (f" (first read {first:.0f}\u00b0, final {reading:.0f}\u00b0)"
                           if first is not None and abs(
                               ((reading - first + 180) % 360) - 180) > tolerance
                           else "")
                    )
                    return reading
                await self._async_step(
                    f"  still turning: {previous:.0f}\u00b0 -> {reading:.0f}\u00b0"
                )
            else:
                first = reading
            previous = reading
        return previous

    def position_diagnosis(self) -> str:
        """Public: the same reason, for anything outside this module."""
        return self._position_diagnosis()

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

        text = raw.decode() if isinstance(raw, bytes) else str(raw)
        self._last_frame = text
        position = decode_position(text, self.profile.flag("AES_IV"))
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

        self._last_frame = raw
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

    def _learn_overshoot(self, commanded: float, achieved: float) -> None:
        """Refine the overshoot factor from one settled turn.

        Skips turns that went the wrong way or barely moved: those mean the
        robot was blocked or the reading was not settled after all, and would
        drag the estimate somewhere useless.
        """
        if abs(commanded) < 3 or abs(achieved) < 1:
            return
        if (commanded > 0) != (achieved > 0):
            return
        observed = abs(achieved) / abs(commanded)
        updated = self._turn_overshoot * 0.7 + observed * 0.3
        self._turn_overshoot = max(
            TURN_OVERSHOOT_MIN, min(TURN_OVERSHOOT_MAX, updated)
        )
        _LOGGER.debug(
            "Overshoot for %s now %.2f (commanded %.0f, turned %.0f)",
            self.device_name, self._turn_overshoot, commanded, achieved,
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
        # Ask for less, because the robot delivers more.
        step = int(round(degrees / self._turn_overshoot))
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
        run, owns = await self._async_begin_run("go_to_point")
        if owns:
            await run.step(
                f"target ({x}, {y})"
                + (f" facing {heading:.0f}\u00b0" if heading is not None else "")
                + f", within {arrival_tolerance}mm"
            )

        mode_ids = self.profile.prop_id("VacuumExtend", "PropWorkMode")
        extend_ids = self.profile.prop_id("VacuumExtend", "PropCleanExtendData")
        if mode_ids is None or extend_ids is None:
            await self._async_end_run(run, owns, False, "Unsupported", {})
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
            message = f"{self.device_name} rejected the go-to-point command"
            await self._async_end_run(run, owns, False, "Rejected", {"error": message})
            raise HomeAssistantError(message)

        try:
            await self._async_wait_until_arrived(int(x), int(y), arrival_tolerance, timeout)
        except HomeAssistantError as err:
            await self._async_end_run(
                run, owns, False, "Did not arrive", {"error": str(err)}
            )
            raise

        if heading is not None:
            # End the cruise task before rotating: it keeps control of the
            # drive and would fight the nudges. Note stopClean, not
            # Vacuum.StopSweeping - despite the name the latter is pause, and
            # a paused mopping task sends the robot back to wash.
            await self.async_action("VacuumExtend", "stopClean")
            await self._async_wait_until_idle()
            try:
                self.last_rotation = await self.async_rotate_to_heading(
                    heading, tolerance=heading_tolerance,
                    use_camera_session=use_camera_session,
                )
            except HomeAssistantError as err:
                await self._async_end_run(
                    run, owns, False, "Arrived, heading failed", {"error": str(err)}
                )
                raise

        pos = self.position or {}
        await self._async_end_run(
            run, owns, True,
            f"Arrived at ({pos.get('x')}, {pos.get('y')}) facing {pos.get('angle')}\u00b0",
            {"trace": (self.last_rotation or {}).get("trace") or []},
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
        tag: str | None = None,
        arrival_tolerance: int = 250,
        heading_tolerance: float = 5.0,
        timeout: float = 180.0,
        return_to_dock: bool = True,
        use_stream: bool = True,
        stop_stream: bool = True,
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

        run, owns = await self._async_begin_run("inspect_point")
        await run.step(
            f"target ({x}, {y})"
            + (f" facing {heading:.0f}\u00b0" if heading is not None else "")
            + f", within {arrival_tolerance}mm"
        )

        cfg = self.config
        creds = (
            cfg[CONF_USERNAME], cfg[CONF_PASSWORD],
            cfg.get(CONF_COUNTRY, "eu"), cfg.get(CONF_CAMERA_PIN, ""),
        )

        # Leave a stream that was already running alone - stopping someone
        # else's stream at the end would be a surprise.
        started_here = False
        already_running = await self.companion.async_stream_status(self.did)
        if not use_stream:
            # Each step then opens its own short camera session instead. The
            # photo still needs one, so this is not a no-camera mode.
            await run.step("skipping the stream, as asked")
        elif not already_running:
            await run.step("starting a camera stream")
            if await self.companion.async_stream_start(*creds, self.did):
                started_here = True
                await run.step("stream open")
            else:
                await run.step("stream would not start - the turn will run the brushes")
                _LOGGER.warning(
                    "Could not start a stream for %s; the turn will run the brushes "
                    "and the photo may be stale", self.device_name,
                )
        elif already_running:
            await run.step("a stream was already running, reusing it")

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
            await run.step("arrived")
        except HomeAssistantError as err:
            # Photograph wherever it got to - that picture is often the point.
            result["error"] = str(err)
            await run.step(f"failed: {err}")
            _LOGGER.warning("Inspection of %s did not arrive: %s", self.device_name, err)

        if self.last_rotation:
            result["rotation"] = self.last_rotation.get("trace")

        await run.step("taking a photo")
        shot = await self._async_capture_to(filename, creds, tag, run)
        if shot:
            result["photo"] = shot.get("copy") or shot.get("path")
            result["snapshot"] = shot
            await run.step(f"photo saved to {shot.get('media_path')}")
        else:
            await run.step("photo failed")

        if self.position:
            result.update({k: self.position.get(v) for k, v in
                           (("x", "x"), ("y", "y"), ("heading", "angle"))})

        if started_here and stop_stream:
            await self.companion.async_stream_stop(self.did)
            await run.step("stream closed")
        elif started_here:
            await run.step("leaving the stream running, as asked")

        if return_to_dock:
            await run.step("returning to the dock")
            await self.async_action("Battery", "StartCharge")

        await self._async_record_run(run, owns, result)
        return result

    async def async_publish_map(self, scale: int = 5) -> dict:
        """Render the current map and hand it to the add-on for the UI.

        The image and its origin travel together: the same pixel means a
        different place on a map with a different origin, so a picker given one
        without the other would quietly select the wrong point.

        Narrated like any other errand, because Home Assistant answers a failed
        service with a bare 500 and keeps the reason to itself.
        """
        if not self.companion:
            raise HomeAssistantError(
                f"{self.device_name} has no companion add-on configured, "
                "which is where the map is displayed"
            )

        run, owns = await self._async_begin_run("publish_map")
        try:
            meta = await self._async_render_and_upload(run, owns, scale)
        except HomeAssistantError:
            raise
        except BaseException as err:
            # Anything unexpected still leaves the run closed and logged -
            # otherwise the row stays 'running' and the reason is nowhere.
            await self._async_abandon_run(run, owns, err)
            raise
        await self._async_end_run(
            run, owns, True, f"Published a {meta['size'][0]}x{meta['size'][1]} map", {}
        )
        return meta

    async def _async_render_and_upload(
        self, run: RunReporter, owns: bool, scale: int
    ) -> dict:
        async def refuse(summary: str, detail: str) -> HomeAssistantError:
            await self._async_end_run(run, owns, False, summary, {"error": detail})
            return HomeAssistantError(detail)

        await run.step("fetching a map frame")
        await self.hass.async_add_executor_job(self._read_map_frame)
        if not self._last_frame:
            raise await refuse(
                "No map frame",
                f"No map available for {self.device_name}. {self._position_diagnosis()}",
            )

        frame = decode_frame(self._last_frame, self.profile.flag("AES_IV"))
        if frame is None:
            raise await refuse(
                "Could not decode the map",
                f"Could not decode {self.device_name}'s map frame "
                f"({len(self._last_frame)} chars)",
            )

        await run.step(
            f"decoded {frame['width']}x{frame['height']} cells at "
            f"{frame['grid_size']}mm, origin {frame['origin']}"
        )
        png = await self.hass.async_add_executor_job(render_png, frame, scale)
        if not png:
            raise await refuse(
                "Could not render the map", "Could not render the map - is Pillow available?"
            )

        meta = map_metadata(frame, scale)
        document = map_document(
            frame, scale, room_names=decode_room_names(frame.get("trailer") or {})
        )
        await run.step(
            f"rendered {len(png)} bytes, {len(document['grid'])} of grid, uploading"
        )
        if not await self.companion.async_publish_map(self.did, png, meta, document):
            raise await refuse(
                "Upload refused", "The add-on would not accept the map - check its log"
            )
        return meta

    async def async_start_task(self, task: str) -> dict:
        """Run a task the add-on holds, by its human-readable id.

        The steps are expanded by the add-on and performed here as ordinary
        service calls, so a task does exactly what its exported script would.
        """
        if not self.companion:
            raise HomeAssistantError(
                f"{self.device_name} has no companion add-on configured, "
                "which is where tasks live"
            )
        if self._run is not None:
            raise HomeAssistantError(
                f"{self.device_name} is already busy with '{self._run.command}'. "
                "Wait for it to finish, or stop the vacuum"
            )

        # The run opens before anything can fail, so a refusal shows up on the
        # Activity page. Home Assistant returns a bare 500 for a service error
        # and puts the reason only in its own log, which is no use to someone
        # looking at the panel.
        run, owns = await self._async_begin_run(f"task:{task}")

        async def refuse(summary: str, detail: str) -> HomeAssistantError:
            await self._async_end_run(run, owns, False, summary, {"error": detail})
            return HomeAssistantError(detail)

        payload = await self.companion.async_task_calls(task)
        if payload is None:
            raise await refuse("Add-on unreachable", "Could not reach the companion add-on")
        if payload.get("error"):
            available = ", ".join(
                t["slug"] for t in await self.companion.async_list_tasks(self.did)
            )
            raise await refuse(
                f"Cannot run '{task}'",
                f"Cannot run task '{task}': {payload['error']}"
                + (f". Available: {available}" if available else ""),
            )

        definition = payload.get("task") or {}
        if str(definition.get("did")) != str(self.did):
            raise await refuse(
                "Wrong vacuum",
                f"Task '{task}' belongs to another vacuum. Its coordinates are "
                "in that robot's map and would send this one somewhere arbitrary",
            )

        calls = payload.get("calls") or []
        if self.active_task:
            self.active_task.task = task
            self.active_task.total = len(calls)
            self._publish_task_state()
        await run.step(f"{definition.get('name', task)}: {len(calls)} steps")

        try:
            for index, call in enumerate(calls, start=1):
                domain, _, service = call["action"].partition(".")
                data = call.get("data") or {}
                if self.active_task:
                    self.active_task.step = index
                    self.active_task.detail = call["action"]
                    self._publish_task_state()
                await run.step(
                    f"step {index}/{len(calls)}: {call['action']}"
                    + (f" {data}" if data else "")
                )
                await self.hass.services.async_call(
                    domain, service, {**data, **call.get("target", {})}, blocking=True
                )
        except Exception as err:  # noqa: BLE001 - reported, then re-raised
            await self._async_end_run(
                run, owns, False, f"Failed at step {index} of {len(calls)}",
                {"error": str(err)},
            )
            raise HomeAssistantError(f"Task '{task}' failed at step {index}: {err}") from err

        await self._async_end_run(
            run, owns, True, f"Completed {len(calls)} steps", {}
        )
        return {"task": task, "steps": len(calls)}

    def _publish_task_state(self) -> None:
        """Push the live task state out to the entity attributes."""
        self.async_update_listeners()

    async def _async_begin_run(self, command: str) -> tuple[RunReporter, bool]:
        """Start narrating a command, or join the errand already narrating.

        Returns the reporter and whether this caller owns it. A composite like
        inspect_point owns one run and the steps of everything it calls land in
        it, rather than each step spawning its own entry.
        """
        if self._run is not None:
            return self._run, False
        run = RunReporter(self, command)
        await run.start()
        self._run = run
        self.active_task = ActiveTask(run.run_id, None, command)
        self._publish_task_state()
        return run, True

    async def _async_abandon_run(self, run: RunReporter, owns: bool, err: BaseException) -> None:
        """Close a run that an unexpected exception escaped through.

        Without this the row stays 'running' forever, the vacuum looks busy,
        and the reason is nowhere - which is exactly how a failure hides.
        """
        if not owns or self._run is None:
            return
        _LOGGER.exception("Unhandled error during %s", run.command, exc_info=err)
        await self._async_end_run(
            run, owns, False, f"{type(err).__name__}", {"error": f"{type(err).__name__}: {err}"}
        )

    async def _async_end_run(
        self, run: RunReporter, owns: bool, ok: bool, summary: str, detail: dict
    ) -> None:
        if not owns:
            return
        await run.finish(ok, summary, detail)
        self._run = None
        self.active_task = None
        self._publish_task_state()

    async def _async_step(self, text: str) -> None:
        """Report a step if an errand is narrating itself, otherwise nothing.

        Lets the drive and rotation stages narrate without every one of them
        having to know whether a run is being recorded.
        """
        if self._run is not None:
            await self._run.step(text)

    async def _async_record_run(self, run: "RunReporter", owns: bool, result: dict) -> None:
        """Close the run record with an outcome the page can summarise."""
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
        await self._async_end_run(run, owns, bool(result.get("arrived")), summary, detail)

    async def async_take_snapshot(
        self, tag: str | None = None, filename: str | None = None
    ) -> dict:
        """Photograph whatever the vacuum is looking at now.

        Uses a running stream if there is one - a capture that has to
        negotiate its own session competes with anything else holding one, and
        the device allows only one at a time.
        """
        if not self.companion:
            raise HomeAssistantError(
                f"{self.device_name} has no companion add-on configured, "
                "which is needed to take a photo"
            )
        run, owns = await self._async_begin_run("take_snapshot")
        cfg = self.config
        creds = (
            cfg[CONF_USERNAME], cfg[CONF_PASSWORD],
            cfg.get(CONF_COUNTRY, "eu"), cfg.get(CONF_CAMERA_PIN, ""),
        )
        streaming = await self.companion.async_stream_status(self.did)
        await run.step("using the running stream" if streaming else "capturing directly")

        shot = await self._async_capture_to(filename, creds, tag, run)
        await self._async_end_run(
            run, owns, bool(shot),
            f"Saved to {shot.get('media_path')}" if shot else "Photo failed",
            {"photo": (shot or {}).get("path")},
        )
        if not shot:
            raise HomeAssistantError(f"Could not take a photo of {self.device_name}")
        return shot

    async def _async_capture_to(
        self, filename: str | None, creds: tuple, tag: str | None = None,
        run: RunReporter | None = None,
    ) -> dict | None:
        """Take a fresh photo, and optionally also copy it where asked.

        The add-on files it under its tag in the media folder; `filename`
        is an extra copy, for somewhere Home Assistant serves over HTTP.
        """
        shot = await self.companion.async_capture(*creds, self.did, tag)
        if not shot or not shot.get("path"):
            _LOGGER.warning("Could not capture a photo of %s", self.device_name)
            return None

        # The add-on classifies inline and hands the results back on this
        # same response - there is no separate connection or credential for
        # that, since a photo can only ever be taken through this method in
        # the first place (see the module docstring in classify_registry.py).
        await self._async_log_classifications(shot.get("classifications") or [], run)

        result = {
            "path": shot.get("path"),
            "latest": shot.get("latest"),
            "tag": shot.get("tag"),
            "media_path": shot.get("media_path"),
            "latest_media_path": shot.get("latest_media_path"),
        }
        if not filename:
            return result

        image = await self.companion.async_latest_image(self.did)
        if not image:
            return result
        try:
            await self.hass.async_add_executor_job(self._write_image, filename, image)
            result["copy"] = filename
        except OSError as err:
            _LOGGER.error("Could not write the photo to %s: %s", filename, err)
        return result

    async def _async_log_classifications(self, reports: list[dict], run: RunReporter | None) -> None:
        """Narrate what the add-on made of this snapshot's linked
        classifications, and feed anything that cleared its threshold to the
        entity registry.

        `reports` covers every classifier linked to this tag, not just the
        ones that produced a usable result - a classifier that is disabled,
        unconfigured, or not trained yet says so explicitly, rather than
        looking identical to "nothing linked here at all".
        """
        if not reports:
            return
        if run is not None:
            for item in reports:
                if "error" in item:
                    await run.step(f"classification: {item['error']}")
                elif "skipped" in item:
                    await run.step(f"classification '{item['name']}': {item['skipped']}")
                else:
                    verdict = "" if item["passed_threshold"] else " (below threshold)"
                    await run.step(
                        f"classification '{item['name']}': {item['label']} "
                        f"({item['score'] * 100:.0f}%){verdict}"
                    )

        registry = get_registry(self.hass)
        if registry is None:
            return
        for item in reports:
            if item.get("passed_threshold"):
                await registry.async_handle_result(item)

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
                    await self._async_step(f"arrived {int(distance)}mm from target")
                    return
                if closest is None or distance < closest:
                    closest = distance
                _LOGGER.debug(
                    "%s at (%s, %s), %dmm to go", self.device_name, pos["x"], pos["y"], distance
                )
                if ticks % 3 == 0:
                    await self._async_step(f"driving, {int(distance)}mm to go")

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
        damping: float = 0.85,
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
        run, owns = await self._async_begin_run("rotate_to_heading")
        if owns:
            await run.step(f"target {heading:.0f}\u00b0, within {tolerance:.0f}\u00b0")

        # Lenient for the opening read: it only needs to know roughly where the
        # robot is pointing. Insisting on a brand new frame here fails before
        # anything has moved, because a stationary robot may not have uploaded
        # one for a long time.
        current = await self.async_refresh_position(max_age=180)
        if current is None:
            message = (
                f"Could not read {self.device_name}'s heading. "
                f"{self._position_diagnosis()}"
            )
            await self._async_end_run(run, owns, False, "No heading", {"error": message})
            raise HomeAssistantError(message)

        # A camera session stops the firmware promoting the drive into a
        # remote-control cleaning task, which is what runs the brushes. A
        # caller already holding one (a running stream, say) passes
        # use_camera_session=False - the device allows only one at a time, so
        # opening a second would fail and leave the turn noisy.
        camera = None
        if use_camera_session:
            camera = await self._async_open_camera_session(camera_settle)
        elif self.companion and not await self.companion.async_stream_status(self.did):
            # The caller said a stream is holding a session, but none is running
            # - a task's steps are expanded before it runs, so a start_stream
            # step that failed still leaves this flag set. Without this the turn
            # would run the brushes.
            _LOGGER.warning(
                "%s was told a stream holds the camera session, but none is "
                "running - opening one for the turn", self.device_name,
            )
            camera = await self._async_open_camera_session(camera_settle)
        # Collected rather than only logged, so a caller can report it without
        # anyone having to read the log.
        trace: list[str] = [
            f"start {current:.0f}deg, target {heading:.0f}deg"
            + (", camera session open" if camera else ", NO camera session")
        ]
        previous = await self._async_quieten() if quiet and not camera else {}
        failure: str | None = None
        try:
            await self._async_rotate_loop(
                heading, current, tolerance, max_attempts, damping, settle, camera, trace
            )
        except HomeAssistantError as err:
            failure = str(err)
            raise
        finally:
            # Restore even if the rotation raised, or a failed turn would
            # silently leave the vacuum on its quietest setting.
            await self._async_restore(previous)
            if camera:
                await self.hass.async_add_executor_job(camera.stop)
            final = (self.position or {}).get("angle")
            await self._async_end_run(
                run, owns, failure is None,
                f"Facing {final}\u00b0" if failure is None else "Heading not reached",
                {"trace": trace, **({"error": failure} if failure else {})},
            )
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
                "(Settings > Devices & Services > Dreame Vacuum Unlocked Integration > Configure)",
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
                await self._async_step(f"heading reached: {current:.0f}\u00b0, {diff:+.0f} off")
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
            await self._async_step(
                f"turn {attempt}: at {current:.0f}\u00b0, want {diff:+.0f}\u00b0, "
                f"commanding {step / self._turn_overshoot:+.0f}\u00b0"
            )
            if not await self.async_turn_degrees(step):
                raise HomeAssistantError(f"{self.device_name} rejected the rotation command")

            # The nudge is accepted immediately but the robot turns at its own
            # pace. Measuring straight away reads a pose from part-way through
            # the turn, so the next correction is computed against a heading
            # the robot has already left - it chases itself and burns every
            # attempt. Wait longer for bigger turns.
            await asyncio.sleep(min(settle + abs(step) * 0.15, 20.0))

            # Strict, and waits for the robot to stop: this is the measurement
            # the next correction depends on, so it has to reflect where the
            # turn finished rather than a frame from part-way through it.
            measured = await self.async_settled_heading()
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
            achieved = ((measured - current + 180) % 360) - 180
            self._learn_overshoot(step / self._turn_overshoot, achieved)
            line = (
                f"turn {attempt}: settled at {measured:.0f}\u00b0 "
                f"(turned {achieved:+.0f}\u00b0, overshoot {self._turn_overshoot:.2f})"
            )
            trace.append(line)
            await self._async_step(line)
            current = measured

        final = (heading - current) % 360
        if final > 180:
            final -= 360
        if abs(final) <= tolerance:
            trace.append(f"done at {current:.0f}deg, {final:+.0f} off target")
            return

        trace.append(f"gave up at {current:.0f}deg, {final:+.0f} off target")
        await self._async_step(f"gave up at {current:.0f}\u00b0, {final:+.0f} off target")
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

    async def async_clean_rooms(self, room_ids: list[int], times: int = 1) -> bool:
        """Clean a chosen set of rooms, in the order their ids are given.

        Mirrors the phone app's room-cleaning command. The app sends
        `VacuumExtend.startClean(WorkMode.AreaClean, payload)` where the payload
        lists each room as `[roomId, times, cleaningMode, mopMode, 1]` and the
        array order *is* the cleaning order - the vacuum visits the rooms in the
        order they appear. We build the same payload over the same action,
        carrying the mode + payload via PropCleanExtendData exactly as
        `go_to_point` does for its own work mode.

        `times` is how many times to clean each room (the app's selectNum).
        """
        if not room_ids:
            return False
        mode_ids = self.profile.prop_id("VacuumExtend", "PropWorkMode")
        extend_ids = self.profile.prop_id("VacuumExtend", "PropCleanExtendData")
        if mode_ids is None or extend_ids is None:
            _LOGGER.warning(
                "%s has no room-clean vocab (PropWorkMode/PropCleanExtendData)",
                self.model,
            )
            return False

        # Room cleaning is expressed as "startClean with this work mode + a
        # payload naming the rooms". Reuse the current cleaning/mop modes as the
        # app does (each entry carries them), defaulting to sweep when unknown.
        cleaning_mode = self.value("VacuumExtend", "PropCleaningMode")
        mop_mode = self.value("VacuumExtend", "PropMopMode")
        cleaning_mode = cleaning_mode if isinstance(cleaning_mode, int) else 0
        mop_mode = mop_mode if isinstance(mop_mode, int) else 0

        selects = [
            [int(rid), int(times), int(cleaning_mode), int(mop_mode), 1]
            for rid in room_ids
        ]
        params = json.dumps({"selects": selects}, separators=(",", ":"))
        return await self.async_action(
            "VacuumExtend",
            "startClean",
            [
                {"piid": mode_ids[1], "value": AREA_CLEAN_MODE},
                {"piid": extend_ids[1], "value": params},
            ],
        )

    def _ensure_custom_voice_pack(self) -> tuple[str, int] | None:
        """Make sure the pack file the robot will fetch exists under config/www.

        The device downloads the pack itself from the URL, so the file must be
        served by Home Assistant at /local/. For now we place an empty
        gzip(tar) at config/www/dreame_vacuum_unlocked/audio/upload.tar.gz so
        that URL resolves; returns (md5, size) of whatever is on disk.
        """
        import gzip
        import hashlib
        import os

        try:
            path = self.hass.config.path(
                "www", "dreame_vacuum_unlocked", "audio", "upload.tar.gz"
            )
            if not os.path.exists(path):
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with gzip.open(path, "wb") as fh:
                    fh.write(b"")
            with open(path, "rb") as fh:
                data = fh.read()
            return hashlib.md5(data).hexdigest(), len(data)
        except Exception as err:  # noqa: BLE001 - best effort, never fail the apply
            _LOGGER.warning("Could not write custom voice pack under www: %s", err)
            return None

    async def async_set_custom_voice(self, url: str, md5: str = "", size: int = 0) -> bool:
        """Install a custom voice pack (id 'CU') on the robot.

        The device downloads the pack itself from `url` (which it must be able
        to reach over the internet), verifies md5, and switches to it. The app
        does the same when switching voice packs (PropSetVoice -> switchVoicePack).
        If md5/size are not supplied we compute them from the pack file we
        place under config/www.
        """
        checksum = await self.hass.async_add_executor_job(self._ensure_custom_voice_pack)
        if checksum and not md5:
            md5, size = checksum
        payload = json.dumps(
            {"id": "CU", "url": url, "md5": md5 or "", "size": int(size or 0)},
            separators=(",", ":"),
        )
        return await self.async_set("Audio", "PropSetVoice", payload)

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
                "entities": self._entity_ids(),
            }
        ]
        if await self.companion.async_register(self.entry.entry_id, payload):
            _LOGGER.debug("Registered %s with companion add-on", self.device_name)

    def _entity_ids(self) -> dict[str, str]:
        """Our entity ids, read from the registry rather than guessed.

        Building them from the device name was wrong twice over: Home Assistant
        may deduplicate an id, and a user who renames an entity keeps the old
        one. The registry is the only place that actually knows. Keyed by the
        suffix each entity was created with, which is also the vocabulary the
        add-on's steps refer to ("stream", "vacuum", ...).
        """
        registry = er.async_get(self.hass)
        prefix = f"{self.did}_"
        out: dict[str, str] = {}
        for entry in er.async_entries_for_config_entry(registry, self.entry.entry_id):
            if entry.unique_id and entry.unique_id.startswith(prefix):
                out[entry.unique_id[len(prefix):]] = entry.entity_id
        return out

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
