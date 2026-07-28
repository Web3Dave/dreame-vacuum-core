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
PIID_CAMERA_KEEP_ALIVE = 6


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


def rotate(protocol, degrees: float, hold: float | None = None) -> None:
    """Hold the turn and release it, the way the live view does.

    `hold` overrides the duration derived from the angle, so the same command
    can be compared at different hold times - the one variable left between a
    call that turns cleanly and one that starts vacuuming.
    """
    rate = TURN_RATE_DPS if degrees > 0 else -TURN_RATE_DPS
    duration = hold if hold is not None else abs(degrees) / TURN_RATE_DPS
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


def _signed_call(protocol, path, body):
    from dreame_sign import sign_params

    signed, _ = sign_params(body)
    return protocol.cloud._api_call(path, signed)


def _send_command_url(protocol) -> str:
    strings = protocol.cloud._strings
    host = f"-{protocol.cloud._host.split('.')[0]}" if protocol.cloud._host else ""
    return f"{strings[37]}{host}/{strings[27]}/{strings[38]}"


def camera_action(protocol, did: str, aiid: int, piid: int, value: dict):
    """Camera actions go over the signed command API, not plain MIoT."""
    req_id = int(time.time() * 1000) % 1000000
    body = {
        "did": did, "id": req_id,
        "data": {
            "did": did, "id": req_id, "method": "action",
            "params": {
                "did": did, "siid": SIID_MONITOR, "aiid": aiid,
                "in": [{"piid": piid, "value": json.dumps(value, separators=(",", ":"))}],
            },
        },
    }
    return _signed_call(protocol, _send_command_url(protocol), body)


def get_identity(protocol, did: str) -> tuple[str, str]:
    """The XP2P product id / device name the live view addresses the camera by."""
    resp = _signed_call(
        protocol, "dreame-third-video/tx/mgr/dev/getIdentity", {"did": did, "os": "ios"}
    )
    if not resp or not resp.get("success"):
        raise RuntimeError(f"getIdentity failed: {resp}")
    data = resp["data"]["data"]
    return data["productId"], data["deviceName"]


def start_live_view(protocol, did: str, pin: str) -> tuple[str, str]:
    """Everything the app does when its live view opens, short of the video.

    An earlier attempt sent operType/monitor with no token or channelId and
    the device accepted it without entering any different state - the app
    passes both, and addresses the camera by its XP2P identity.
    """
    session = uuid.uuid4().hex
    product_id, device_name = get_identity(protocol, did)
    print(f"    identity: {product_id}/{device_name}")

    r1 = camera_action(protocol, did, AIID_STREAM_CODE, PIID_STREAM_CODE_OPEN,
                       {"open": True, "session": session})
    print(f"    open:   {_summarise(r1)}")

    r2 = camera_action(protocol, did, AIID_STREAM_CODE, PIID_VERIFY_ACCESS_CODE,
                       {"oldcode": hashlib.sha256(pin.encode()).hexdigest(),
                        "lazymode": 0, "session": session})
    print(f"    verify: {_summarise(r2)}")

    r3 = camera_action(protocol, did, AIID_CAMERA_OPERATE, PIID_MONITOR,
                       {"token": "tx", "channelId": f"{product_id}/{device_name}",
                        "operType": "monitor", "operation": "start", "session": session})
    print(f"    start:  {_summarise(r3)}")
    return session, f"{product_id}/{device_name}"


def camera_keep_alive(protocol, did: str, session: str) -> None:
    """"Someone is still watching" - the device drops the session without it."""
    camera_action(protocol, did, AIID_CAMERA_OPERATE, PIID_CAMERA_KEEP_ALIVE,
                  {"operType": "keep_alive", "videoStatus": "opened", "session": session})


def stop_live_view(protocol, did: str, session: str) -> None:
    camera_action(protocol, did, AIID_CAMERA_OPERATE, PIID_MONITOR,
                  {"operType": "monitor", "operation": "end", "session": session})


def _summarise(resp) -> str:
    if not resp:
        return "no response"
    result = (resp.get("data") or {}).get("result") or {}
    return f"code={result.get('code')} out={result.get('out')}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--did", required=True)
    ap.add_argument("--degrees", type=float, default=40)
    ap.add_argument("--country", default="eu")
    ap.add_argument("--outdir", default=".", help="where to write snapshots")
    ap.add_argument("--hold", type=float, help="seconds to hold the turn, overriding the angle")
    ap.add_argument("--skip-monitor", action="store_true", help="just rotate once, no snapshots")
    ap.add_argument("--no-snapshots", action="store_true",
                    help="skip the property dumps - much faster, purely observational")
    ap.add_argument("--pause", action="store_true",
                    help="wait for Enter before each turn so you can be watching")
    ap.add_argument("--no-live-view", action="store_true",
                    help="skip opening the live view, so both turns are identical - "
                         "the control run for whether the session matters at all")
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

    if args.skip_monitor:
        hold = args.hold if args.hold is not None else abs(args.degrees) / TURN_RATE_DPS
        print(f"\n>>> Rotating: spdw {TURN_RATE_DPS} held for {hold:.2f}s. Watch the brushes.")
        time.sleep(3)
        rotate(protocol, args.degrees, hold=args.hold)
        print(">>> Done.")
        try:
            protocol.disconnect()
        except Exception:  # noqa: BLE001
            pass
        return 0

    def snapshot(name: str) -> dict:
        if args.no_snapshots:
            return {}
        print(f"\n--- snapshot: {name}")
        data = collect(protocol, args.did)
        (outdir / f"{name}.json").write_text(json.dumps(data, indent=2, sort_keys=True))
        return data

    def countdown(label: str) -> None:
        hold = args.hold if args.hold is not None else abs(args.degrees) / TURN_RATE_DPS
        print(f"\n>>> {label}")
        print(f">>> {args.degrees:.0f} degrees = {hold:.1f}s of turning. Watch the brushes.")
        if args.pause:
            input(">>> Press Enter when you are watching the robot... ")
        for n in (3, 2, 1):
            print(f"    {n}...", flush=True)
            time.sleep(1)
        print("    TURNING NOW", flush=True)

    idle = snapshot("1-idle")

    countdown("TURN 1 of 2: no live view session")
    rotate(protocol, args.degrees, hold=args.hold)
    time.sleep(2)
    plain = snapshot("2-after-plain-rotate")

    session = None
    if args.no_live_view:
        print("\n--- SKIPPING the live view session (control run)")
    else:
        print("\n--- opening a live view session (full app sequence)")
        session, channel = start_live_view(protocol, args.did, pin)
        time.sleep(3)
    monitoring = snapshot("3-monitor-open")

    if session:
        camera_keep_alive(protocol, args.did, session)
    countdown(
        "TURN 2 of 2: live view session OPEN" if session
        else "TURN 2 of 2: still no live view (control)"
    )
    rotate(protocol, args.degrees, hold=args.hold)
    if session:
        camera_keep_alive(protocol, args.did, session)
    time.sleep(2)
    snapshot("4-after-monitor-rotate")

    if session:
        print("\n--- closing the live view session")
        stop_live_view(protocol, args.did, session)

    print("\n" + "=" * 60)
    print("IDLE vs LIVE VIEW OPEN - what the session changes:")
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
