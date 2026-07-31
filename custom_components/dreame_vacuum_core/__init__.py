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
from .map_view import DreameMapView
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
MAP_STATIC_URL = "/dreame_vacuum_core"
MAP_MODULE_URL = f"{MAP_STATIC_URL}/map.js"
MAP_CARD_URL = f"{MAP_STATIC_URL}/dreame-map-card.js"

# Bumped whenever the served JavaScript changes, so a browser holding the old
# file fetches the new one instead of failing in a way that looks like a bug
# in the integration.
FRONTEND_VERSION = "7"


async def _async_serve_frontend(hass: HomeAssistant) -> None:
    """Serve the `www` folder and register the dashboard card.

    The whole folder rather than one file: the renderer draws the vacuum and
    dock with the phone app's own sprites, which it loads relative to its own
    URL, so they have to be reachable beside it.

    The card is registered here rather than left to the user to add under
    Settings > Dashboards > Resources, because a resource pointing at an
    integration that has been removed is a confusing thing to leave behind.
    """
    if hass.data.get(f"{DOMAIN}_static"):
        return
    hass.data[f"{DOMAIN}_static"] = True

    await hass.http.async_register_static_paths(
        [
            StaticPathConfig(
                MAP_STATIC_URL,
                str(Path(__file__).parent / "www"),
                # Cached by the browser; busted by FRONTEND_VERSION.
                True,
            )
        ]
    )
    hass.http.register_view(DreameMapView(hass))

    # Loads the card on every dashboard without a manual resource entry.
    # Guarded: `frontend` is all but guaranteed, but a headless install can
    # run without it and the map is not worth failing setup over.
    try:
        from homeassistant.components.frontend import add_extra_js_url

        add_extra_js_url(hass, f"{MAP_CARD_URL}?v={FRONTEND_VERSION}")
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning("Could not register the map card with the frontend: %s", err)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    await _async_serve_frontend(hass)

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
