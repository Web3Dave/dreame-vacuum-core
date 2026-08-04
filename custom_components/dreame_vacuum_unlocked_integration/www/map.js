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

/**
 * The vacuum's real footprint, in millimetres.
 *
 * Taken from the phone app, which computes its on-screen size as
 * `320 * screenWidth / realWidth` rather than picking an icon size - so the
 * shape on the map covers the floor the machine actually covers. That matters
 * for a picker: a point drawn clear of a wall at icon size can be a point the
 * vacuum physically cannot occupy.
 */
export const VACUUM_FOOTPRINT_MM = 320;
/** The dock is drawn at 1.2x the vacuum, again following the app. */
export const DOCK_SCALE = 1.2;
/** Below this the sprite is unrecognisable, so stop shrinking. The app agrees. */
const MIN_SPRITE_PX = 15;

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
  // JS % keeps the dividend's sign, so (0 - 1) % N is -1 and index -1 is
  // undefined. Backup maps can carry an "outside" room 0, so wrap to a real
  // colour instead of handing undefined to shade().
  const n = ((room - 1) % ROOM_COLOURS.length + ROOM_COLOURS.length) % ROOM_COLOURS.length;
  return ROOM_COLOURS[n];
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

/** Room labels, positioned at each room's bounding-box centre.
 *
 * Mirrors the phone app: its map decoder computes each area's
 * `minX/minY/maxX/maxY` (its axis-aligned bounding box in map cells) and
 * places the label at `(maxX-minX)/2+minX, (maxY-minY)/2+minY` - the box
 * centre, not the cells' centre of mass. An L or U-shaped room's centroid
 * can sit on a wall or outside the room, so the box centre reads far better.
 */
function drawRoomNames(ctx, map, scale) {
  const box = new Map(); // room -> {minCol,minRow,maxCol,maxRow}
  const { cols, rows, grid } = map;
  for (let row = 0; row < rows; row++) {
    for (let col = 0; col < cols; col++) {
      const value = grid[row * cols + col];
      const room = value >> 2;
      if (!value || room === WALL) continue;
      const b = box.get(room) || { minCol: cols, minRow: rows, maxCol: -1, maxRow: -1 };
      if (col < b.minCol) b.minCol = col;
      if (col > b.maxCol) b.maxCol = col;
      if (row < b.minRow) b.minRow = row;
      if (row > b.maxRow) b.maxRow = row;
      box.set(room, b);
    }
  }
  ctx.font = `${Math.max(11, scale * 2.4)}px system-ui, sans-serif`;
  for (const [room, b] of box) {
    const label = (map.room_names || {})[room] || `Room ${room}`;
    const cx = (b.minCol + b.maxCol) / 2 + 0.5;
    const cy = (b.minRow + b.maxRow) / 2 + 0.5;
    // Flip the row the same way the grid is drawn (see drawBase).
    const x = cx * scale;
    const y = (rows - 1 - cy) * scale;
    ctx.save();
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.lineWidth = 3;
    ctx.strokeStyle = "rgba(0,0,0,.55)";
    ctx.strokeText(label, x, y);
    ctx.fillStyle = "#fff";
    ctx.fillText(label, x, y);
    ctx.restore();
  }
}

/**
 * How wide something of `mm` millimetres is on the canvas.
 *
 * Exported because a caller often needs it without drawing: to size a hit
 * target, or to decide whether the vacuum is large enough on screen to be
 * worth labelling.
 */
export function footprintPx(map, scale = 1, mm = VACUUM_FOOTPRINT_MM) {
  return Math.max(MIN_SPRITE_PX, (mm / map.grid_size) * scale);
}

// Sprites, loaded once per page and shared by every canvas on it. Kept in the
// module rather than passed around because a card that redraws on each state
// update must not re-decode a PNG each time.
const SPRITES = { vacuum: null, dock: null };
let spritesPromise = null;

/** Where a sprite lives - beside this module, wherever it is served from. */
export function spriteUrl(name) {
  return new URL(`sprites/${name}.png`, import.meta.url).href;
}

