# dreame_vacuum_unlocked_integration

Layered rewrite of the Dreame vacuum integration, built around generated
device profiles instead of hand-maintained mapping tables.

```
transport/     vendored: login, request signing, MQTT  (sync; executor only)
profiles/      generated JSON - service vocabulary + per-model capabilities
profile.py     loads the above; keeps vocabulary/flags/presence separate
coordinator.py push-first state, adaptive polling, device keep-alive
companion.py   client for the dreame_vacuum_unlocked add-on (camera)
*.py           thin HA entity platforms
```

## Three things that are easy to conflate

1. **Vocabulary** (`_services.json`, merged across models) — *what siid 32 is*.
   A dictionary. Safe to merge; says nothing about your device.
2. **Feature flags** (`<model>.json`) — Dreame's own published manifest.
   Good for gating optional behaviour. **Not** a service inventory: there is
   no flag meaning "has a fluffing roller".
3. **Presence** — whether *this unit* implements a property. Only knowable by
   probing: unsupported reads return `code: -1`, supported ones `code: 0`.

Entities are only created when the vocabulary knows the property *and* the
device answered for it. Merging vocabulary into presence is how you end up
with a fluffing-roller sensor on a vacuum that has none.

## Regenerating profiles

```bash
export DREAME_USERNAME=... DREAME_PASSWORD=... DREAME_COUNTRY=eu
export DREAME_LIB_PATH=../dreame-vacuum-unlocked/dreame_vacuum_unlocked
python3 scripts/fetch_plugins.py --models-file scripts/models.txt --from-account
python3 scripts/extract_profiles.py
```

Profiles ship with the integration, so installs never depend on Dreame's
servers. See `scripts/README.md`.

## Camera

Camera/streaming lives in the `dreame_vacuum_unlocked` add-on because
Tencent's XP2P libraries are x86_64-only. Keeping it out of process is what
lets this integration stay pure Python and run on ARM (Pi, HA Green/Yellow).
Camera setup is an optional, skippable step in the config flow.

## Status

Working: config flow, profile loading, push+poll coordinator, device
keep-alive, vacuum entity (start/stop/pause/return/locate + battery),
battery/volume sensors, camera + snapshot via the companion add-on, and
device registration with the companion UI.

Not yet: map handling, consumable sensors, `playSound` (siid 7 / aiid 2 /
piid 4), fan-speed/water-level selects, closed-loop `rotate_to`.
