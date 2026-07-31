/**
 * A live map card for a Dreame vacuum.
 *
 * The map itself changes rarely, the robot on it changes constantly - so the
 * two are fetched and drawn on different clocks. The grid is fetched once and
 * kept; the pose comes from the vacuum entity's attributes, which the robot
 * pushes over MQTT while it moves, and every push repaints just the overlay.
 *
 * Drawing is `map.js`, the same module the add-on's picker imports, so the
 * coordinate transform here cannot drift from the one that turns a click into
 * a task.
 *
 *   type: custom:dreame-map-card
 *   entity: vacuum.snuffles
 */

// Beside this file, wherever the integration is mounted - and carrying the
// same cache-busting query, so the card and the renderer are never a mixed
// pair of old and new.
const MODULE = new URL(`map.js${new URL(import.meta.url).search}`, import.meta.url).href;

// Widest backing store we will allocate, in pixels.
const MAX_CANVAS_PX = 4096;

/**
 * What this instance is for.
 *
 * One component, several jobs: the same map, transform and sprites serve a
 * dashboard, a point picker, and - next - a heading picker. They differ only
 * in what they do with a gesture, so that is what the mode selects. Anything
 * that is true of the map itself belongs in `map.js` and is shared by all of
 * them; anything a mode needs alone belongs here.
 *
 * `view` is the default deliberately. On a dashboard, clicking a map reads as
 * "send the vacuum here", so a card that quietly accepted clicks and did not
 * send it anywhere was the wrong thing on both counts.
 */
const MODE_VIEW = "view";
const MODE_PICK_POINT = "pick-point";
const MODES = [MODE_VIEW, MODE_PICK_POINT];

class DreameMapCard extends HTMLElement {
  static getStubConfig() {
    return { entity: "" };
  }

  setConfig(config) {
    if (!config.entity || !config.entity.startsWith("vacuum.")) {
      throw new Error("dreame-map-card needs a vacuum entity, e.g. vacuum.snuffles");
    }
    const mode = config.mode || MODE_VIEW;
    if (!MODES.includes(mode)) {
      throw new Error(
        `dreame-map-card: unknown mode ${JSON.stringify(mode)}. ` +
        `Expected one of: ${MODES.join(", ")}`);
    }
    this._config = { showRoomNames: true, showDock: true, fov: 70, ...config, mode };
    this._built = false;
    this._doc = null;
    this._mapId = null;
    this._picked = null;
  }

  /** Whether this instance takes input at all. */
  get _interactive() {
    return this._config.mode !== MODE_VIEW;
  }

  getCardSize() {
    return 6;
  }

  /**
   * Sections dashboards lay cards out on a 12-column grid, and a custom card
   * that says nothing gets the narrow default - which is why widening the
   * section did not widen the map. A map is a picture of a floor: it wants
   * the width it can get, so the default is the full row, still draggable
   * narrower down to a quarter.
   */
  getGridOptions() {
    return { columns: "full", rows: "auto", min_columns: 3, min_rows: 3 };
  }

  /** The same, under the name Home Assistant used before 2024.11. */
  getLayoutOptions() {
    return { grid_columns: "full", grid_rows: "auto", grid_min_columns: 3 };
  }

  set hass(hass) {
    this._hass = hass;
    const state = hass.states[this._config.entity];
    if (!state) return this._fail(`${this._config.entity} does not exist`);

    this._build();
    const attrs = state.attributes;

    // The grid is only refetched when the vacuum says it is on a different
    // map - a rescan or a different floor. Refetching on every pose update
    // would move tens of kilobytes several times a second.
    if (attrs.map_id != null && attrs.map_id !== this._mapId) {
      this._mapId = attrs.map_id;
      this._failed = false;
      this._fetchMap(false);
    } else if (!this._doc && !this._loading && !this._failed) {
      // Only once. A vacuum that has never sent a frame would otherwise be
      // asked for its map on every state update, which is several a second
      // while it is moving.
      this._fetchMap(false);
    }

    this._pose = attrs.position_x == null ? null : {
      x: attrs.position_x, y: attrs.position_y, heading: attrs.heading,
    };
    this._status = attrs.located === false
      ? "On the map but not localised"
      : state.state;
    this._draw();
  }

