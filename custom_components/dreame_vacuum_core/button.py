"""Buttons for one-shot actions."""
from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_CAMERA_PIN, CONF_COUNTRY, CONF_PASSWORD, CONF_USERNAME, DOMAIN
from .coordinator import DreameCoordinator
from .entity import DreameEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: DreameCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[ButtonEntity] = []
    if coordinator.companion:
        entities.append(DreameSnapshotButton(coordinator))
    async_add_entities(entities)


class DreameSnapshotButton(DreameEntity, ButtonEntity):
    """One-shot camera capture, performed by the companion add-on."""

    _attr_name = "Take snapshot"
    _attr_icon = "mdi:camera"

    def __init__(self, coordinator: DreameCoordinator) -> None:
        super().__init__(coordinator, "snapshot")

    async def async_press(self) -> None:
        c = self.coordinator
        data = c.config
        path = await c.companion.async_capture(
            data[CONF_USERNAME],
            data[CONF_PASSWORD],
            data.get(CONF_COUNTRY, "eu"),
            data.get(CONF_CAMERA_PIN, ""),
            c.did,
        )
        if path:
            _LOGGER.debug("Snapshot saved: %s", path)
