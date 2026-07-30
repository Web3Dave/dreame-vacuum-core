/**
 * Shared map rendering for the add-on's picker and any Lovelace card.
 *
 * Served by the integration so there is one copy: the vertical flip used to
 * live in Python (render) and JavaScript (hit test) as two implementations,
 * and they disagreed - clicks landed mirrored about the middle of the map.
 * Here `worldToPixel` and `pixelToWorld` are the only things that know about
 * orientation, and everything draws and hit-tests through them.
 */

export const VERSION = 1;

const WALL = 63;
const AREA_KIND = 3;          // an area inside a room, carpet on the maps seen
export const ROOM_COLOURS = [
  "#6fb3d9", "#8fd19e", "#f2c46b", "#e08c8c",
  "#b99dd6", "#7fd1c9", "#d9a377", "#9fb8e0",
];
const WALL_COLOUR = "#373d45";

/** Turn the published document into something drawable. */
export function decodeMap(doc) {
  if (!doc || !doc.grid) throw new Error("Map document has no grid");
  if (doc.version > VERSION) {
    throw new Error(`Map document is version ${doc.version}, this renderer speaks ${VERSION}`);
  }
  const binary = atob(doc.grid);
  const grid = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) grid[i] = binary.charCodeAt(i);
  const [cols, rows] = doc.cells;
  if (grid.length !== cols * rows) {
    throw new Error(`Grid is ${grid.length} bytes, expected ${cols * rows}`);
  }
  return { ...doc, grid, cols, rows };
}

/**
 * Millimetres to canvas pixels.
 *
 * Rows are inverted: the map's y axis increases upward while canvas rows
 * increase downward, so without this the flat renders upside down.
 */
export function worldToPixel(map, x, y, scale = 1) {
  const [ox, oy] = map.origin;
  return {
    x: ((x - ox) / map.grid_size) * scale,
    // `rows -` rather than `rows - 1 -`: the canvas y of the map's lowest
    // world y is the bottom edge of the image, not the top of its last row.
    y: (map.rows - (y - oy) / map.grid_size) * scale,
  };
}

/**
 * Canvas pixels back to millimetres. The exact inverse of `worldToPixel` -
 * an earlier version snapped to cell centres in one direction only, which
 * left the two half a cell apart.
 */
export function pixelToWorld(map, px, py, scale = 1) {
  const [ox, oy] = map.origin;
  return {
    x: Math.round(ox + (px / scale) * map.grid_size),
    y: Math.round(oy + (map.rows - py / scale) * map.grid_size),
  };
}

/** The raw grid byte at a world coordinate, or 0 outside the map. */
export function cellAt(map, x, y) {
  const [ox, oy] = map.origin;
  const col = Math.floor((x - ox) / map.grid_size);
  const row = Math.floor((y - oy) / map.grid_size);
  if (col < 0 || col >= map.cols || row < 0 || row >= map.rows) return 0;
  return map.grid[row * map.cols + col];
}

/**
 * What is at a point: which room, and whether the vacuum could stand there.
 *
 * The picker uses this to refuse a click on a wall or off the map - selecting
 * one produces a task that drives about and gives up, with nothing to say why.
 */
export function describePoint(map, x, y) {
  const value = cellAt(map, x, y);
  if (!value) return { ok: false, reason: "outside the mapped area", room: null };
  const room = value >> 2;
  if (room === WALL) return { ok: false, reason: "a wall", room: null };
  return { ok: true, room, name: (map.room_names || {})[room] || `Room ${room}` };
}

export function roomColour(room) {
  return ROOM_COLOURS[(room - 1) % ROOM_COLOURS.length];
}

/** Rooms and walls. Everything else draws on top of this. */
export function drawBase(ctx, map, { scale = 1, showRoomNames = false } = {}) {
  ctx.save();
  ctx.imageSmoothingEnabled = false;
  const { cols, rows, grid } = map;
  for (let row = 0; row < rows; row++) {
    for (let col = 0; col < cols; col++) {
      const value = grid[row * cols + col];
      if (!value) continue;
      const room = value >> 2;
      let colour;
      if (room === WALL) {
        colour = WALL_COLOUR;
      } else {
        colour = roomColour(room);
        if ((value & 3) === AREA_KIND) colour = shade(colour, 0.78);
      }
      ctx.fillStyle = colour;
      // Flip the row here too, through the same rule as worldToPixel.
      ctx.fillRect(col * scale, (rows - 1 - row) * scale, scale, scale);
    }
  }
  if (showRoomNames) drawRoomNames(ctx, map, scale);
  ctx.restore();
}

