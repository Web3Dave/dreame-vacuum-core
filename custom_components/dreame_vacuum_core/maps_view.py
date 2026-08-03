"""Serve the list of maps and backups for a vacuum.

The companion add-on's Maps tab is the client - a listing page has no
reason to talk to the Dreame cloud itself when the integration already
holds the login session and knows how to read this. Same reasoning as
DreameMapView: whichever thing draws it fetches through Home Assistant's
own API rather than duplicating the cloud client.
"""
from __future__ import annotations

import logging

from aiohttp import web

from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

MAPS_API_PATH = "/api/dreame_vacuum_core/maps/{did}"


class DreameMapsView(HomeAssistantView):
    """GET /api/dreame_vacuum_core/maps/<did>"""

    url = MAPS_API_PATH
    name = "api:dreame_vacuum_core:maps"
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    def _coordinator(self, did: str):
        for coordinator in (self.hass.data.get(DOMAIN) or {}).values():
            if str(coordinator.did) == str(did):
                return coordinator
        return None

    async def get(self, request: web.Request, did: str) -> web.Response:
        coordinator = self._coordinator(did)
        if coordinator is None:
            return self.json_message("No such vacuum", 404)

        try:
            result = await coordinator.async_list_maps()
        except Exception as err:  # noqa: BLE001 - a broken listing must not 500
            _LOGGER.warning("Could not list maps for %s: %s", did, err)
            return self.json_message(f"Could not read maps: {err}", 502)

        return self.json(result)
