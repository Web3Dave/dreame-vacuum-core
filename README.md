# Dreame Vacuum Core — Home Assistant Integration

A Home Assistant integration for Dreame robot vacuums, built around **generated
device profiles** rather than hand-maintained mapping tables: the `siid`/`piid`/
`aiid` numbers and per-model capability flags are extracted from Dreame's own
plugin bundles, so supporting a new model is mostly data rather than code.

Camera streaming is handled by the companion
[Dreame Vacuum Companion add-on](https://github.com/Web3Dave/dreame-vacuum-companion),
which is optional.

**This only works for a device and account you own.** The signing algorithm and
API endpoints this relies on are undocumented and reverse-engineered — Dreame
could change them at any time without notice.

## Why the split

Tencent's XP2P libraries (which the camera needs) are published for x86_64 Linux
only — there is no ARM build. Keeping streaming in a separate add-on is what
allows this integration to stay pure Python and run on a Raspberry Pi, HA Green
or HA Yellow.

| | Where | Runs on |
|---|---|---|
| Vacuum control, state, entities | this integration | anything HA runs on |
| Camera streaming + snapshots | companion add-on | x86_64 only |

Camera setup is a skippable step in the config flow. Without it you still get
the vacuum, sensors and controls.

## Entities

Per device:

| Entity | Notes |
|---|---|
| `vacuum.<name>` | start / stop / pause / return to base / locate, battery |
| `sensor.<name>_battery` | |
| `sensor.<name>_volume` | |
| `camera.<name>_camera` | only with the add-on. `stream_source` starts the RTSP feed lazily when you open the live view; the thumbnail is the last snapshot, so rendering it never wakes the camera |
| `button.<name>_take_snapshot` | only with the add-on. Saves to the Media Browser |

Sensors are only created when the profile knows the property **and** the device
actually answered for it, so a model without a given part never gets a phantom
entity.

## Installation

1. *(Optional, for camera)* Install and start the
   [companion add-on](https://github.com/Web3Dave/dreame-vacuum-companion) and
   set an `api_token` in its Configuration tab.
2. Copy `custom_components/dreame_vacuum_core/` into your Home Assistant
   `config/custom_components/` directory, or add this repo to HACS as a custom
   repository.
3. Restart Home Assistant.
4. **Settings → Devices & Services → Add Integration → Dreame Vacuum Core**.
5. Enter your Dreame account username, password and region, then pick a device.
6. On the camera step, either enter the add-on's host/port/token and the
   camera's 4-digit privacy PIN, or **leave the token blank to skip it**.

## How profiles work

Three things that are easy to conflate, and are deliberately kept apart:

1. **Vocabulary** — `profiles/_services.json`, generated and safe to merge
   across models. Says *what* `siid 32` is. A dictionary; it implies nothing
   about your device.
2. **Feature flags** — `profiles/<model>.json`, Dreame's own published manifest
   (~125 flags such as `supportDrySpeed`). Good for gating optional behaviour.
   **Not** a service inventory — there is no flag meaning "has a fluffing
   roller".
3. **Presence** — whether *this unit* implements a property. Only knowable by
   probing: unsupported reads return `code: -1`, supported ones `code: 0`.

Both generated files ship with the integration, so setup never depends on
Dreame's servers being reachable. A model with no shipped manifest still works —
it gets the shared vocabulary and falls back to probing.

Regenerate with `scripts/` (see [scripts/README.md](scripts/README.md)):

```bash
export DREAME_USERNAME=... DREAME_PASSWORD=... DREAME_COUNTRY=eu
export DREAME_LIB_PATH=../dreame-vacuum-companion/dreame_vacuum_companion
python3 scripts/fetch_plugins.py --models-file scripts/models.txt --from-account
python3 scripts/extract_profiles.py
```

## Layout

```
custom_components/dreame_vacuum_core/
  transport/     vendored: login, request signing, MQTT (sync - executor only)
  profiles/      generated JSON: service vocabulary + model manifests
  profile.py     loads the above, keeps the three concerns separate
  coordinator.py MQTT push primary, adaptive polling to reconcile, keep-alive
  companion.py   client for the companion add-on
  vacuum.py sensor.py camera.py button.py
scripts/         profile generation (dev-time only)
```

`transport/` is carried over from the add-on rather than rewritten — the signing
scheme was recovered by instrumenting the real app, and a second from-scratch
implementation would risk being subtly wrong for no benefit. Everything above it
is new.

## Two things the device requires

- **A keep-alive every ~25s** (`siid 14 / piid 4`). The device stops sending
  non-essential data when it lapses. The coordinator handles this.
- **A separate camera keep-alive** (`siid 10001 / aiid 1 / piid 6`) while a
  stream is running, which the add-on owns. Miss it and the video freezes
  abruptly after ~60s while the connection still looks healthy.

## Status

This is an early implementation. Working: config flow, profile loading,
push + poll coordination, device keep-alive, the entities listed above, and
device registration with the companion add-on's UI.

Not yet: map handling, consumable sensors, `playSound`, fan-speed and
water-level selects, and closed-loop `rotate_to` (pose is only available from
map data at roughly 0.4 Hz, so that needs step → settle → measure rather than
continuous control).

Known rough edges:

- The vacuum status mapping covers the common values; unmapped ones log at
  debug and leave the state as unknown.
- `pause` maps to the device's stop action — there is no distinct pause action
  in the profile.
- One account per config entry.

## Legacy

`custom_components/dreame_camera_capture/` is the earlier camera-only
integration, superseded by `dreame_vacuum_core`. It is kept temporarily so
existing installs keep working and will be removed.