/** Room labels at each room's centre of mass. */
function drawRoomNames(ctx, map, scale) {
  const sums = new Map();
  const { cols, rows, grid } = map;
  for (let row = 0; row < rows; row++) {
    for (let col = 0; col < cols; col++) {
      const value = grid[row * cols + col];
      const room = value >> 2;
      if (!value || room === WALL) continue;
      const acc = sums.get(room) || { x: 0, y: 0, n: 0 };
      acc.x += col; acc.y += row; acc.n += 1;
      sums.set(room, acc);
    }
  }
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  ctx.font = `${Math.max(11, scale * 2.4)}px system-ui, sans-serif`;
  for (const [room, acc] of sums) {
    const label = (map.room_names || {})[room] || `Room ${room}`;
    const x = (acc.x / acc.n) * scale;
    const y = (rows - 1 - acc.y / acc.n) * scale;
    ctx.lineWidth = 3;
    ctx.strokeStyle = "rgba(0,0,0,.55)";
    ctx.strokeText(label, x, y);
    ctx.fillStyle = "#fff";
    ctx.fillText(label, x, y);
  }
}

/**
 * The vacuum, with a cone showing where the camera is pointing.
 *
 * `heading` is degrees as the device reports them; the cone is drawn about it
 * so the direction reads at a glance rather than needing the number.
 */
export function drawVacuum(ctx, map, pose, { scale = 1, fov = 70, reach = 900,
                                             colour = "#ff5252" } = {}) {
  if (!pose || pose.x == null || pose.y == null) return;
  const { x, y } = worldToPixel(map, pose.x, pose.y, scale);
  const radius = Math.max(5, scale * 1.6);

  if (pose.heading != null && fov > 0) {
    const reachPx = (reach / map.grid_size) * scale;
    // Screen y grows downward while headings grow the other way, so negate.
    const centre = (-pose.heading * Math.PI) / 180;
    const half = (fov * Math.PI) / 360;
    const gradient = ctx.createRadialGradient(x, y, radius, x, y, reachPx);
    gradient.addColorStop(0, "rgba(255,255,255,.45)");
    gradient.addColorStop(1, "rgba(255,255,255,0)");
    ctx.beginPath();
    ctx.moveTo(x, y);
    ctx.arc(x, y, reachPx, centre - half, centre + half);
    ctx.closePath();
    ctx.fillStyle = gradient;
    ctx.fill();
  }

  ctx.beginPath();
  ctx.arc(x, y, radius, 0, Math.PI * 2);
  ctx.fillStyle = colour;
  ctx.fill();
  ctx.lineWidth = 2;
  ctx.strokeStyle = "#fff";
  ctx.stroke();
}

/** A labelled point - a task target, a picked coordinate. */
export function drawMarker(ctx, map, point, { scale = 1, colour = "#ff5252",
                                              label = null } = {}) {
  const { x, y } = worldToPixel(map, point.x, point.y, scale);
  const radius = Math.max(5, scale * 1.4);
  ctx.beginPath();
  ctx.arc(x, y, radius, 0, Math.PI * 2);
  ctx.fillStyle = colour;
  ctx.fill();
  ctx.lineWidth = 2;
  ctx.strokeStyle = "#fff";
  ctx.stroke();
  if (label) {
    ctx.font = `${Math.max(11, scale * 2.2)}px system-ui, sans-serif`;
    ctx.textAlign = "center";
    ctx.textBaseline = "bottom";
    ctx.lineWidth = 3;
    ctx.strokeStyle = "rgba(0,0,0,.55)";
    ctx.strokeText(label, x, y - radius - 3);
    ctx.fillStyle = "#fff";
    ctx.fillText(label, x, y - radius - 3);
  }
}

function shade(hex, factor) {
  const n = parseInt(hex.slice(1), 16);
  const r = Math.round(((n >> 16) & 255) * factor);
  const g = Math.round(((n >> 8) & 255) * factor);
  const b = Math.round((n & 255) * factor);
  return `rgb(${r},${g},${b})`;
}