/**
 * Fetch the sprites. Await it before the first draw for a sharp result;
 * skip it and the drawing falls back to plain shapes, so nothing breaks if
 * the images are missing, blocked, or this runs somewhere without a DOM.
 */
export function loadSprites() {
  if (spritesPromise) return spritesPromise;
  if (typeof Image === "undefined") {
    spritesPromise = Promise.resolve(SPRITES);
    return spritesPromise;
  }
  spritesPromise = Promise.all(
    Object.keys(SPRITES).map((name) => new Promise((resolve) => {
      const image = new Image();
      image.onload = () => { SPRITES[name] = image; resolve(); };
      // Resolve rather than reject: a missing sprite is a cosmetic problem,
      // and a rejected promise here would take the whole map down with it.
      image.onerror = () => resolve();
      image.src = spriteUrl(name);
    }))
  ).then(() => SPRITES);
  return spritesPromise;
}

/**
 * Draw a sprite centred on a world coordinate, turned to face `heading`.
 *
 * The negated rotation is the app's, and it is not arbitrary: headings grow
 * anticlockwise while canvas angles grow clockwise. Both sprites are drawn
 * facing +x at 0 degrees, matching the field-of-view cone.
 */
function drawSprite(ctx, map, name, place, size, scale, fallback) {
  const { x, y } = worldToPixel(map, place.x, place.y, scale);
  const image = SPRITES[name];
  if (!image) return fallback(x, y, size / 2);
  ctx.save();
  ctx.translate(x, y);
  if (place.heading != null) ctx.rotate((-place.heading * Math.PI) / 180);
  // The sprites are small and being scaled up; smoothing them keeps the
  // outline round, where the map itself deliberately stays pixelated.
  ctx.imageSmoothingEnabled = true;
  ctx.drawImage(image, -size / 2, -size / 2, size, size);
  ctx.restore();
}

/**
 * The vacuum at its true size, with a cone showing where the camera points.
 *
 * `heading` is degrees as the device reports them; the cone is drawn about it
 * so the direction reads at a glance rather than needing the number.
 */
export function drawVacuum(ctx, map, pose, { scale = 1, fov = 70, reach = 900,
                                             colour = "#ff5252", opacity = 1,
                                             footprint = VACUUM_FOOTPRINT_MM } = {}) {
  if (!pose || pose.x == null || pose.y == null) return;
  if (opacity !== 1) {
    // A ghost at a candidate point: the picker draws the machine where it
    // would end up, so its footprint against the walls is visible before
    // anything is sent to it.
    ctx.save();
    ctx.globalAlpha = opacity;
    try {
      drawVacuum(ctx, map, pose, { scale, fov, reach, colour, footprint });
    } finally {
      ctx.restore();
    }
    return;
  }
  const { x, y } = worldToPixel(map, pose.x, pose.y, scale);
  const size = footprintPx(map, scale, footprint);
  const radius = size / 2;

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

  drawSprite(ctx, map, "vacuum", pose, size, scale, () => {
    // No sprite: a disc of the same footprint, with a nose for the heading.
    ctx.beginPath();
    ctx.arc(x, y, radius, 0, Math.PI * 2);
    ctx.fillStyle = colour;
    ctx.fill();
    ctx.lineWidth = 2;
    ctx.strokeStyle = "#fff";
    ctx.stroke();
    if (pose.heading != null) {
      const angle = (-pose.heading * Math.PI) / 180;
      ctx.beginPath();
      ctx.moveTo(x, y);
      ctx.lineTo(x + Math.cos(angle) * radius, y + Math.sin(angle) * radius);
      ctx.stroke();
    }
  });
}

/** The dock, drawn the same way and at the same 1.2x the app uses. */
export function drawDock(ctx, map, dock, { scale = 1, colour = "#4caf50" } = {}) {
  if (!dock || dock.x == null || dock.y == null) return;
  const size = footprintPx(map, scale, VACUUM_FOOTPRINT_MM * DOCK_SCALE);
  drawSprite(ctx, map, "dock", dock, size, scale, (x, y, radius) => {
    ctx.beginPath();
    ctx.arc(x, y, radius, 0, Math.PI * 2);
    ctx.fillStyle = colour;
    ctx.fill();
    ctx.lineWidth = 2;
    ctx.strokeStyle = "#fff";
    ctx.stroke();
  });
}

