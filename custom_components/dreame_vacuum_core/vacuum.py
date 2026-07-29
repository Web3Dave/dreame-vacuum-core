"""Vacuum entity.

Commands map onto actions from the generated profile rather than hardcoded
siid/aiid pairs, so a model whose ids differ works without code changes:

  start           -> VacuumExtend.startClean
  pause           -> Vacuum.StopSweeping   (pause, despite the name)
  stop            -> VacuumExtend.stopClean
  return_to_base  -> Battery.StartCharge
  locate          -> Audio.position   (the robot announces itself)
"""
from __future__ import annotations

import logging

import voluptuous as vol
from homeassistant.components.vacuum import (
    StateVacuumEntity,
    VacuumActivity,
    VacuumEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, SupportsResponse
from homeassistant.helpers import config_validation as cv, entity_platform
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import DreameCoordinator
from .entity import DreameEntity

_LOGGER = logging.getLogger(__name__)

# Work modes (siid 4 piid 1). The app's plugin bundle lists 27 of these; the
# five marked below appear only in the upstream fork, which was built across
# many more models - so the bundle is not the whole story.
STATUS_IDLE = 0
STATUS_PAUSED = 1            # PauseAndStopMode
STATUS_AUTO_CLEAN = 2
STATUS_BACK_HOME = 3
STATUS_PART_CLEAN = 4
STATUS_FOLLOW_WALL = 5
STATUS_CHARGING = 6
STATUS_OTA = 7
STATUS_ERROR = 12            # ErrRepotMode
STATUS_REMOTE_CONTROL = 13
STATUS_SLEEPING = 14
STATUS_SELF_TEST = 15
STATUS_STANDBY = 17
STATUS_AREA_CLEAN = 18
STATUS_CUSTOM_AREA_CLEAN = 19
STATUS_SPOT_CLEAN = 20
STATUS_FAST_MAPPING = 21
STATUS_MONITOR_CRUISE = 22
STATUS_MONITOR_SPOT = 23     # the mode go_to_point uses
STATUS_SUMMON_CLEAN = 24
STATUS_SHORTCUT = 25           # not in the app bundle
STATUS_PERSON_FOLLOW = 26
STATUS_PET_GUARDING = 27       # not in the app bundle
STATUS_AUTO_ARRANGEMENT = 28   # not in the app bundle
STATUS_SMART_ARRANGEMENT = 29  # not in the app bundle
STATUS_ZONED_ARRANGEMENT = 30  # not in the app bundle
STATUS_WATER_SELF_CHECK = 1501

CLEANING_STATES = {
    STATUS_AUTO_CLEAN,
    STATUS_PART_CLEAN,
    STATUS_FOLLOW_WALL,
    STATUS_AREA_CLEAN,
    STATUS_CUSTOM_AREA_CLEAN,
    STATUS_SPOT_CLEAN,
    STATUS_FAST_MAPPING,
    STATUS_SUMMON_CLEAN,
    STATUS_SHORTCUT,
    STATUS_AUTO_ARRANGEMENT,
    STATUS_SMART_ARRANGEMENT,
    STATUS_ZONED_ARRANGEMENT,
}

# Driving under our own control, or following something - moving, but not
# cleaning, so "cleaning" would misreport it.
MOVING_STATES = {
    STATUS_REMOTE_CONTROL,
    STATUS_MONITOR_CRUISE,
    STATUS_MONITOR_SPOT,
    STATUS_PERSON_FOLLOW,
    STATUS_PET_GUARDING,
}

# A second, finer-grained enum on siid 2 piid 1 - what the app shows as the
# activity label. Distinct from the work mode above and far more detailed, so
# it is surfaced as a name rather than a bare number.
DEVICE_STATES: dict[int, str] = {
    1: "sweeping",
    2: "idle",
    3: "paused",
    4: "error",
    5: "returning",
    6: "charging",
    7: "mopping",
    8: "drying",
    9: "washing",
    10: "returning_to_wash",
    11: "building",
    12: "sweeping_and_mopping",
    13: "charging_completed",
    14: "upgrading",
    15: "clean_summon",
    16: "station_reset",
    17: "returning_install_mop",
    18: "returning_remove_mop",
    19: "water_check",
    20: "clean_add_water",
    21: "washing_paused",
    22: "auto_emptying",
    23: "remote_control",
    24: "smart_charging",
    25: "second_cleaning",
    26: "human_following",
    27: "spot_cleaning",
    28: "returning_auto_empty",
    29: "waiting_for_task",
    30: "station_cleaning",
    31: "returning_to_drain",
    32: "draining",
    33: "auto_water_draining",
    34: "emptying",
    35: "dust_bag_drying",
    36: "dust_bag_drying_paused",
    37: "heading_to_extra_cleaning",
    38: "extra_cleaning",
    95: "finding_pet_paused",
    96: "finding_pet",
    97: "shortcut",
    98: "monitoring",
    99: "monitoring_paused",
    101: "initial_deep_cleaning",
    102: "initial_deep_cleaning_paused",
    103: "sanitizing",
    104: "sanitizing_with_dry",
    105: "changing_mop",
    106: "changing_mop_paused",
    107: "floor_maintaining",
    108: "floor_maintaining_paused",
    109: "remote_pickup",
    113: "arranging_items",
    114: "pet_guarding",
    115: "pet_guarding_paused",
    116: "installing_mop",
    117: "uninstalling_mop",
    118: "intelligent_recharging",
    120: "assisted_cleaning",
    121: "entering_dock",
    122: "leaving_dock",
    140: "navigating_to_climber",
    141: "docking_to_climber",
    142: "climber_docked",
    143: "climber_navigating",
    144: "climbing_stairs",
    145: "climbing_stairs_completed",
    146: "climber_at_dock",
    147: "climber_leaving_dock",
}

CHARGING_STATUS_CHARGING = 1

SERVICE_ROTATE_TO_HEADING = "rotate_to_heading"
ATTR_HEADING = "heading"
ATTR_TOLERANCE = "tolerance"
ATTR_MAX_ATTEMPTS = "max_attempts"
ATTR_DAMPING = "damping"
ATTR_SETTLE = "settle"
ATTR_QUIET = "quiet"
ATTR_CAMERA_SETTLE = "camera_settle"

SERVICE_GO_TO_POINT = "go_to_point"
ATTR_X = "x"
ATTR_Y = "y"
ATTR_ARRIVAL_TOLERANCE = "arrival_tolerance"
ATTR_TIMEOUT = "timeout"
ATTR_HEADING_TOLERANCE = "heading_tolerance"

SERVICE_REMOTE_CONTROL = "remote_control"
ATTR_ROTATION = "rotation"
ATTR_VELOCITY = "velocity"
ATTR_DURATION = "duration"
ATTR_SILENT = "silent"

SERVICE_INSPECT_POINT = "inspect_point"
ATTR_FILENAME = "filename"
ATTR_RETURN_TO_DOCK = "return_to_dock"
ATTR_USE_CAMERA_SESSION = "use_camera_session"


def _device_state_name(value) -> str | None:
    """Name the state where we know it, otherwise pass the number through."""
    try:
        code = int(value)
    except (TypeError, ValueError):
        return None
    return DEVICE_STATES.get(code, str(code))


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: DreameCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([DreameVacuum(coordinator)])

    platform = entity_platform.async_get_current_platform()
    platform.async_register_entity_service(
        SERVICE_ROTATE_TO_HEADING,
        {
            vol.Required(ATTR_HEADING): vol.All(vol.Coerce(float), vol.Range(min=0, max=359)),
            vol.Optional(ATTR_TOLERANCE, default=1): vol.All(
                vol.Coerce(float), vol.Range(min=1, max=30)
            ),
            vol.Optional(ATTR_MAX_ATTEMPTS, default=10): vol.All(
                vol.Coerce(int), vol.Range(min=1, max=20)
            ),
            vol.Optional(ATTR_DAMPING, default=0.6): vol.All(
                vol.Coerce(float), vol.Range(min=0.1, max=1)
            ),
            vol.Optional(ATTR_SETTLE, default=4): vol.All(
                vol.Coerce(float), vol.Range(min=0, max=15)
            ),
            vol.Optional(ATTR_QUIET, default=True): cv.boolean,
            vol.Optional(ATTR_USE_CAMERA_SESSION, default=True): cv.boolean,
            vol.Optional(ATTR_CAMERA_SETTLE): vol.All(
                vol.Coerce(float), vol.Range(min=0, max=20)
            ),
        },
        "async_rotate_to_heading",
    )
    platform.async_register_entity_service(
        SERVICE_GO_TO_POINT,
        {
            vol.Required(ATTR_X): vol.Coerce(int),
            vol.Required(ATTR_Y): vol.Coerce(int),
            # Optional: omit to arrive without caring which way it faces.
            vol.Optional(ATTR_HEADING): vol.All(vol.Coerce(float), vol.Range(min=0, max=359)),
            vol.Optional(ATTR_HEADING_TOLERANCE, default=1): vol.All(
                vol.Coerce(float), vol.Range(min=1, max=30)
            ),
            vol.Optional(ATTR_ARRIVAL_TOLERANCE, default=250): vol.All(
                vol.Coerce(int), vol.Range(min=1, max=2000)
            ),
            vol.Optional(ATTR_TIMEOUT, default=180): vol.All(
                vol.Coerce(float), vol.Range(min=10, max=600)
            ),
        },
        "async_go_to_point",
    )
    platform.async_register_entity_service(
        SERVICE_REMOTE_CONTROL,
        {
            vol.Optional(ATTR_ROTATION, default=0): vol.All(
                vol.Coerce(int), vol.Range(min=-360, max=360)
            ),
            vol.Optional(ATTR_VELOCITY, default=0): vol.All(
                vol.Coerce(int), vol.Range(min=-400, max=400)
            ),
            vol.Optional(ATTR_DURATION, default=0): vol.All(
                vol.Coerce(float), vol.Range(min=0, max=30)
            ),
            vol.Optional(ATTR_SILENT, default=True): cv.boolean,
        },
        "async_remote_control",
    )
    platform.async_register_entity_service(
        SERVICE_INSPECT_POINT,
        {
            vol.Required(ATTR_X): vol.Coerce(int),
            vol.Required(ATTR_Y): vol.Coerce(int),
            vol.Optional(ATTR_HEADING): vol.All(vol.Coerce(float), vol.Range(min=0, max=359)),
            vol.Optional(ATTR_FILENAME): cv.string,
            vol.Optional(ATTR_ARRIVAL_TOLERANCE, default=250): vol.All(
                vol.Coerce(int), vol.Range(min=1, max=2000)
            ),
            vol.Optional(ATTR_HEADING_TOLERANCE, default=5): vol.All(
                vol.Coerce(float), vol.Range(min=1, max=30)
            ),
            vol.Optional(ATTR_TIMEOUT, default=180): vol.All(
                vol.Coerce(float), vol.Range(min=10, max=600)
            ),
            vol.Optional(ATTR_RETURN_TO_DOCK, default=True): cv.boolean,
        },
        "async_inspect_point",
        supports_response=SupportsResponse.OPTIONAL,
    )


class DreameVacuum(DreameEntity, StateVacuumEntity):
    _attr_name = None  # primary entity for the device
    _attr_supported_features = (
        VacuumEntityFeature.STATE
        | VacuumEntityFeature.START
        | VacuumEntityFeature.PAUSE
        | VacuumEntityFeature.STOP
        | VacuumEntityFeature.RETURN_HOME
        | VacuumEntityFeature.LOCATE
    )

    def __init__(self, coordinator: DreameCoordinator) -> None:
        super().__init__(coordinator, "vacuum")

    @property
    def activity(self) -> VacuumActivity | None:
        raw = self.coordinator.value("VacuumExtend", "PropWorkMode")
        try:
            status = int(raw)
        except (TypeError, ValueError):
            return None

        if status == STATUS_ERROR:
            return VacuumActivity.ERROR
        if status in CLEANING_STATES or status in MOVING_STATES:
            return VacuumActivity.CLEANING
        if status == STATUS_PAUSED:
            return VacuumActivity.PAUSED
        if status == STATUS_BACK_HOME:
            return VacuumActivity.RETURNING
        if status == STATUS_CHARGING:
            return VacuumActivity.DOCKED

        if status in (STATUS_IDLE, STATUS_SLEEPING, STATUS_STANDBY):
            # Idle on the dock should read as docked, which the status alone
            # doesn't distinguish.
            charging = self.coordinator.value("Battery", "PropChargingState")
            try:
                if int(charging) == CHARGING_STATUS_CHARGING:
                    return VacuumActivity.DOCKED
            except (TypeError, ValueError):
                pass
            return VacuumActivity.IDLE

        _LOGGER.debug("Unmapped vacuum status %s for %s", status, self.coordinator.model)
        return None

    @property
    def extra_state_attributes(self) -> dict:
        c = self.coordinator
        attrs = {
            "model": c.model,
            "did": c.did,
            "work_mode": c.value("VacuumExtend", "PropWorkMode"),
            "device_state": _device_state_name(c.value("Vacuum", "PropVacuumStatus")),
            "fault": c.value("Vacuum", "PropVacuumFault"),
            "profiled": c.profile.profiled,
        }

        # Pose, when a map frame has been seen. x/y are millimetres in the
        # map's own frame of reference and angle is degrees; both are only
        # meaningful relative to `map_id`, which changes when the map is
        # rebuilt.
        if c.position:
            attrs.update(
                {
                    "map_id": c.position.get("map_id"),
                    "position_x": c.position.get("x"),
                    "position_y": c.position.get("y"),
                    "heading": c.position.get("angle"),
                    "charger_x": c.position.get("charger_x"),
                    "charger_y": c.position.get("charger_y"),
                }
            )
            # Distinct from "no frame yet": the robot is on the map but can't
            # place itself, which is worth surfacing rather than hiding.
            attrs["located"] = c.position.get("x") is not None

        return {k: v for k, v in attrs.items() if v is not None}

    async def async_start(self) -> None:
        await self.coordinator.async_action("VacuumExtend", "startClean")

    async def async_pause(self) -> None:
        await self.coordinator.async_action("Vacuum", "StopSweeping")

    async def async_stop(self, **kwargs) -> None:
        # Ends the task. Vacuum.StopSweeping only pauses it, which left stop
        # and pause doing the same thing.
        await self.coordinator.async_action("VacuumExtend", "stopClean")

    async def async_return_to_base(self, **kwargs) -> None:
        await self.coordinator.async_action("Battery", "StartCharge")

    async def async_locate(self, **kwargs) -> None:
        await self.coordinator.async_action("Audio", "position")

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
    ) -> None:
        await self.coordinator.async_rotate_to_heading(
            heading,
            tolerance=tolerance,
            max_attempts=max_attempts,
            damping=damping,
            settle=settle,
            quiet=quiet,
            camera_settle=camera_settle,
            use_camera_session=use_camera_session,
        )

    async def async_go_to_point(
        self,
        x: int,
        y: int,
        heading: float | None = None,
        heading_tolerance: float = 1.0,
        arrival_tolerance: int = 250,
        timeout: float = 180.0,
    ) -> None:
        await self.coordinator.async_go_to_point(
            x,
            y,
            heading=heading,
            heading_tolerance=heading_tolerance,
            arrival_tolerance=arrival_tolerance,
            timeout=timeout,
        )

    async def async_remote_control(
        self, rotation: int = 0, velocity: int = 0, duration: float = 0.0,
        silent: bool = True,
    ) -> None:
        await self.coordinator.async_remote_control(
            rotation=rotation, velocity=velocity, duration=duration, silent=silent
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
        return await self.coordinator.async_inspect_point(
            x, y, heading=heading, filename=filename,
            arrival_tolerance=arrival_tolerance,
            heading_tolerance=heading_tolerance,
            timeout=timeout, return_to_dock=return_to_dock,
        )
