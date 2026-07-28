"""Vacuum entity.

Commands map onto actions from the generated profile rather than hardcoded
siid/aiid pairs, so a model whose ids differ works without code changes:

  start           -> VacuumExtend.startClean
  pause / stop    -> Vacuum.StopSweeping
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
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_platform
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import DreameCoordinator
from .entity import DreameEntity

_LOGGER = logging.getLogger(__name__)

# Values from the device's own status enum.
STATUS_IDLE = 0
STATUS_PAUSED = 1
STATUS_CLEANING = 2
STATUS_BACK_HOME = 3
STATUS_PARTIAL_CLEANING = 4
STATUS_CHARGING = 6
STATUS_ERROR = 12
STATUS_SLEEPING = 14
STATUS_STANDBY = 17
STATUS_SEGMENT_CLEANING = 18

CLEANING_STATES = {
    STATUS_CLEANING,
    STATUS_PARTIAL_CLEANING,
    STATUS_SEGMENT_CLEANING,
}

CHARGING_STATUS_CHARGING = 1

SERVICE_ROTATE_TO_HEADING = "rotate_to_heading"
ATTR_HEADING = "heading"
ATTR_TOLERANCE = "tolerance"
ATTR_MAX_ATTEMPTS = "max_attempts"
ATTR_DAMPING = "damping"

SERVICE_GO_TO_POINT = "go_to_point"
ATTR_X = "x"
ATTR_Y = "y"
ATTR_ARRIVAL_TOLERANCE = "arrival_tolerance"
ATTR_TIMEOUT = "timeout"


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
            vol.Optional(ATTR_DAMPING, default=0.3): vol.All(
                vol.Coerce(float), vol.Range(min=0.1, max=1)
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
            vol.Optional(ATTR_ARRIVAL_TOLERANCE, default=250): vol.All(
                vol.Coerce(int), vol.Range(min=50, max=2000)
            ),
            vol.Optional(ATTR_TIMEOUT, default=180): vol.All(
                vol.Coerce(float), vol.Range(min=10, max=600)
            ),
        },
        "async_go_to_point",
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
        if status in CLEANING_STATES:
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
            "device_state": c.value("Vacuum", "PropVacuumStatus"),
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
        # The device exposes stop rather than a distinct pause action; HA's
        # pause maps onto it so the button behaves as users expect.
        await self.coordinator.async_action("Vacuum", "StopSweeping")

    async def async_stop(self, **kwargs) -> None:
        await self.coordinator.async_action("Vacuum", "StopSweeping")

    async def async_return_to_base(self, **kwargs) -> None:
        await self.coordinator.async_action("Battery", "StartCharge")

    async def async_locate(self, **kwargs) -> None:
        await self.coordinator.async_action("Audio", "position")

    async def async_rotate_to_heading(
        self,
        heading: float,
        tolerance: float = 1.0,
        max_attempts: int = 10,
        damping: float = 0.3,
    ) -> None:
        await self.coordinator.async_rotate_to_heading(
            heading, tolerance=tolerance, max_attempts=max_attempts, damping=damping
        )

    async def async_go_to_point(
        self,
        x: int,
        y: int,
        heading: float | None = None,
        arrival_tolerance: int = 250,
        timeout: float = 180.0,
    ) -> None:
        await self.coordinator.async_go_to_point(
            x, y, heading=heading, arrival_tolerance=arrival_tolerance, timeout=timeout
        )
