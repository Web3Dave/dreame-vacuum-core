# Dreame Vacuum Camera Capture — Home Assistant Integration

Owns the config UI, device discovery, and entities for streaming/snapshotting your
Dreame vacuum's onboard camera. Requires the companion
[Dreame Vacuum Camera Capture add-on](https://github.com/Web3Dave/dreame-vacuum-video-capture)
to actually be installed and running first - this integration has no copy of the
reverse-engineered Dreame/XP2P pipeline itself, it just drives the add-on's API.

**This only works for a device and account you own.** Don't point it at anyone
else's vacuum or account.

## Architecture

- **This integration** — config flow (add-on connection + Dreame account + PIN +
  device selection), and three entities per enabled device:
  - `camera.<name>_camera` — `stream_source` lazily starts a real RTSP stream via
    the add-on when you open the live view; the still-image thumbnail is whatever
    snapshot the add-on already has, no activation triggered just to render it.
  - `switch.<name>_stream` — explicit start/stop for the RTSP stream, independent
    of the camera entity's own lazy behavior.
  - `button.<name>_take_photo` — one-shot: activation → grab a frame → save to
    Home Assistant's Media Browser under `dreame-capture/<did>/` → tear down.
- **The add-on** — does all the actual work (Dreame login/signing, the camera PIN
  activation sequence, Tencent's XP2P SDK, ffmpeg, and a bundled RTSP server). It's
  stateless about your identity - every request from this integration carries its
  own credentials and target device.

## Installation

1. Install and start the [add-on](https://github.com/Web3Dave/dreame-vacuum-video-capture)
   first, and set an `api_token` (any random string) in its Configuration tab.
2. Copy `custom_components/dreame_camera_capture/` into your Home Assistant's
   `config/custom_components/` directory (or add this repo via HACS as a custom
   repository).
3. Restart Home Assistant.
4. **Settings → Devices & Services → Add Integration → Dreame Vacuum Camera Capture**.
5. Enter:
   - The add-on's host/port (`localhost`/`8099` if HA Core and the add-on share a
     host, otherwise the HA machine's LAN IP)
   - The same `api_token` you set in the add-on
   - Your Dreame account username/password/region
   - The camera's 4-digit privacy PIN
6. On the next screen, pick which of your account's devices to expose - each
   becomes its own set of camera/switch/button entities.

## Notes

- One `api_token`/account per config entry currently - if you have devices on
  different Dreame accounts, you'd need a second add-on instance (different port)
  and a second integration entry.
- The signing algorithm and API endpoints the add-on relies on are undocumented and
  reverse-engineered — Dreame could change them at any time without notice.
