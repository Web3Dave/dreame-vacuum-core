"""What does opening a live view change on the device? Nothing else happens.

Snapshots every property, opens a live view session, waits, snapshots again
and diffs. The robot is never moved.

The point is to find something that can be set directly - if some property
flips when the session opens, setting it ourselves might give a silent
rotation without holding a video session open at all.

    python scripts/live_view_diff.py --did 2089953038 --delay 5

Credentials come from a .env in the repo root (DREAME_USERNAME,
DREAME_PASSWORD, VIDEO_STREAM_PIN).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dreame_api import _load_protocol  # noqa: E402
from dump_properties import collect, diff  # noqa: E402
from rotate_experiment import (  # noqa: E402
    camera_keep_alive,
    load_env,
    start_live_view,
    stop_live_view,
)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--did", required=True)
    ap.add_argument("--delay", type=float, default=5,
                    help="seconds to wait after the session opens before the second snapshot")
    ap.add_argument("--country", default="eu")
    ap.add_argument("--outdir", default=".")
    args = ap.parse_args()

    root = Path(__file__).resolve().parents[2]
    env = load_env(root)
    username, password = env.get("DREAME_USERNAME"), env.get("DREAME_PASSWORD")
    pin = env.get("VIDEO_STREAM_PIN", "")
    if not username or not password:
        print(f"DREAME_USERNAME / DREAME_PASSWORD not found in {root / '.env'}", file=sys.stderr)
        return 1

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    protocol_cls = _load_protocol()
    protocol = protocol_cls(
        username=username, password=password, country=args.country,
        prefer_cloud=True, account_type="dreame",
    )
    if not protocol.cloud.login():
        print("Login failed", file=sys.stderr)
        return 1
    protocol.cloud._did = args.did
    protocol.connect(lambda _message: None)
    if not protocol.connected:
        print("Connected to the cloud but not to the device", file=sys.stderr)
        return 1
    print("Connected to the device", file=sys.stderr)

    def snapshot(name: str) -> dict:
        print(f"\n--- snapshot: {name}", file=sys.stderr)
        data = collect(protocol, args.did)
        (outdir / f"{name}.json").write_text(json.dumps(data, indent=2, sort_keys=True))
        return data

    before = snapshot("before-live-view")

    print("\n--- opening a live view session")
    session, channel = start_live_view(protocol, args.did, pin)
    print(f"--- holding it open for {args.delay}s")
    time.sleep(args.delay)
    camera_keep_alive(protocol, args.did, session)

    # Taken while the session is still open, and kept alive part-way through,
    # because the snapshot itself takes a while and the device drops the
    # session without a keep-alive.
    after = snapshot("after-live-view")
    camera_keep_alive(protocol, args.did, session)

    print("\n--- closing the live view session")
    stop_live_view(protocol, args.did, session)
    time.sleep(2)
    closed = snapshot("after-close")

    print("\n" + "=" * 60)
    print(f"BEFORE vs DURING live view ({args.delay}s in)")
    print("=" * 60)
    diff(before, after)

    print("\n" + "=" * 60)
    print("DURING vs AFTER CLOSING - which of those changes revert")
    print("=" * 60)
    diff(after, closed)

    try:
        protocol.disconnect()
    except Exception:  # noqa: BLE001
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
