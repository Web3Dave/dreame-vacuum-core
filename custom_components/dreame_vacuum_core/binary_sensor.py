"""Binary sensors.

Currently just the per-class on/off entities the classification registry
creates lazily as results arrive - there are no static binary sensors for
the vacuum itself yet, so this platform exists only to give the registry
something to add entities through.
"""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .classify_registry import get_registry


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    registry = get_registry(hass)
    if registry is not None:
        registry.register_binary_adder(async_add_entities)
