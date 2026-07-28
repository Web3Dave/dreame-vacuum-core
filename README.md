# Dreame Vacuum Core

Control your Dreame robot vacuum from Home Assistant — cleaning controls,
battery and status, and optionally a live camera feed.

> **Use it only with your own vacuum and account.** This talks to Dreame's
> servers the same way their app does, using undocumented endpoints. Dreame
> could change them at any time and break it without warning.

## What you get

| Entity | What it does |
|---|---|
| **Vacuum** | Start, stop, pause, send home, locate, battery level |
| **Battery** | Battery percentage |
| **Volume** | Speaker volume |
| **Camera** \* | Live view and still images |
| **Take snapshot** \* | Saves a photo to your Media browser |

\* Camera entities need the companion add-on — see below.

## What you need

- Home Assistant
- A Dreame account (the same one you use in the Dreamehome app)
- Your vacuum's **4-digit camera PIN**, if you want the camera

### About the camera

Camera support needs the separate
[Dreame Vacuum Companion](https://github.com/Web3Dave/dreame-vacuum-companion)
add-on, and that add-on **only runs on x86_64 hardware** — an Intel/AMD mini-PC,
NUC or VM. It will not run on a Raspberry Pi, Home Assistant Green or Yellow,
because the video libraries it depends on aren't published for those.

Everything else — cleaning, status, battery, controls — works on any Home
Assistant install. The camera step is optional and can be skipped.

## Install

**Via HACS** (recommended)

1. HACS → ⋮ → **Custom repositories**
2. Add `https://github.com/Web3Dave/dreame-vacuum-core` as an **Integration**
3. Install **Dreame Vacuum Core**, then restart Home Assistant

**Manually**

Copy `custom_components/dreame_vacuum_core/` into your Home Assistant
`config/custom_components/` folder and restart.

## Set up

1. **Settings → Devices & Services → Add Integration → Dreame Vacuum Core**
2. Enter your Dreame username, password and region (most European accounts are
   `eu` — check **Settings → Region** in the app if unsure)
3. Choose your vacuum
4. **Camera step** — either fill in the add-on's host, port, API token and your
   camera PIN, or **leave the token blank to skip the camera entirely**

## Supported vacuums

Any Dreame vacuum on your account should connect. The integration ships tuned
profiles for some models and automatically detects what the others support, so
an unrecognised vacuum still works — you may just see fewer entities.

If something is missing for your model, open an issue with the model name (it
looks like `dreame.vacuum.r2579h`) and it can usually be added.

## Not included yet

- Maps and room-based cleaning
- Consumable/filter life sensors
- Fan speed and water level selection
- Playing sounds through the vacuum

Some vacuum states may show as unknown — please report those with the model
name so the mapping can be filled in.

## Troubleshooting

**Won't sign in** — check the region matches your account. A wrong region looks
exactly like a wrong password.

**No camera entities** — the camera is opt-in. Re-add the integration and fill
in the add-on details on the camera step.

**Camera view freezes or won't start** — confirm the companion add-on is running
and its API token matches, and check the add-on's log.

---

Contributing? See [`custom_components/dreame_vacuum_core/README.md`](custom_components/dreame_vacuum_core/README.md)
for the architecture and [`scripts/README.md`](scripts/README.md) for how device
profiles are generated.