  // -- setup ---------------------------------------------------------------

  _build() {
    if (this._built) return;
    this._built = true;
    this.innerHTML = `
      <ha-card>
        <div class="dm-head">
          <span class="dm-title"></span>
          <span class="dm-status"></span>
          <ha-icon-button class="dm-refresh" title="Ask the vacuum for a fresh map">
            <ha-icon icon="mdi:refresh"></ha-icon>
          </ha-icon-button>
        </div>
        <div class="dm-body"><canvas></canvas></div>
        <div class="dm-foot"></div>
      </ha-card>
      <style>
        ha-card { overflow: hidden; }
        .dm-head { display: flex; align-items: center; gap: 8px;
                   padding: 12px 8px 4px 16px; }
        .dm-title { font-weight: 500; }
        .dm-status { color: var(--secondary-text-color); font-size: .9em;
                     margin-left: auto; }
        .dm-body { padding: 8px 12px; }
        /* Filled to the container width. The backing store is rounded up to
           the next whole pixel per cell, so this only ever scales down a
           little - which is why smoothing is left on here, unlike the
           picker, where the canvas is drawn at its natural size. */
        /* The crosshair is set from the mode, not here: in view mode the map
           is something you look at, and a cursor promising otherwise lies. */
        .dm-body canvas { width: 100%; height: auto; display: block; }
        .dm-foot { padding: 4px 16px 14px; min-height: 1.2em;
                   color: var(--secondary-text-color); font-size: .9em; }
        .dm-foot.dm-error { color: var(--error-color); }
      </style>`;
    this._canvas = this.querySelector("canvas");
    this._foot = this.querySelector(".dm-foot");
    this._observe();
    this.querySelector(".dm-title").textContent = this._config.title || "Map";
    this.querySelector(".dm-refresh").addEventListener("click", () => this._fetchMap(true));
    if (this._interactive) {
      this._canvas.style.cursor = "crosshair";
      this._canvas.addEventListener("click", (event) => this._onClick(event));
    }
  }

  disconnectedCallback() {
    // Editing a dashboard builds and discards cards repeatedly; an observer
    // left attached to a detached node keeps the whole card alive with it.
    this._observer?.disconnect();
    this._observer = null;
  }

  connectedCallback() {
    if (this._built) this._observe();
  }

  /**
   * Redraw whenever the card's width changes.
   *
   * The card is laid out by the dashboard, not by itself, so the width it
   * gets is only known once it is on screen - and changes when the section is
   * widened or the browser resized.
   */
  _observe() {
    if (this._observer || typeof ResizeObserver === "undefined") return;
    this._observer = new ResizeObserver(() => this._draw());
    this._observer.observe(this.querySelector(".dm-body"));
  }

  async _module() {
    if (!this._api) {
      this._api = await import(MODULE);
      await this._api.loadSprites();
    }
    return this._api;
  }

  async _fetchMap(refresh) {
    const did = (this._hass.states[this._config.entity].attributes || {}).did;
    if (!did) return this._fail("This vacuum has not reported a device id yet");
    this._loading = true;
    this._failed = false;
    this._say(refresh ? "Asking the vacuum for a fresh map..." : "Loading map...");
    try {
      const api = await this._module();
      const path = `dreame_vacuum_core/map/${did}${refresh ? "?refresh=1" : ""}`;
      this._doc = api.decodeMap(await this._hass.callApi("GET", path));
      this._mapId = this._doc.map_id;
      this._say("");
      this._draw();
    } catch (err) {
      // callApi rejects with the response body, which carries the reason the
      // integration worked out - much more useful than "failed to fetch".
      this._failed = true;
      this._fail(err?.body?.message || err?.message || "Could not load the map");
    } finally {
      this._loading = false;
    }
  }

  // -- drawing -------------------------------------------------------------

