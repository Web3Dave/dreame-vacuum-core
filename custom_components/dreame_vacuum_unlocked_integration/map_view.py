"""Serve the map document to the dashboard card.

The add-on already serves a copy, but a card should not need the add-on
installed to draw a map - the integration is what talks to the vacuum, so it
is what answers here. Same document either way; `map.js` cannot tell them
apart.

Authenticated like any other Home Assistant API: the card fetches it through
`hass.callApi`, which attaches the user's token.
"""
from __future__ import annotations

import logging

from aiohttp import web

from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

MAP_API_PATH = "/api/dreame_vacuum_unlocked_integration/map/{did}"


class DreameMapView(HomeAssistantView):
    """GET /api/dreame_vacuum_unlocked_integration/map/<did>[?refresh=1]"""

    url = MAP_API_PATH
    name = "api:dreame_vacuum_unlocked_integration:map"
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

        # Refreshing costs a round trip to the device, so it is opt-in: the
        # card asks for it on the button, not on every redraw.
        refresh = request.query.get("refresh") in ("1", "true", "yes")
        try:
            document = await coordinator.async_map_document(refresh=refresh)
        except Exception as err:  # noqa: BLE001 - a broken map must not 500
            _LOGGER.warning("Could not build a map document for %s: %s", did, err)
            return self.json_message(f"Could not read the map: {err}", 502)

        if document is None:
            # The diagnosis is the useful part - "no map" is usually "this
            # model keeps its map in the cloud and has not pushed a frame".
            return self.json_message(
                f"No map available for {coordinator.device_name}. "
                f"{coordinator.position_diagnosis()}",
                404,
            )
        return self.json(document)
