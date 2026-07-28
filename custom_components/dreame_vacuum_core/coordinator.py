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

import logging
import time
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from datetime import timedelta

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
        self._map_ids = self.profile.prop_id("CleanMap", "PropMapdata")

        # siid.piid -> bool; None until probed
        self._present: dict[str, bool] = {}
        self._last_change = time.time()
        self._last_failure: float | None = None
        self._keep_alive_cancel = None

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
        return await self.async_action(
            "CleanMap", "mapReq", [{"piid": ids[1], "value": '{"frame_type":"I"}'}]
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
        ok = isinstance(result, list) and result and result[0].get("code") == 0
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
        ok = bool(result and result.get("code") == 0)
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
