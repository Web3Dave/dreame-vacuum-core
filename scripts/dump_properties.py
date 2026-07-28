"""Read every property in the generated vocabulary and dump it as JSON.

Dev-time only. Built to answer a specific question: what does the device
change when the app's live view is open? Driving the robot from the live view
turns without running the brushes; driving it any other way vacuums as it
goes. Something in the device's state must differ, and the cheapest way to
find it is to snapshot everything twice and diff.

    # 1. robot idle, nothing streaming
    python scripts/dump_properties.py --out before.json

    # 2. start the stream (HA's Stream switch, or open the app's live view)
    python scripts/dump_properties.py --out during.json

    # 3. what changed
    python scripts/dump_properties.py --diff before.json during.json

Reads are batched and failures are recorded rather than raised: a device that
does not implement a property answers with a non-zero code, and knowing which
ones those are is part of the point.
"""
from __future__ import annotations

import argparse
import getpass
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dreame_api import _load_protocol  # noqa: E402

# Read the generated vocabulary straight from JSON rather than importing
# profile.py: that module is part of the integration package and pulls in
# homeassistant, which isn't available outside HA and isn't needed to read a
# dict of siids and piids.
SERVICES_JSON = (
    Path(__file__).resolve().parents[1]
    / "custom_components" / "dreame_vacuum_core" / "profiles" / "_services.json"
)

BATCH = 15

# Map frames are tens of kilobytes of base64 and change every frame, so they
# would swamp a diff without saying anything.
SKIP = {("CleanMap", "PropMapdata"), ("CleanMap", "PropOldIMap")}


def collect(protocol, did: str) -> dict:
    services = json.loads(SERVICES_JSON.read_text())["services"]

    wanted: list[tuple[str, str, int, int]] = []
    for name, service in services.items():
        siid = service["siid"]
        for prop, piid in (service.get("piid_symbolic") or {}).items():
            if (name, prop) in SKIP:
                continue
            wanted.append((name, prop, siid, piid))

    out: dict[str, dict] = {}
    for i in range(0, len(wanted), BATCH):
        batch = wanted[i : i + BATCH]
        try:
            result = protocol.get_properties(
                [{"did": did, "siid": s, "piid": p} for _, _, s, p in batch]
            )
        except Exception as err:  # noqa: BLE001 - a dead batch shouldn't end the run
            print(f"  batch {i // BATCH} failed: {err}", file=sys.stderr)
            continue
        if not isinstance(result, list):
            continue
        for (name, prop, siid, piid), item in zip(batch, result):
            if not isinstance(item, dict):
                continue
            code = item.get("code")
            value = item.get("value")
            if isinstance(value, str) and len(value) > 300:
                value = f"<{len(value)} chars>"
            out[f"{name}.{prop}"] = {"siid": siid, "piid": piid, "code": code, "value": value}
        print(f"  read {min(i + BATCH, len(wanted))}/{len(wanted)}", file=sys.stderr)
    return out


def diff(before: dict, after: dict) -> None:
    keys = sorted(set(before) | set(after))
    changed = 0
    for key in keys:
        b, a = before.get(key), after.get(key)
        bv = None if b is None else (b["value"] if b.get("code") == 0 else f"code {b['code']}")
        av = None if a is None else (a["value"] if a.get("code") == 0 else f"code {a['code']}")
        if bv == av:
            continue
        changed += 1
        ids = (a or b).get("siid"), (a or b).get("piid")
        print(f"{key}  (siid {ids[0]} piid {ids[1]})")
        print(f"    before: {bv!r}")
        print(f"    after:  {av!r}")
    print(f"\n{changed} changed of {len(keys)} properties")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", help="write a snapshot here")
    ap.add_argument("--diff", nargs=2, metavar=("BEFORE", "AFTER"), help="compare two snapshots")
    ap.add_argument("--did", help="device id")
    ap.add_argument("--username")
    ap.add_argument("--country", default="eu")
    args = ap.parse_args()

    if args.diff:
        diff(json.loads(Path(args.diff[0]).read_text()), json.loads(Path(args.diff[1]).read_text()))
        return 0

    if not args.out:
        ap.error("--out or --diff is required")

    username = args.username or input("Dreame username: ")
    password = getpass.getpass("Dreame password: ")
    did = args.did or input("Device id: ")

    protocol_cls = _load_protocol()
    protocol = protocol_cls(
        username=username, password=password, country=args.country,
        prefer_cloud=True, account_type="dreame",
    )
    if not protocol.cloud.login():
        print("Login failed", file=sys.stderr)
        return 1
    protocol.cloud._did = did

    print("Reading properties...", file=sys.stderr)
    snapshot = collect(protocol, did)
    Path(args.out).write_text(json.dumps(snapshot, indent=2, sort_keys=True))
    print(f"Wrote {len(snapshot)} properties to {args.out}", file=sys.stderr)

    try:
        protocol.disconnect()
    except Exception:  # noqa: BLE001
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
