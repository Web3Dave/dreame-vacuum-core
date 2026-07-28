"""Client for the dreame_vacuum_companion add-on.

The add-on owns everything camera/streaming, because Tencent's XP2P libraries
are x86_64-only. Keeping that out of process is what allows this integration
to remain pure Python and work on ARM hardware.

Every method degrades to None/False rather than raising, and camera support is
optional. A vacuum should never go unavailable because the camera add-on is
restarting.
"""
from __future__ import annotations

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
        except (aiohttp.ClientError, TimeoutError) as err:
            if not self._warned:
                _LOGGER.warning("Companion add-on unreachable at %s: %s", self._base, err)
                self._warned = True
            return None

    async def async_health(self) -> bool:
        try:
            async with self._session.get(
                f"{self._base}/health", timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                return resp.status == 200
        except (aiohttp.ClientError, TimeoutError):
            return False

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

    async def async_capture(
        self, username: str, password: str, country: str, pin: str, did: str
    ) -> str | None:
        result = await self._post(
            "/capture",
            {
                "username": username,
                "password": password,
                "country": country,
                "four_digit_code": pin,
                "did": did,
            },
        )
        return (result or {}).get("path")

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
