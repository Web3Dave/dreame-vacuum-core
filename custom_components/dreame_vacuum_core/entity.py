"""Shared entity base."""
from __future__ import annotations

from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .coordinator import DreameCoordinator


class DreameEntity(CoordinatorEntity[DreameCoordinator]):
    """Groups every entity under one device and inherits push updates."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: DreameCoordinator, key: str) -> None:
        super().__init__(coordinator)
        self._key = key
        self._attr_unique_id = f"{coordinator.did}_{key}"
        self._attr_device_info = coordinator.device_info

    @property
    def _profile(self):
        return self.coordinator.profile
