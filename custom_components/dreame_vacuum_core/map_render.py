"""Render a map frame to a PNG, flat and legible.

Deliberately not the upstream fork's renderer, which spends thousands of lines
on paths, carpets, furniture, obstacles and no-go zones. This draws rooms and
walls only, because its one job is to be something a person can point at to
pick a coordinate.

Rendered here rather than in the add-on for two reasons: the integration is
what can fetch a frame, and Home Assistant already ships Pillow.
"""
from __future__ import annotations

import base64
import io
import logging

_LOGGER = logging.getLogger(__name__)

# Distinct rather than pretty: the point is telling one room from the next.
ROOM_COLOURS = [
    (0x6F, 0xB3, 0xD9), (0x8F, 0xD1, 0x9E), (0xF2, 0xC4, 0x6B),
    (0xE0, 0x8C, 0x8C), (0xB9, 0x9D, 0xD6), (0x7F, 0xD1, 0xC9),
    (0xD9, 0xA3, 0x77), (0x9F, 0xB8, 0xE0),
]
WALL_COLOUR = (0x37, 0x3D, 0x45)
WALL_SEGMENT = 63

# Areas inside a room - carpet on the map this was derived from. Shaded rather
# than drawn as wall: at wall colour they read as furniture and made the map
# look full of obstacles that are not there.
AREA_SHADE = 0.78


def render_png(frame: dict, scale: int = 5) -> bytes | None:
    """A PNG of the map, `scale` screen pixels per map cell.

    Nearest-neighbour on purpose: a smoothed occupancy grid looks like a
    photograph of a map rather than a grid, and blurs the cell boundaries the
    coordinate maths depends on.

    Rows are drawn bottom-up: the map's y axis increases upward, image rows
    increase downward, so drawing them in order renders the flat upside down.
    `point_to_world` inverts the same way, and the two must be changed
    together or a click will select the wrong place.
    """
    try:
        from PIL import Image
    except ImportError:  # pragma: no cover - Home Assistant ships Pillow
        _LOGGER.error("Pillow is not available, so the map cannot be rendered")
        return None

    width, height, grid = frame["width"], frame["height"], frame["grid"]
    image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    put = image.load()
    for index, value in enumerate(grid):
        if not value:
            continue  # outside the map
        segment, kind = value >> 2, value & 3
        if segment == WALL_SEGMENT:
            colour = WALL_COLOUR
        else:
            colour = ROOM_COLOURS[(segment - 1) % len(ROOM_COLOURS)]
            if kind == 3:
                colour = tuple(int(channel * AREA_SHADE) for channel in colour)
        put[index % width, height - 1 - index // width] = (*colour, 255)

    if scale > 1:
        image = image.resize((width * scale, height * scale), Image.NEAREST)

    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def metadata(frame: dict, scale: int = 5) -> dict:
    """What a caller needs to turn a click back into a coordinate.

    Kept next to the image because the two are only meaningful together: the
    same pixel means a different place on a map with a different origin.
    """
    return {
        "map_id": frame.get("map_id"),
        "grid_size": frame["grid_size"],
        "origin": frame["origin"],
        "cells": [frame["width"], frame["height"]],
        "size": [frame["width"] * scale, frame["height"] * scale],
        "scale": scale,
        "robot": frame.get("robot"),
        "angle": frame.get("angle"),
        "rooms": sorted({v >> 2 for v in frame["grid"] if v and (v >> 2) != WALL_SEGMENT}),
    }


MAP_DOCUMENT_VERSION = 1


def map_document(frame: dict, scale: int = 5) -> dict:
    """The map as data, for a client that renders it itself.

    Sent instead of only a picture so the viewer decides what to draw: room
    names on or off, a room highlighted, the robot and its field of view drawn
    over the top - none of which should cost a server round trip, because the
    robot moves far more often than the map changes.

    `version` is here from the start: once a dashboard card reads this, its
    shape is an interface.
    """
    return {
        "version": MAP_DOCUMENT_VERSION,
        "map_id": frame.get("map_id"),
        "grid_size": frame["grid_size"],
        "origin": frame["origin"],
        "cells": [frame["width"], frame["height"]],
        "rooms": sorted({v >> 2 for v in frame["grid"] if v and (v >> 2) != WALL_SEGMENT}),
        "grid": base64.b64encode(frame["grid"]).decode("ascii"),
        "robot": frame.get("robot"),
        "angle": frame.get("angle"),
        # Optional, and older documents simply lack them - the renderer skips
        # the dock when it has no position, so this stays version 1.
        "dock": frame.get("charger"),
        "dock_angle": frame.get("charger_angle"),
        "suggested_scale": scale,
    }


def point_to_world(frame: dict, px: float, py: float, scale: int = 5) -> tuple[int, int]:
    """A pixel on the rendered image to millimetres on the map.

    The inverse of the render, and the reason the origin is published with the
    image. Cell centres, so a click lands in the middle of the cell it hit
    rather than on its corner.
    """
    grid_size = frame["grid_size"]
    origin_x, origin_y = frame["origin"]
    col = px / scale
    # Undo the vertical flip the render applies.
    row = (frame["height"] - 1) - py / scale
    return (
        int(origin_x + (col + 0.5) * grid_size),
        int(origin_y + (row + 0.5) * grid_size),
    )