  /**
   * Pixels per map cell for the width the card was actually given.
   *
   * The maths lives in map.js (`fitScale`) so the task editor sizes its
   * canvas identically - card and module ship together, so no version guard
   * is needed here.
   */
  _scaleFor(map) {
    const width = this._canvas?.parentElement?.clientWidth || 0;
    return this._api.fitScale(map, width, {
      dpr: window.devicePixelRatio || 1, max: MAX_CANVAS_PX,
    });
  }

  _draw() {
    if (!this._doc || !this._api) return;
    const map = this._doc;
    const scale = this._scaleFor(map);
    const canvas = this._canvas;
    if (canvas.width !== map.cols * scale) {
      canvas.width = map.cols * scale;
      canvas.height = map.rows * scale;
    }
    // Remembered because it is no longer the document's suggestion: the two
    // part company the moment the card is resized.
    this._scale = scale;
    const ctx = canvas.getContext("2d");
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    this._api.drawBase(ctx, map, { scale, showRoomNames: this._config.showRoomNames });
    if (this._config.showDock && map.dock) {
      this._api.drawDock(ctx, map, {
        x: map.dock[0], y: map.dock[1], heading: map.dock_angle,
      }, { scale });
    }
    // The pose comes from the entity, not from the map document: the document
    // is a snapshot, the entity is live.
    if (this._pose) {
      this._api.drawVacuum(ctx, map, this._pose, { scale, fov: this._config.fov });
    }
    // Whatever this mode has chosen, drawn last so it sits above the robot.
    if (this._picked) this._api.drawTarget(ctx, map, this._picked, { scale });
    this.querySelector(".dm-status").textContent = this._status || "";
  }

  /**
   * A gesture on the map, turned into a world coordinate and handed to the
   * mode. Only the interpretation differs between modes - the transform, the
   * refusal of walls, and the readout are the same wherever a point is being
   * chosen, so they live here rather than in each of them.
   */
  _onClick(event) {
    if (!this._doc) return;
    const canvas = this._canvas;
    const rect = canvas.getBoundingClientRect();
    // The canvas is displayed at whatever width fits, so translate into its
    // own pixels before asking the shared module for a coordinate.
    const ratio = canvas.width / rect.width;
    const scale = this._scale || this._doc.suggested_scale || 5;
    const point = this._api.pixelToWorld(
      this._doc, (event.clientX - rect.left) * ratio,
      (event.clientY - rect.top) * ratio, scale);

    const where = this._api.describePoint(this._doc, point.x, point.y);
    if (!where.ok) {
      // Choosing a wall produces an errand that drives about and gives up,
      // with nothing to say why - so refuse it here instead.
      this._picked = null;
      this._say(`That is ${where.reason} - pick a spot on the floor.`, true);
      this._draw();
      return;
    }

    this._picked = point;
    this._say(`x ${point.x}, y ${point.y} in ${where.name}`);
    this._draw();
    // Announced rather than acted on: this component chooses a point, it does
    // not decide what a point means. A host - the task editor, a dialog, an
    // automation-building card - listens and does something with it.
    this.dispatchEvent(new CustomEvent("dreame-map-select", {
      bubbles: true, composed: true,
      detail: { mode: this._config.mode, x: point.x, y: point.y,
                room: where.room, name: where.name,
                entity: this._config.entity },
    }));
  }

  /** The chosen point, for a host that would rather ask than listen. */
  get selection() {
    return this._picked ? { ...this._picked } : null;
  }

  _say(text, isError = false) {
    if (this._foot) {
      this._foot.textContent = text;
      this._foot.classList.toggle("dm-error", isError);
    }
  }

  _fail(text) {
    this._build();
    if (this._foot) {
      this._foot.textContent = text;
      this._foot.classList.add("dm-error");
    }
  }
}

if (!customElements.get("dreame-map-card")) {
  customElements.define("dreame-map-card", DreameMapCard);
}

// Puts the card in the dashboard's "add card" list rather than making it
// something you have to know the name of.
window.customCards = window.customCards || [];
if (!window.customCards.some((c) => c.type === "dreame-map-card")) {
  window.customCards.push({
    type: "dreame-map-card",
    name: "Dreame Map",
    description: "Live map with the vacuum's position and heading.",
  });
}
