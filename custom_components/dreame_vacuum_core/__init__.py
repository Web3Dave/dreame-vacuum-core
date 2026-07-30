"""The Dreame Vacuum Core integration."""
from __future__ import annotations

import logging

from pathlib import Path

from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .const import CONF_MODEL, DOMAIN
from .coordinator import DreameCoordinator
from .profile import load_profile

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.VACUUM,
    Platform.SENSOR,
    Platform.CAMERA,
    Platform.BUTTON,
    Platform.SWITCH,
]


# Served to whoever draws a map - the add-on's panel today, a Lovelace card
# next. One copy so the coordinate transform cannot drift between them, which
# is exactly how a click once landed mirrored about the middle of the map.
MAP_MODULE_URL = "/dreame_vacuum_core/map.js"


async def _async_serve_map_module(hass: HomeAssistant) -> None:
    if hass.data.get(f"{DOMAIN}_static"):
        return
    hass.data[f"{DOMAIN}_static"] = True
    await hass.http.async_register_static_paths(
        [
            StaticPathConfig(
                MAP_MODULE_URL,
                str(Path(__file__).parent / "www" / "map.js"),
                # Cached by the browser; callers bust it with the version.
                True,
            )
        ]
    )


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    await _async_serve_map_module(hass)

    model = {**entry.data, **entry.options}.get(CONF_MODEL) or "unknown"
    profile = await hass.async_add_executor_job(load_profile, model)

    coordinator = DreameCoordinator(hass, entry, profile)
    await coordinator.async_setup()
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Tell the companion add-on which devices are ours. It is authoritative
    # information the add-on cannot reliably infer, so we push it rather than
    # making the add-on guess from the entity registry.
    entry.async_create_background_task(
        hass, coordinator.async_register_with_companion(), name=f"{DOMAIN}_register"
    )

    # The device only pushes map frames while it is moving, so a vacuum that
    # has been parked on its dock since before HA started would report no
    # position at all until its next clean. Ask once at startup.
    entry.async_create_background_task(
        hass, coordinator.async_request_map(), name=f"{DOMAIN}_map"
    )

    entry.async_on_unload(entry.add_update_listener(_async_reload))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        coordinator: DreameCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
        await coordinator.async_shutdown_device()
    return unloaded


async def _async_reload(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
