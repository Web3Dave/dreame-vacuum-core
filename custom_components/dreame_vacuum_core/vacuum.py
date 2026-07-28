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

from homeassistant.components.vacuum import (
    StateVacuumEntity,
    VacuumActivity,
    VacuumEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
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


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: DreameCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([DreameVacuum(coordinator)])


class DreameVacuum(DreameEntity, StateVacuumEntity):
    _attr_name = None  # primary entity for the device
    _attr_supported_features = (
        VacuumEntityFeature.STATE
        | VacuumEntityFeature.START
        | VacuumEntityFeature.PAUSE
        | VacuumEntityFeature.STOP
        | VacuumEntityFeature.RETURN_HOME
        | VacuumEntityFeature.LOCATE
        | VacuumEntityFeature.BATTERY
    )

    def __init__(self, coordinator: DreameCoordinator) -> None:
        super().__init__(coordinator, "vacuum")

    @property
    def battery_level(self) -> int | None:
        value = self.coordinator.value("Battery", "PropBatteryLevel")
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

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
