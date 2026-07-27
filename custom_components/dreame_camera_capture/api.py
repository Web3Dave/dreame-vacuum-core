"""Thin async HTTP client for the Dreame Vacuum Camera Capture add-on.

This integration holds no copy of the reverse-engineered Dreame signing/XP2P
pipeline itself - that all lives in the add-on. Every call here just forwards
credentials and a target device id to the add-on's API and returns its
response.
"""
from __future__ import annotations

import aiohttp


class DreameCaptureApiError(Exception):
    """Raised for any add-on communication failure."""


class DreameCaptureAuthError(DreameCaptureApiError):
    """Raised when the add-on rejects our api_token."""


class DreameCaptureClient:
    def __init__(self, session: aiohttp.ClientSession, host: str, port: int, api_token: str) -> None:
        self._session = session
        self._base = f"http://{host}:{port}"
        self._headers = {"X-Api-Token": api_token}

    async def _post(self, path: str, body: dict, timeout: int = 30) -> dict:
        try:
            async with self._session.post(
                f"{self._base}{path}", json=body, headers=self._headers,
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as resp:
                if resp.status == 401:
                    raise DreameCaptureAuthError("add-on rejected the api_token")
                data = await resp.json()
                if resp.status >= 400:
                    raise DreameCaptureApiError(str(data))
                return data
        except aiohttp.ClientError as err:
            raise DreameCaptureApiError(str(err)) from err

    async def _get(self, path: str, params: dict | None = None, timeout: int = 10) -> dict:
        try:
            async with self._session.get(
                f"{self._base}{path}", params=params, headers=self._headers,
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as resp:
                if resp.status == 401:
                    raise DreameCaptureAuthError("add-on rejected the api_token")
                if resp.status >= 400:
                    raise DreameCaptureApiError(f"HTTP {resp.status}")
                return await resp.json()
        except aiohttp.ClientError as err:
            raise DreameCaptureApiError(str(err)) from err

    async def list_devices(self, username: str, password: str, country: str) -> list[dict]:
        data = await self._post("/devices", {"username": username, "password": password, "country": country})
        return data["devices"]

    async def capture(self, username: str, password: str, country: str, four_digit_code: str, did: str) -> str:
        data = await self._post("/capture", {
            "username": username, "password": password, "country": country,
            "four_digit_code": four_digit_code, "did": did,
        })
        return data["path"]

    async def stream_start(self, username: str, password: str, country: str, four_digit_code: str, did: str) -> str:
        data = await self._post("/stream/start", {
            "username": username, "password": password, "country": country,
            "four_digit_code": four_digit_code, "did": did,
        })
        return data["rtsp_url"]

    async def stream_stop(self, did: str) -> None:
        await self._post("/stream/stop", {"did": did})

    async def stream_status(self, did: str) -> bool:
        data = await self._get("/stream/status", {"did": did})
        return bool(data.get("running"))

    async def get_latest_jpg(self, did: str) -> bytes | None:
        try:
            async with self._session.get(
                f"{self._base}/latest.jpg", params={"did": did}, headers=self._headers,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    return None
                return await resp.read()
        except aiohttp.ClientError as err:
            raise DreameCaptureApiError(str(err)) from err
