"""Serve one historical backup map document to the Maps tab.

The companion add-on's Maps tab lets a user expand a backup row; this is the
endpoint that answers it. Same reasoning as DreameMapView and DreameMapsView:
the cloud login and protocol client live in the integration, so whoever draws
a map fetches through Home Assistant's own API rather than re-logging in.
"""
from __future__ import annotations

import logging

from aiohttp import web

from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

BACKUP_MAP_API_PATH = (
    "/api/dreame_vacuum_unlocked_integration/maps/{did}/backup/{map_id}/{time}"
)


class DreameBackupMapView(HomeAssistantView):
    """GET /api/dreame_vacuum_unlocked_integration/maps/<did>/backup/<map_id>/<time>"""

    url = BACKUP_MAP_API_PATH
    name = "api:dreame_vacuum_unlocked_integration:maps:backup"
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    def _coordinator(self, did: str):
        for coordinator in (self.hass.data.get(DOMAIN) or {}).values():
            if str(coordinator.did) == str(did):
                return coordinator
        return None

    async def get(
        self, request: web.Request, did: str, map_id: str, time: str
    ) -> web.Response:
        coordinator = self._coordinator(did)
        if coordinator is None:
            return self.json_message("No such vacuum", 404)

        try:
            map_id_int = int(map_id)
            time_int = int(time)
        except (TypeError, ValueError):
            return self.json_message("Bad map or backup id", 400)

        try:
            document = await coordinator.async_backup_map_document(
                map_id_int, time_int
            )
        except Exception as err:  # noqa: BLE001 - a broken backup must not 500
            _LOGGER.warning("Could not build map document for %s/%s: %s", map_id, time, err)
            return self.json_message(f"Could not read the backup map: {err}", 502)

        if document is None:
            return self.json_message("That backup map could not be decoded", 404)
        return self.json(document)
