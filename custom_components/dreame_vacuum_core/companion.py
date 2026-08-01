"""Client for the dreame_vacuum_companion add-on.

The add-on owns everything camera/streaming, because Tencent's XP2P libraries
are x86_64-only. Keeping that out of process is what allows this integration
to remain pure Python and work on ARM hardware.

Every method degrades to None/False rather than raising, and camera support is
optional. A vacuum should never go unavailable because the camera add-on is
restarting.
"""
from __future__ import annotations

import json
import logging

import aiohttp

_LOGGER = logging.getLogger(__name__)


class CompanionClient:
    def __init__(self, session: aiohttp.ClientSession, host: str, port: int, token: str) -> None:
        self._session = session
        self._base = f"http://{host}:{port}"
        self._headers = {"X-Api-Token": token}
        self._warned = False

    async def _post(self, path: str, body: dict, timeout: int = 45) -> dict | None:
        # One retry on a dropped connection. HA's shared aiohttp session pools
        # connections, and a pooled one can be closed by the server before we
        # reuse it - which surfaces as ServerDisconnectedError before the
        # request is ever sent. Retrying is safe: /stream/start returns the
        # existing stream if one is running, and /stream/stop is idempotent.
        try:
            return await self._post_once(path, body, timeout)
        except aiohttp.ServerDisconnectedError:
            _LOGGER.debug("Connection to the add-on was dropped, retrying %s", path)
        try:
            return await self._post_once(path, body, timeout)
        except aiohttp.ServerDisconnectedError as err:
            if not self._warned:
                _LOGGER.warning("Companion add-on dropped the connection twice: %s", err)
                self._warned = True
            return None

    async def _post_once(self, path: str, body: dict, timeout: int) -> dict | None:
        try:
            async with self._session.post(
                f"{self._base}{path}",
                json=body,
                headers=self._headers,
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as resp:
                if resp.status == 401:
                    _LOGGER.error("Companion add-on rejected the API token")
                    return None
                if resp.status >= 400:
                    _LOGGER.debug("Companion %s -> HTTP %s", path, resp.status)
                    return None
                self._warned = False
                return await resp.json()
        except aiohttp.ServerDisconnectedError:
            # Handled by _post's retry. Caught explicitly because it subclasses
            # ClientError and would otherwise be swallowed below.
            raise
        except (aiohttp.ClientError, TimeoutError) as err:
            if not self._warned:
                _LOGGER.warning("Companion add-on unreachable at %s: %s", self._base, err)
                self._warned = True
            return None

    async def async_health(self) -> bool:
        """Reachability only. /health is deliberately unauthenticated, so this
        says nothing about whether the token is right - use async_check_auth
        for that."""
        try:
            async with self._session.get(
                f"{self._base}/health", timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                return resp.status == 200
        except (aiohttp.ClientError, TimeoutError):
            return False

    async def async_check_auth(self) -> bool | None:
        """Verify the API token against an authenticated endpoint.

        Returns True (token accepted), False (rejected) or None (add-on not
        reachable) so the caller can tell "wrong token" from "wrong host".
        """
        try:
            async with self._session.get(
                f"{self._base}/registered",
                headers=self._headers,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status == 401:
                    return False
                return resp.status == 200
        except (aiohttp.ClientError, TimeoutError):
            return None

    async def async_register(self, entry_id: str, devices: list[dict]) -> bool:
        """Tell the add-on which devices belong to this integration.

        The add-on cannot reliably derive this itself (the REST API exposes
        entity states but not owning integration), so we push it.
        """
        result = await self._post("/register", {"entry_id": entry_id, "devices": devices})
        return bool(result and result.get("success"))

    async def async_stream_start(
        self, username: str, password: str, country: str, pin: str, did: str
    ) -> str | None:
        """Returns an RTSP url. Blocks until the feed is actually publishing."""
        result = await self._post(
            "/stream/start",
            {
                "username": username,
                "password": password,
                "country": country,
                "four_digit_code": pin,
                "did": did,
            },
        )
        return (result or {}).get("rtsp_url")

    async def async_stream_stop(self, did: str) -> bool:
        result = await self._post("/stream/stop", {"did": did})
        return bool(result and result.get("success"))

    async def async_stream_status(self, did: str) -> bool | None:
        """True/False if known, None if the add-on couldn't be reached."""
        state = await self.async_stream_state(did)
        return None if state is None else bool(state.get("running"))

    async def async_stream_url(self, did: str) -> str | None:
        """The RTSP url of an already-running stream, or None.

        Read-only on purpose: it never starts a session.
        """
        state = await self.async_stream_state(did)
        return (state or {}).get("rtsp_url")

    async def async_stream_state(self, did: str) -> dict | None:
        try:
            async with self._session.get(
                f"{self._base}/stream/status",
                params={"did": did},
                headers=self._headers,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    # Worth a warning: the symptom the user sees is a Stream
                    # switch stuck on "unavailable" with nothing in the log.
                    _LOGGER.warning(
                        "Companion add-on returned HTTP %s for /stream/status", resp.status
                    )
                    return None
                self._warned = False
                return await resp.json()
        except (aiohttp.ClientError, TimeoutError) as err:
            if not self._warned:
                _LOGGER.warning("Companion add-on unreachable at %s: %s", self._base, err)
                self._warned = True
            return None

    async def async_task_calls(self, slug: str) -> dict | None:
        """A task expanded into service calls, or None if unreachable.

        Deliberately asks the add-on rather than expanding steps here: the
        add-on owns the step schema, and a second implementation of that
        expansion would drift from the one the export uses.
        """
        try:
            async with self._session.get(
                f"{self._base}/tasks/{slug}/calls",
                headers=self._headers,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status == 200:
                    return await resp.json()
                return {"error": (await resp.text())[:300], "status": resp.status}
        except (aiohttp.ClientError, TimeoutError) as err:
            _LOGGER.warning("Could not fetch task %s: %s", slug, err)
            return None

    async def async_list_tasks(self, did: str | None = None) -> list[dict]:
        try:
            async with self._session.get(
                f"{self._base}/tasks",
                params={"did": did} if did else None,
                headers=self._headers,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status != 200:
                    return []
                return (await resp.json()).get("tasks") or []
        except (aiohttp.ClientError, TimeoutError):
            return []

    async def async_publish_map(
        self, did: str, png: bytes, meta: dict, document: dict | None = None
    ) -> bool:
        """Upload a rendered map and its geometry.

        Multipart rather than JSON: base64 would inflate the image by a third
        for no benefit, and the add-on writes the bytes straight to disk.
        """
        # Retried like every other call: Home Assistant pools connections, and
        # a pooled one the add-on has already closed fails as a reset or
        # "cannot write request body" before the request is really sent. The
        # form is rebuilt each attempt because FormData cannot be replayed.
        last: Exception | None = None
        for attempt in (1, 2):
            form = aiohttp.FormData()
            form.add_field("did", did)
            form.add_field("meta", json.dumps(meta), content_type="application/json")
            if document is not None:
                form.add_field(
                    "document", json.dumps(document), content_type="application/json"
                )
            form.add_field("image", png, filename="map.png", content_type="image/png")
            try:
                async with self._session.post(
                    f"{self._base}/map",
                    data=form,
                    headers=self._headers,
                    timeout=aiohttp.ClientTimeout(total=60),
                ) as resp:
                    if resp.status != 200:
                        _LOGGER.warning(
                            "Add-on rejected the map: HTTP %s %s",
                            resp.status, (await resp.text())[:200],
                        )
                        return False
                    return True
            except (aiohttp.ClientError, TimeoutError) as err:
                last = err
                if attempt == 1:
                    _LOGGER.debug("Map upload failed, retrying: %s", err)
        _LOGGER.warning("Could not publish the map: %s", last)
        return False

    async def async_close_orphaned_runs(self, did: str) -> int:
        """Close runs the add-on still thinks are in progress.

        Called at startup: a Home Assistant restart ends any errand, but the
        add-on's own history has no way to know that and would show it running
        forever.
        """
        result = await self._post("/runs/reconcile", {"did": did}, timeout=10)
        return (result or {}).get("closed", 0)

    async def async_start_run(
        self, did: str, command: str, run_id: str | None = None
    ) -> int | None:
        """Open a run record; steps stream against the returned id.

        Best-effort throughout: losing the log must never affect the errand.
        """
        result = await self._post(
            "/runs", {"did": did, "command": command, "run_uid": run_id}, timeout=10
        )
        return (result or {}).get("id")

    async def async_run_step(self, run_id: int, text: str) -> None:
        await self._post(f"/runs/{run_id}/steps", {"text": text}, timeout=10)

    async def async_finish_run(
        self, run_id: int, ok: bool, summary: str, detail: dict
    ) -> None:
        await self._post(
            f"/runs/{run_id}/finish",
            {"ok": ok, "summary": summary, "detail": detail},
            timeout=10,
        )

    async def async_capture(
        self, username: str, password: str, country: str, pin: str, did: str,
        tag: str | None = None,
    ) -> dict | None:
        """Returns the add-on's paths for the new photo, or None."""
        return await self._post(
            "/capture",
            {
                "username": username,
                "password": password,
                "country": country,
                "four_digit_code": pin,
                "did": did,
                "tag": tag or "general",
            },
        )

    async def async_latest_image(self, did: str) -> bytes | None:
        try:
            async with self._session.get(
                f"{self._base}/latest.jpg",
                params={"did": did},
                headers=self._headers,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status != 200:
                    return None
                return await resp.read()
        except (aiohttp.ClientError, TimeoutError):
            return None
