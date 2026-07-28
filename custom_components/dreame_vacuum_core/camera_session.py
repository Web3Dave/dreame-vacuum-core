"""A camera session with no video attached.

Driving the robot normally makes the firmware start a remote-control cleaning
task - work mode 13, brushes and mop running. With a live view open it stays
in standby and simply moves. Measured, not assumed: opening a session is the
only thing out of 242 properties that changes, and the brushes stop.

None of the video machinery is needed for that. This sends the same four
actions the app's video page sends and then stops, so no stream is decoded,
no P2P client runs, and the companion add-on is not involved - which means it
works on ARM hardware where the add-on cannot run.

The device drops the session without a keep-alive, so anything holding one
open for more than ~20s has to refresh it.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid
from typing import Any

from .transport.signing import sign_params

_LOGGER = logging.getLogger(__name__)

SIID_CAMERA = 10001
AIID_CAMERA_OPERATE = 1
AIID_STREAM_CODE = 4
PIID_MONITOR = 1
PIID_KEEP_ALIVE = 6
PIID_STREAM_CODE_OPEN = 1100
PIID_VERIFY_ACCESS_CODE = 1102

KEEP_ALIVE_SECONDS = 15


class CameraSession:
    """Open/close a monitor session. Blocking - call from the executor."""

    def __init__(self, protocol, did: str, pin: str) -> None:
        self._protocol = protocol
        self._did = did
        self._pin = pin
        self.session: str | None = None
        self._last_keep_alive = 0.0

    # -- plumbing ---------------------------------------------------------
    def _url(self) -> str:
        cloud = self._protocol.cloud
        strings = cloud._strings
        host = f"-{cloud._host.split('.')[0]}" if cloud._host else ""
        return f"{strings[37]}{host}/{strings[27]}/{strings[38]}"

    def _action(self, aiid: int, piid: int, value: dict) -> Any:
        """Camera actions go over the signed command API, not plain MIoT."""
        req_id = int(time.time() * 1000) % 1000000
        body = {
            "did": self._did, "id": req_id,
            "data": {
                "did": self._did, "id": req_id, "method": "action",
                "params": {
                    "did": self._did, "siid": SIID_CAMERA, "aiid": aiid,
                    "in": [{"piid": piid, "value": json.dumps(value, separators=(",", ":"))}],
                },
            },
        }
        signed, _ = sign_params(body)
        return self._protocol.cloud._api_call(self._url(), signed)

    def _identity(self) -> tuple[str, str]:
        """The XP2P product id / device name the camera is addressed by."""
        signed, _ = sign_params({"did": self._did, "os": "ios"})
        resp = self._protocol.cloud._api_call(
            "dreame-third-video/tx/mgr/dev/getIdentity", signed
        )
        if not resp or not resp.get("success"):
            raise RuntimeError(f"getIdentity failed: {resp}")
        data = resp["data"]["data"]
        return data["productId"], data["deviceName"]

    # -- lifecycle --------------------------------------------------------
    def start(self) -> bool:
        session = uuid.uuid4().hex
        product_id, device_name = self._identity()

        self._action(AIID_STREAM_CODE, PIID_STREAM_CODE_OPEN,
                     {"open": True, "session": session})
        self._action(AIID_STREAM_CODE, PIID_VERIFY_ACCESS_CODE,
                     {"oldcode": hashlib.sha256(self._pin.encode()).hexdigest(),
                      "lazymode": 0, "session": session})
        # token and channelId are what make this a real session - without them
        # the device accepts the call but enters no different state.
        result = self._action(AIID_CAMERA_OPERATE, PIID_MONITOR,
                              {"token": "tx",
                               "channelId": f"{product_id}/{device_name}",
                               "operType": "monitor", "operation": "start",
                               "session": session})
        code = ((result or {}).get("data") or {}).get("result", {}).get("code")
        if code != 0:
            _LOGGER.debug("Camera session refused: %s", result)
            return False

        self.session = session
        self._last_keep_alive = time.monotonic()
        return True

    def keep_alive(self, force: bool = False) -> None:
        """Refresh the session. Cheap to call often - it rate-limits itself."""
        if not self.session:
            return
        if not force and time.monotonic() - self._last_keep_alive < KEEP_ALIVE_SECONDS:
            return
        self._action(AIID_CAMERA_OPERATE, PIID_KEEP_ALIVE,
                     {"operType": "keep_alive", "videoStatus": "opened",
                      "session": self.session})
        self._last_keep_alive = time.monotonic()

    def stop(self) -> None:
        if not self.session:
            return
        try:
            self._action(AIID_CAMERA_OPERATE, PIID_MONITOR,
                         {"operType": "monitor", "operation": "end",
                          "session": self.session})
        except Exception as err:  # noqa: BLE001 - never mask the caller's outcome
            _LOGGER.debug("Closing the camera session failed: %s", err)
        finally:
            self.session = None