/**
 * The heading from one world point toward another, in the device's own
 * convention: degrees, 0 along +x, growing anticlockwise, 0-359.
 *
 * This is the whole of the drag maths for the heading editor - the editor
 * turns a pointer position into a world point and asks this what the vacuum
 * would then be facing. Kept here so the convention lives in exactly one
 * place, beside the drawing code that negates it for the canvas.
 */
export function headingFromPoints(from, to) {
  const degrees = (Math.atan2(to.y - from.y, to.x - from.x) * 180) / Math.PI;
  return Math.round((degrees + 360) % 360);
}

/**
 * Where the heading editor's drag knob sits, in canvas pixels.
 *
 * Exported separately from the drawing so a pointer-down can be hit-tested
 * against the same circle the user sees - a knob drawn in one place and
 * grabbed in another is the map-mirroring bug in miniature.
 */
export function headingHandle(map, pose, scale = 1) {
  const centre = worldToPixel(map, pose.x, pose.y, scale);
  const radius = footprintPx(map, scale) / 2;
  const reach = radius + Math.max(16, radius * 0.7);
  const angle = (-(pose.heading || 0) * Math.PI) / 180;
  return {
    x: centre.x + Math.cos(angle) * reach,
    y: centre.y + Math.sin(angle) * reach,
    r: Math.max(10, radius * 0.4),
    ring: reach,
  };
}

/**
 * The rotate control: a ring around the vacuum with a knob on it at the
 * current heading. The vacuum itself (and its field of view) is drawn by
 * drawVacuum - this only adds the affordance for changing it.
 */
export function drawHeadingHandle(ctx, map, pose, { scale = 1, colour = "#ff9800" } = {}) {
  if (!pose || pose.x == null || pose.y == null) return;
  const centre = worldToPixel(map, pose.x, pose.y, scale);
  const knob = headingHandle(map, pose, scale);
  ctx.save();

  // The ring the knob travels on, dashed so it reads as a track rather than
  // a boundary.
  ctx.beginPath();
  ctx.arc(centre.x, centre.y, knob.ring, 0, Math.PI * 2);
  ctx.setLineDash([4, 5]);
  ctx.lineWidth = 1.5;
  ctx.strokeStyle = colour;
  ctx.globalAlpha = 0.8;
  ctx.stroke();
  ctx.setLineDash([]);
  ctx.globalAlpha = 1;

  // Spoke from the vacuum to the knob, so knob and machine read as one
  // control even when the ring is faint on a busy map.
  ctx.beginPath();
  ctx.moveTo(centre.x, centre.y);
  ctx.lineTo(knob.x, knob.y);
  ctx.lineWidth = 2;
  ctx.strokeStyle = colour;
  ctx.stroke();

  ctx.beginPath();
  ctx.arc(knob.x, knob.y, knob.r, 0, Math.PI * 2);
  ctx.fillStyle = colour;
  ctx.fill();
  ctx.lineWidth = 2;
  ctx.strokeStyle = "#fff";
  ctx.stroke();

  // Two small arrows on the ring either side of the knob: "this turns".
  const heading = (-(pose.heading || 0) * Math.PI) / 180;
  for (const dir of [-1, 1]) {
    const at = heading + dir * (Math.PI / 7);
    const ax = centre.x + Math.cos(at) * knob.ring;
    const ay = centre.y + Math.sin(at) * knob.ring;
    const tangent = at + (dir * Math.PI) / 2;
    ctx.beginPath();
    ctx.moveTo(ax, ay);
    ctx.lineTo(ax + Math.cos(tangent - 0.5) * 6, ay + Math.sin(tangent - 0.5) * 6);
    ctx.moveTo(ax, ay);
    ctx.lineTo(ax + Math.cos(tangent + 0.5) * 6, ay + Math.sin(tangent + 0.5) * 6);
    ctx.lineWidth = 2;
    ctx.strokeStyle = colour;
    ctx.stroke();
  }
  ctx.restore();
}

