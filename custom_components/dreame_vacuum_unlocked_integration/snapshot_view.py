"""Serve a captured snapshot to whoever Home Assistant has already authenticated.

Built for one job in particular: a mobile app notification's `data.image`
field. The companion app attaches its own token automatically to any image
URL that starts with `/`, the same way it already does for camera snapshots -
so a path under this integration's own `/api/...` namespace is fetchable
from a notification with no token of its own to manage, the same trick
Frigate's own integration uses for `/api/frigate/notifications/.../thumbnail.jpg`.

Reads the file directly off disk rather than proxying through the add-on:
`/media` is a folder the Supervisor shares between every add-on and Home
Assistant core itself, so the snapshot the add-on wrote is already sitting
right here - this only needs to find it, not fetch it from anywhere.
"""
from __future__ import annotations

import logging
import os

from aiohttp import web

from homeassistant.components.http import HomeAssistantView

_LOGGER = logging.getLogger(__name__)

SNAPSHOT_API_PATH = "/api/dreame_vacuum_unlocked_integration/snapshot/{tag}/{filename}"

SNAPSHOT_ROOT = "/media/dreame_vacuum_unlocked/snapshots"


def _safe_component(value: str) -> str:
    """A single path segment, not a path - no slashes, no traversal.

    Matches the add-on's own tag-sanitising rule (see the companion repo's
    ui.py/app.py `_safe_tag`) so the same tag id resolves to the same folder
    on both sides - it does not have to be identical byte-for-byte, only
    equally strict, since this only ever reads a path it built itself.
    """
    cleaned = "".join(c if (c.isalnum() or c in "-_") else "_" for c in (value or "").strip())
    return cleaned.strip("_")[:48].lower() or "general"


class DreameSnapshotView(HomeAssistantView):
    """GET /api/dreame_vacuum_unlocked_integration/snapshot/<tag>/<filename>"""

    url = SNAPSHOT_API_PATH
    name = "api:dreame_vacuum_unlocked_integration:snapshot"
    requires_auth = True

    async def get(self, request: web.Request, tag: str, filename: str) -> web.Response:
        safe_tag = _safe_component(tag)
        # basename, not the tag sanitiser: a filename is a timestamp plus
        # ".jpg", never something that needs character-filtering, and
        # basename is what actually stops "../.." rather than merely
        # tolerating it.
        safe_file = os.path.basename(filename)
        if not safe_file.lower().endswith(".jpg"):
            return self.json_message("Not a snapshot filename", 400)

        path = os.path.join(SNAPSHOT_ROOT, safe_tag, safe_file)
        if not os.path.isfile(path):
            return self.json_message("No such snapshot", 404)

        try:
            with open(path, "rb") as handle:
                data = handle.read()
        except OSError as err:
            _LOGGER.warning("Could not read snapshot %s: %s", path, err)
            return self.json_message("Could not read that snapshot", 502)

        return web.Response(body=data, content_type="image/jpeg")
