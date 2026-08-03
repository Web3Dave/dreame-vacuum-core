"""Sensors.

Only created when the profile knows the property AND the device actually
answered for it. Presence comes from the coordinator's probe, never from the
capability manifest - the manifest lists features, not services.
"""
from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .classify_registry import get_registry
from .const import DOMAIN
from .coordinator import DreameCoordinator
from .entity import DreameEntity


@dataclass(frozen=True, kw_only=True)
class DreameSensorDescription(SensorEntityDescription):
    service: str
    prop: str


SENSORS: tuple[DreameSensorDescription, ...] = (
    DreameSensorDescription(
        key="battery",
        name="Battery",
        service="Battery",
        prop="PropBatteryLevel",
        device_class=SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=PERCENTAGE,
    ),
    DreameSensorDescription(
        key="volume",
        name="Volume",
        service="Audio",
        prop="PropVolume",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=PERCENTAGE,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: DreameCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities = []
    for desc in SENSORS:
        if coordinator.profile.prop_id(desc.service, desc.prop) is None:
            continue  # not even in the vocabulary
        if coordinator.is_present(desc.service, desc.prop) is False:
            continue  # probed and absent on this unit
        entities.append(DreameSensor(coordinator, desc))
    async_add_entities(entities)

    # Classification sensors (state + last-updated) are created lazily by
    # the registry as results arrive, not listed here - there is no way to
    # know a classifier's existence ahead of time, since it is authored
    # entirely in the companion add-on's own UI.
    registry = get_registry(hass)
    if registry is not None:
        registry.register_sensor_adder(async_add_entities)


class DreameSensor(DreameEntity, SensorEntity):
    entity_description: DreameSensorDescription

    def __init__(self, coordinator: DreameCoordinator, desc: DreameSensorDescription) -> None:
        super().__init__(coordinator, desc.key)
        self.entity_description = desc

    @property
    def native_value(self):
        value = self.coordinator.value(self.entity_description.service, self.entity_description.prop)
        try:
            return int(value)
        except (TypeError, ValueError):
            return value