/**
 * A small camera badge beside a pose - marks "a photo is taken here" on a
 * step that has no geometry of its own. Offset to the upper right so it does
 * not sit on the vacuum it annotates.
 */
export function drawCameraBadge(ctx, map, pose, { scale = 1, colour = "#455a64" } = {}) {
  if (!pose || pose.x == null || pose.y == null) return;
  const centre = worldToPixel(map, pose.x, pose.y, scale);
  const radius = footprintPx(map, scale) / 2;
  const size = Math.max(14, radius * 0.7);
  const x = centre.x + radius * 0.9;
  const y = centre.y - radius * 0.9 - size;
  ctx.save();
  ctx.beginPath();
  const r = 3;
  ctx.roundRect
    ? ctx.roundRect(x, y, size * 1.3, size, r)
    : ctx.rect(x, y, size * 1.3, size);
  ctx.fillStyle = colour;
  ctx.fill();
  ctx.lineWidth = 1.5;
  ctx.strokeStyle = "#fff";
  ctx.stroke();
  // The lens.
  ctx.beginPath();
  ctx.arc(x + size * 0.65, y + size / 2, size * 0.28, 0, Math.PI * 2);
  ctx.strokeStyle = "#fff";
  ctx.lineWidth = 2;
  ctx.stroke();
  ctx.restore();
}

/**
 * Whole pixels per cell for a canvas that should fill `cssWidth`.
 *
 * Shared by the dashboard card and the task editor so both size the same
 * way: rounded up (scaling a grid of flat colours down a little is
 * invisible; up leaves cells unevenly wide), capped so a very wide map in a
 * very wide browser cannot ask for tens of megapixels.
 */
export function fitScale(map, cssWidth, { dpr = 1, max = 4096 } = {}) {
  if (!cssWidth) return map.suggested_scale || 5;
  const wanted = Math.ceil((cssWidth * Math.min(dpr || 1, 2)) / map.cols);
  const ceiling = Math.max(2, Math.floor(max / map.cols));
  return Math.max(2, Math.min(ceiling, wanted));
}

/**
 * A chosen point, drawn as the vacuum standing on it.
 *
 * A dot says where the coordinate is; this says what choosing it means. The
 * ring is the machine's real 320mm outline, so a target that cannot fit
 * between two walls looks wrong before it is saved rather than after it has
 * been driven at.
 */
export function drawTarget(ctx, map, point, { scale = 1, colour = "#ff5252",
                                              label = null, opacity = .65 } = {}) {
  if (!point || point.x == null || point.y == null) return;
  const { x, y } = worldToPixel(map, point.x, point.y, scale);
  const radius = footprintPx(map, scale) / 2;

  drawVacuum(ctx, map, point, { scale, fov: 0, opacity, colour });

  ctx.save();
  ctx.beginPath();
  ctx.arc(x, y, radius, 0, Math.PI * 2);
  ctx.lineWidth = 2;
  ctx.strokeStyle = colour;
  ctx.stroke();
  // A cross at the exact coordinate: the outline shows the footprint, but the
  // number in the readout is this point, and at a glance the two are easy to
  // confuse.
  const tick = Math.max(3, radius * 0.28);
  ctx.beginPath();
  ctx.moveTo(x - tick, y); ctx.lineTo(x + tick, y);
  ctx.moveTo(x, y - tick); ctx.lineTo(x, y + tick);
  ctx.stroke();
  ctx.restore();

  if (label) drawLabel(ctx, label, x, y - radius - 3, scale);
}

/** A labelled point - kept for a plain marker where a footprint is wrong. */
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
  if (label) drawLabel(ctx, label, x, y - radius - 3, scale);
}

/** White text with a dark halo, so it reads on any room colour. */
function drawLabel(ctx, text, x, y, scale) {
  ctx.save();
  ctx.font = `${Math.max(11, scale * 2.2)}px system-ui, sans-serif`;
  ctx.textAlign = "center";
  ctx.textBaseline = "bottom";
  ctx.lineWidth = 3;
  ctx.strokeStyle = "rgba(0,0,0,.55)";
  ctx.strokeText(text, x, y);
  ctx.fillStyle = "#fff";
  ctx.fillText(text, x, y);
  ctx.restore();
}

