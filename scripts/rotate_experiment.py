"""Does an open camera/monitor session stop the robot vacuuming while it turns?

Driving from the app's live view turns without running the brushes; the same
command sent any other way vacuums as it goes. This runs the same rotation
twice - once plain, once with a monitor session open - and snapshots the
device's properties around each, so the difference shows up as data rather
than as a guess.

    python scripts/rotate_experiment.py --did 2089953038

Credentials come from a .env in the repo root (DREAME_USERNAME,
DREAME_PASSWORD, VIDEO_STREAM_PIN). Put the robot somewhere open first - it
turns 40 degrees twice, on the spot.

Watch and note, for each rotation, whether the brushes and mop run. The script
cannot see that; you are the sensor for it.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dreame_api import _load_protocol  # noqa: E402
from dump_properties import collect, diff  # noqa: E402

# Rotation, matching the app's live view: spdw is a rate, held then released.
TURN_RATE_DPS = 45
REFRESH_SECONDS = 1.0
SIID_VACUUM_EXTEND, PIID_REMOTE_STATE = 4, 15

# Camera service, from the app's own plugin bundle.
SIID_MONITOR = 10001
AIID_CAMERA_OPERATE = 1
PIID_MONITOR = 1
PIID_STREAM_CODE_OPEN = 1100
PIID_VERIFY_ACCESS_CODE = 1102
AIID_STREAM_CODE = 4


def load_env(root: Path) -> dict:
    env: dict[str, str] = {}
    path = root / ".env"
    if not path.exists():
        return env
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def send_remote(protocol, rotation: int) -> None:
    payload = json.dumps(
        {
            "spdv": 0,
            "spdw": int(rotation),
            "audio": "false",
            "random": int(time.time() * 1000) % 1000,
            "timestamp": int(time.time() * 1000),
        },
        separators=(",", ":"),
    )
    protocol.set_property(SIID_VACUUM_EXTEND, PIID_REMOTE_STATE, payload, 1)


def rotate(protocol, degrees: float) -> None:
    """Hold the turn and release it, the way the live view does."""
    rate = TURN_RATE_DPS if degrees > 0 else -TURN_RATE_DPS
    duration = abs(degrees) / TURN_RATE_DPS
    send_remote(protocol, rate)
    deadline = time.monotonic() + duration
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(REFRESH_SECONDS, remaining))
        if remaining > REFRESH_SECONDS:
            send_remote(protocol, rate)
    send_remote(protocol, 0)


def start_monitor(protocol, pin: str) -> tuple[bool, str]:
    """Open a monitor session - CAMERA_OPERATE on PropMonitor.

    Mirrors the plugin's startMonitor(). The access-code steps come first
    because the camera refuses to open without them; they are the same
    sequence the companion add-on performs before streaming.
    """
    session = str(uuid.uuid4())
    try:
        if pin:
            protocol.action(
                SIID_MONITOR, AIID_STREAM_CODE,
                [{"piid": PIID_STREAM_CODE_OPEN,
                  "value": json.dumps({"operType": "open", "session": session},
                                      separators=(",", ":"))}],
            )
            protocol.action(
                SIID_MONITOR, AIID_STREAM_CODE,
                [{"piid": PIID_VERIFY_ACCESS_CODE,
                  "value": json.dumps(
                      {"operType": "verify",
                       "oldcode": hashlib.sha256(pin.encode()).hexdigest(),
                       "session": session},
                      separators=(",", ":"))}],
            )
        result = protocol.action(
            SIID_MONITOR, AIID_CAMERA_OPERATE,
            [{"piid": PIID_MONITOR,
              "value": json.dumps(
                  {"operType": "monitor", "operation": "start", "session": session},
                  separators=(",", ":"))}],
        )
        return True, json.dumps(result)[:300]
    except Exception as err:  # noqa: BLE001
        return False, str(err)


def stop_monitor(protocol) -> None:
    try:
        protocol.action(
            SIID_MONITOR, AIID_CAMERA_OPERATE,
            [{"piid": PIID_MONITOR,
              "value": json.dumps({"operType": "monitor", "operation": "end"},
                                  separators=(",", ":"))}],
        )
    except Exception as err:  # noqa: BLE001
        print(f"  (stop monitor failed: {err})")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--did", required=True)
    ap.add_argument("--degrees", type=float, default=40)
    ap.add_argument("--country", default="eu")
    ap.add_argument("--outdir", default=".", help="where to write snapshots")
    args = ap.parse_args()

    root = Path(__file__).resolve().parents[2]
    env = load_env(root)
    username = env.get("DREAME_USERNAME")
    password = env.get("DREAME_PASSWORD")
    pin = env.get("VIDEO_STREAM_PIN", "")
    if not username or not password:
        print(f"DREAME_USERNAME / DREAME_PASSWORD not found in {root / '.env'}", file=sys.stderr)
        return 1

    outdir = Path(args.outdir)
    protocol_cls = _load_protocol()
    protocol = protocol_cls(
        username=username, password=password, country=args.country,
        prefer_cloud=True, account_type="dreame",
    )
    if not protocol.cloud.login():
        print("Login failed", file=sys.stderr)
        return 1
    protocol.cloud._did = args.did

    # connect() is what resolves the device's routing details; without it
    # every command 404s at the cloud's sendCommand endpoint. The integration
    # does the same thing before issuing anything.
    protocol.connect(lambda _message: None)
    if not protocol.connected:
        print("Connected to the cloud but not to the device", file=sys.stderr)
        return 1
    print("Connected to the device", file=sys.stderr)

    def snapshot(name: str) -> dict:
        print(f"\n--- snapshot: {name}")
        data = collect(protocol, args.did)
        (outdir / f"{name}.json").write_text(json.dumps(data, indent=2, sort_keys=True))
        return data

    idle = snapshot("1-idle")

    print(f"\n>>> ROTATING {args.degrees} degrees with NO monitor session.")
    print(">>> WATCH THE ROBOT: do the brushes/mop run?")
    time.sleep(3)
    rotate(protocol, args.degrees)
    time.sleep(2)
    plain = snapshot("2-after-plain-rotate")

    print("\n--- opening a monitor session")
    ok, detail = start_monitor(protocol, pin)
    print(f"    start_monitor ok={ok}: {detail}")
    time.sleep(3)
    monitoring = snapshot("3-monitor-open")

    print(f"\n>>> ROTATING {args.degrees} degrees WITH the monitor session open.")
    print(">>> WATCH THE ROBOT: do the brushes/mop run this time?")
    time.sleep(3)
    rotate(protocol, args.degrees)
    time.sleep(2)
    snapshot("4-after-monitor-rotate")

    print("\n--- closing the monitor session")
    stop_monitor(protocol)

    print("\n" + "=" * 60)
    print("IDLE vs MONITOR OPEN - what the session changes:")
    print("=" * 60)
    diff(idle, monitoring)

    print("\n" + "=" * 60)
    print("IDLE vs AFTER PLAIN ROTATE - what rotating alone changes:")
    print("=" * 60)
    diff(idle, plain)

    try:
        protocol.disconnect()
    except Exception:  # noqa: BLE001
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
