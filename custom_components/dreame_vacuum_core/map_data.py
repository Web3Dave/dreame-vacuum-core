"""Just enough map decoding to know where the robot is.

Rendering the map is a large job - the upstream fork spends ~14k lines on it,
pulling in numpy, Pillow and an embedded JavaScript engine. Position is not:
every map frame starts with a 27-byte header that carries the robot and dock
coordinates at fixed offsets, so reading it needs nothing beyond base64, zlib
and (on models that encrypt) AES-CBC.

Frame layout, little-endian signed int16 unless noted:

    0   map_id
    2   frame_id
    4   frame_type      int8 - ord('I') full frame, ord('P') partial
    5   robot x         mm
    7   robot y         mm
    9   robot angle     degrees
    11  charger x
    13  charger y
    15  charger angle
    17  grid size       mm per pixel
    19  width           pixels
    21  height          pixels

The image and a JSON trailer follow; neither is needed here.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import zlib

_LOGGER = logging.getLogger(__name__)

HEADER_SIZE = 27

# The angle the firmware sends when it has no fix - the robot is somewhere it
# can't localise, typically mid-relocation. Treated as "unknown", not as 32767
# degrees.
ANGLE_UNKNOWN = 32767


def _int16(data: bytes, offset: int) -> int:
    return int.from_bytes(data[offset : offset + 2], byteorder="little", signed=True)


def _decompress(raw: str, iv: str | None) -> bytes | None:
    """base64 -> optional AES-CBC -> zlib."""
    payload = raw.replace("_", "/").replace("-", "+")
    key: str | None = None
    if "," in payload:
        # Encrypting models append the per-frame key to the payload.
        payload, key = payload.split(",", 1)

    try:
        data = base64.decodebytes(payload.encode("utf8"))
    except Exception as err:  # noqa: BLE001 - malformed frames are not fatal
        _LOGGER.debug("Map frame is not valid base64: %s", err)
        return None

    if key:
        if not iv:
            # Without the model's IV the frame can't be read. Not an error we
            # can act on, and it would otherwise log on every single frame.
            _LOGGER.debug("Map frame is encrypted but no AES_IV is known for this model")
            return None
        try:
            from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

            cipher = Cipher(
                algorithms.AES(hashlib.sha256(key.encode()).hexdigest()[0:32].encode("utf8")),
                modes.CBC(iv.encode("utf8")),
            )
            decryptor = cipher.decryptor()
            data = decryptor.update(data) + decryptor.finalize()
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Map frame decryption failed: %s", err)
            return None

    try:
        return zlib.decompress(data)
    except zlib.error as err:
        _LOGGER.debug("Map frame decompression failed: %s", err)
        return None


def decode_position(raw: str, iv: str | None = None) -> dict | None:
    """Read the robot's pose from a raw map property value.

    Returns None when the frame can't be read; a dict with x/y/angle set to
    None when the frame is readable but the robot has no fix.
    """
    if not raw or len(raw) < 3:
        return None

    data = _decompress(raw, iv)
    if data is None or len(data) < HEADER_SIZE:
        return None

    angle = _int16(data, 9)
    located = angle != ANGLE_UNKNOWN

    # The trailer's "nr" flag also means no robot position - it appears when
    # the robot is off the map entirely rather than merely unlocalised.
    width, height = _int16(data, 19), _int16(data, 21)
    trailer_at = HEADER_SIZE + max(width, 0) * max(height, 0)
    if located and len(data) > trailer_at:
        try:
            if json.loads(data[trailer_at:].decode("utf8")).get("nr"):
                located = False
        except Exception:  # noqa: BLE001 - trailer is optional and often absent
            pass

    # The dock uses the same sentinel as the angle when its position is
    # unknown - which it is whenever the robot hasn't seen the dock on this
    # map. Reporting 32767 as a coordinate would put it 32 metres away.
    charger_x, charger_y = _int16(data, 11), _int16(data, 13)
    docked_known = charger_x != ANGLE_UNKNOWN and charger_y != ANGLE_UNKNOWN

    return {
        "map_id": _int16(data, 0),
        "frame_id": _int16(data, 2),
        "frame_type": chr(data[4]) if 32 <= data[4] < 127 else data[4],
        "x": _int16(data, 5) if located else None,
        "y": _int16(data, 7) if located else None,
        "angle": angle if located else None,
        "charger_x": charger_x if docked_known else None,
        "charger_y": charger_y if docked_known else None,
        "grid_size": _int16(data, 17),
    }