/**
 * Room id at a canvas pixel, or null for a wall / outside the map.
 *
 * The Room tab makes rooms tappable on the map itself, so a tap needs to know
 * which room it landed on. `pixelToWorld` inverts the drawing transform, then
 * `describePoint` resolves the world point to a room through the grid - the
 * same path the point picker uses, so a click can never disagree with what a
 * tap meant to hit.
 */
export function roomAtPixel(map, px, py, scale = 1) {
  const w = pixelToWorld(map, px, py, scale);
  const info = describePoint(map, w.x, w.y);
  return info.ok ? info.room : null;
}

/**
 * Per-room bounding boxes, in world cells. Shared by the picker's labels and
 * the Room tab's selection badges so both place markers at the same spot.
 */
function roomBoxes(map) {
  const box = new Map();
  const { cols, rows, grid } = map;
  for (let row = 0; row < rows; row++) {
    for (let col = 0; col < cols; col++) {
      const value = grid[row * cols + col];
      const room = value >> 2;
      if (!value || room === WALL) continue;
      const b = box.get(room) || { minCol: cols, minRow: rows, maxCol: -1, maxRow: -1 };
      if (col < b.minCol) b.minCol = col;
      if (col > b.maxCol) b.maxCol = col;
      if (row < b.minRow) b.minRow = row;
      if (row > b.maxRow) b.maxRow = row;
      box.set(room, b);
    }
  }
  return box;
}

/**
 * The map with rooms selectable, for the Room tab.
 *
 * Unselected rooms are drawn semi-transparent so they recede; selected rooms
 * stay full colour and get a numbered bubble next to their name showing the
 * order in which they were picked (matching the phone app, where the number is
 * the cleaning order). `selectedOrder` is an array of room ids in pick order.
 */
export function drawRoomSelection(ctx, map, { scale = 1, selectedOrder = [] } = {}) {
  const selected = new Set(selectedOrder.map(String));
  const { cols, rows, grid } = map;
  ctx.save();
  ctx.imageSmoothingEnabled = false;
  const box = roomBoxes(map);

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
        if (!selected.has(String(room))) ctx.globalAlpha = 0.35;
      }
      ctx.fillStyle = colour;
      ctx.fillRect(col * scale, (rows - 1 - row) * scale, scale, scale);
      ctx.globalAlpha = 1;
    }
  }

  // Room names on every room, then a number bubble beside selected rooms.
  drawRoomNames(ctx, map, scale);
  for (const [room, b] of box) {
    const order = selectedOrder.indexOf(String(room));
    if (order < 0) continue;
    const cx = (b.minCol + b.maxCol) / 2 + 0.5;
    const cy = (b.minRow + b.maxRow) / 2 + 0.5;
    const x = cx * scale;
    const y = (rows - 1 - cy) * scale;
    drawOrderBubble(ctx, order + 1, x + Math.max(10, scale * 2.6), y, scale);
  }
  ctx.restore();
}

/** The numbered circle the app shows next to a selected room. */
function drawOrderBubble(ctx, number, x, y, scale) {
  const r = Math.max(11, scale * 2.4);
  ctx.save();
  ctx.beginPath();
  ctx.arc(x, y, r, 0, Math.PI * 2);
  ctx.fillStyle = "#03a9f4";
  ctx.fill();
  ctx.lineWidth = Math.max(2, scale * 0.5);
  ctx.strokeStyle = "#fff";
  ctx.stroke();
  ctx.font = `bold ${Math.max(12, scale * 2.6)}px system-ui, sans-serif`;
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  ctx.fillStyle = "#fff";
  ctx.fillText(String(number), x, y + 1);
  ctx.restore();
}

function shade(hex, factor) {
  if (typeof hex !== "string" || !/^#[0-9a-f]{6}$/i.test(hex)) return hex || "#000";
  const n = parseInt(hex.slice(1), 16);
  const r = Math.round(((n >> 16) & 255) * factor);
  const g = Math.round(((n >> 8) & 255) * factor);
  const b = Math.round((n & 255) * factor);
  return `rgb(${r},${g},${b})`;
}
