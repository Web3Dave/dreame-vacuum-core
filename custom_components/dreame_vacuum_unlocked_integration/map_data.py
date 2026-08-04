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

# Room-type code -> standard English name, mirrored from the phone app's
# `areaTypesLDS` table. A seg_inf entry with `type != 0` is a room the
# firmware auto-classified; the app displays these standard names (while
# `type == 0` rooms carry a user-renamed base64 `name` instead). Unknown /
# non-room types are omitted so the renderer falls back to "Room N".
AREA_TYPE_NAMES: dict[int, str] = {
    1: "Living Room",
    2: "Bedroom",
    3: "Study",
    4: "Kitchen",
    5: "Dining Room",
    6: "Bathroom",
    7: "Balcony",
    8: "Hallway",
    9: "Storage Room",
    10: "Closet",
    11: "Drawing Room",
    12: "Office",
    13: "Fitness Area",
    14: "Leisure Area",
    15: "Bedroom 2",
}


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


def decode_frame(raw: str, iv: str | None = None) -> dict | None:
    """The whole frame: header, occupancy grid and JSON trailer.

    The grid is one byte per cell, encoding `segment = value >> 2` and
    `kind = value & 3`. Segment 63 is wall; segment 0 is outside the map;
    anything else is a room, and kind 3 marks an area within one (carpet, on
    the map this was derived from). Verified against a real map: the robot's
    reported position lands on floor of the room it was standing in.

    `origin` is the top-left corner in millimetres, so a cell maps to the world
    as `x = origin_x + col * grid_size` - and back the other way, which is what
    makes a point on a rendered map selectable.
    """
    if not raw or len(raw) < 3:
        return None
    data = _decompress(raw, iv)
    if data is None or len(data) < HEADER_SIZE:
        return None

    width, height = _int16(data, 19), _int16(data, 21)
    if width <= 0 or height <= 0:
        return None
    expected = HEADER_SIZE + width * height
    if len(data) < expected:
        _LOGGER.debug("Map frame is short: %d bytes, expected %d", len(data), expected)
        return None

    trailer: dict = {}
    if len(data) > expected:
        try:
            trailer = json.loads(data[expected:].decode("utf8")) or {}
        except Exception:  # noqa: BLE001 - the grid is still usable without it
            trailer = {}

    origin = trailer.get("origin")
    if not (isinstance(origin, list) and len(origin) == 2):
        # Also in the header, and the two agreed on every frame checked.
        origin = [_int16(data, 23), _int16(data, 25)]

    angle = _int16(data, 9)
    # Same sentinel as the angle: the dock's position is unknown until the
    # robot has seen it on this map, and 32767 would place it 32 metres out.
    charger_x, charger_y = _int16(data, 11), _int16(data, 13)
    docked_known = charger_x != ANGLE_UNKNOWN and charger_y != ANGLE_UNKNOWN
    return {
        "map_id": _int16(data, 0),
        "frame_id": _int16(data, 2),
        "grid_size": _int16(data, 17),
        "width": width,
        "height": height,
        "origin": [int(origin[0]), int(origin[1])],
        "robot": None if angle == ANGLE_UNKNOWN else [_int16(data, 5), _int16(data, 7)],
        "angle": None if angle == ANGLE_UNKNOWN else angle,
        "charger": [charger_x, charger_y] if docked_known else None,
        "charger_angle": _int16(data, 15) if docked_known else None,
        "grid": data[HEADER_SIZE:expected],
        "trailer": trailer,
    }


def _room_names_from_seg_inf(seg_inf) -> dict[int, str]:
    """Segment id -> name from a `seg_inf` dict.

    `seg_inf` is keyed by the same segment ids the grid encodes as
    `value >> 2`. Two kinds of room:

    * `type: 0` is a user-named (custom) room - its `name` is plain base64 of
      the UTF-8 text.
    * `type != 0` is a room the firmware classified - the phone app maps that
      type code to a standard name via its `areaTypesLDS` table (kitchen,
      bathroom, wardrobe, ...). We mirror those English labels so typed rooms
      get a real name instead of the renderer's "Room N" fallback.

    Structural entries (doorways, connectors) are not rooms and are skipped.
    """
    if not isinstance(seg_inf, dict):
        return {}
    names: dict[int, str] = {}
    for area_id, item in seg_inf.items():
        if not isinstance(item, dict):
            continue
        rtype = item.get("type")
        if rtype is None:
            # No type field at all - not a nameable room, skip.
            continue
        if rtype == 0:
            name_b64 = item.get("name")
            if not name_b64:
                continue
            try:
                name = base64.b64decode(name_b64).decode("utf8")
            except Exception:  # noqa: BLE001 - one bad name must not lose the rest
                continue
        else:
            name = AREA_TYPE_NAMES.get(int(rtype))
            if not name:
                continue  # unknown/infra type - let the renderer fall back
        names[int(area_id)] = name
    return names


def decode_room_names(trailer: dict) -> dict[int, str]:
    """Segment id -> room name, from a frame's trailer.

    Two sources, same format:

    * Backup maps (what `thb` decompresses to) carry `seg_inf` directly in
      the trailer, each room entry with a base64 `name`. Verified live: names
      like "Dinner Table", "Hall", "Cat Zone" decode exactly.
    * Live frames put it inside `rism` - a *second*, separately-compressed
      frame with the identical header/grid/trailer shape as the outer one -
      present when the trailer's `ris` is 1 or 2. Reverse-engineered from the
      phone app's own bundled JS (`decodeSaveMapData` in the vacuum's React
      Native plugin), which decodes it exactly this way: base64 (url-safe,
      `_`/`-` swapped back) then zlib, no AES step. Its inner trailer carries
      the same `seg_inf`.
    """
    if isinstance(trailer.get("seg_inf"), dict):
        return _room_names_from_seg_inf(trailer["seg_inf"])

    ris = trailer.get("ris")
    rism = trailer.get("rism")
    if ris not in (1, 2) or not rism:
        return {}

    try:
        payload = rism.replace("_", "/").replace("-", "+")
        data = zlib.decompress(base64.b64decode(payload + "=="))
    except Exception as err:  # noqa: BLE001 - a bad rism blob is not fatal
        _LOGGER.debug("Could not decompress rism: %s", err)
        return {}

    if len(data) < HEADER_SIZE:
        return {}
    width, height = _int16(data, 19), _int16(data, 21)
    if width <= 0 or height <= 0:
        return {}
    expected = HEADER_SIZE + width * height
    if len(data) < expected:
        return {}

    try:
        inner_trailer = json.loads(data[expected:].decode("utf8")) or {}
    except Exception:  # noqa: BLE001
        return {}

    return _room_names_from_seg_inf(inner_trailer.get("seg_inf"))


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
