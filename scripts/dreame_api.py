"""Minimal authenticated client for Dreame's product/plugin APIs.

Dev-time only - used by the profile-generation scripts, never shipped to or
run on a user's Home Assistant.

Authentication is delegated to the `dreame_lib` package vendored in the
companion add-on repo rather than reimplemented here: the login flow and
request signing are fiddly and having two copies drift apart is a much worse
failure mode than a path dependency on a sibling checkout.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

DEFAULT_LIB_PATHS = [
    # sibling checkout of the add-on repo
    Path(__file__).resolve().parents[2] / "dreame-vacuum-companion" / "dreame_vacuum_companion",
    # ...or an explicit override
]


def _load_protocol():
    candidates = []
    env = os.environ.get("DREAME_LIB_PATH")
    if env:
        candidates.append(Path(env))
    candidates.extend(DEFAULT_LIB_PATHS)

    for path in candidates:
        if (path / "dreame_lib" / "protocol.py").exists():
            sys.path.insert(0, str(path))
            from dreame_lib.protocol import DreameVacuumProtocol  # noqa: E402

            return DreameVacuumProtocol

    raise SystemExit(
        "Could not locate dreame_lib.\n"
        "Checked:\n  " + "\n  ".join(str(c) for c in candidates) + "\n\n"
        "Set DREAME_LIB_PATH to the directory containing dreame_lib/, e.g.:\n"
        "  export DREAME_LIB_PATH=../dreame-vacuum-companion/dreame_vacuum_companion"
    )


class DreameApi:
    """Thin wrapper exposing the GET endpoints the plugin APIs use.

    dreame_lib only speaks POST (that's all the device API needs), but the
    plugin-discovery endpoints are GETs with query params, so we borrow its
    authenticated header set and issue the request ourselves.
    """

    def __init__(self, username: str, password: str, country: str = "eu") -> None:
        protocol_cls = _load_protocol()
        self._protocol = protocol_cls(
            username=username,
            password=password,
            country=country,
            prefer_cloud=True,
            account_type="dreame",
        )
        if not self._protocol.cloud.login():
            raise SystemExit("Dreame login failed - check username/password/country")
        self._cloud = self._protocol.cloud

    @property
    def base_url(self) -> str:
        return self._cloud.get_api_url()

    def _headers(self) -> dict:
        c = self._cloud
        s = c._strings
        return {
            "Accept": "*/*",
            "Accept-Language": "en-US;q=0.8",
            "Accept-Encoding": "gzip, deflate",
            s[47]: s[3],
            s[49]: s[5],
            s[50]: c._ti if c._ti else s[6],
            s[51]: s[52],
            s[46]: c._key,
        }

    def get(self, path: str, params: dict | None = None, timeout: int = 20):
        import requests

        resp = requests.get(
            f"{self.base_url}/{path.lstrip('/')}",
            headers=self._headers(),
            params=params,
            timeout=timeout,
        )
        resp.raise_for_status()
        return resp.json()

    def get_app_plugin(self, model: str, app_ver: int = 160, os_id: int = 1) -> dict | None:
        """Plugin bundle info for a model.

        os_id: 0 = iOS, 1 = Android. Only affects the *common* bundle URL -
        the model resource package (which carries the capability manifest) is
        identical for both.

        Returns None when the backend has nothing for this model, which is
        common for older hardware and is not an error.
        """
        data = self.get(
            "dreame-product/upgrades/appplugin",
            {"model": model, "appVer": app_ver, "os": os_id},
        )
        payload = data.get("data")
        if not payload:
            return None
        return payload

    def get_devices(self) -> list[dict]:
        """Devices on the logged-in account - handy for seeding a model list."""
        devices = self._cloud.get_devices() or {}
        return list(devices.get("page", {}).get("records", []))
