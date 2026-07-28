#!/usr/bin/env python3
"""Turn downloaded plugin bundles into the static profiles we ship.

Produces two kinds of output under custom_components/<domain>/profiles/:

  _services.json          service map (siid -> model, aiid names) from the
                          shared JS bundle - the same data the old integration
                          maintained by hand as a ~370-entry table
  <model>.json            per-model capability manifest (the ~125 flags Dreame
                          publishes for each vacuum)

JSON rather than generated Python on purpose: these get regenerated whenever
Dreame ships an update, and a reviewable diff is the whole point.

Usage:
    python3 scripts/extract_profiles.py
    python3 scripts/extract_profiles.py --print-services
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PLUGINS = REPO_ROOT / "plugins"
DEFAULT_DOMAIN_DIR = REPO_ROOT / "custom_components" / "dreame_camera_capture"

GENERATOR_VERSION = 1


def _read_bundle(zip_path: Path) -> str | None:
    """Pull the RN JS bundle out of a common-plugin zip."""
    with zipfile.ZipFile(zip_path) as zf:
        candidates = [n for n in zf.namelist() if n.endswith((".bundle", ".jsbundle"))]
        if not candidates:
            return None
        # the main bundle is the biggest one
        biggest = max(candidates, key=lambda n: zf.getinfo(n).file_size)
        return zf.read(biggest).decode("utf-8", errors="replace")


def _read_model_config(zip_path: Path) -> dict | None:
    """Pull config.json (the capability manifest) out of a resource package."""
    with zipfile.ZipFile(zip_path) as zf:
        for name in zf.namelist():
            if name.endswith("config.json") and name.count("/") <= 1:
                try:
                    return json.loads(zf.read(name).decode("utf-8", errors="replace"))
                except json.JSONDecodeError:
                    return None
    return None


def extract_services(js: str) -> dict:
    """Recover the service map from the bundle's model classes.

    Each service is a class that sets `_this.SIID = <n>` and then fills in
    `_this.PIID` / `_this.AIID` maps. We capture the literal AIID maps (they're
    inline) and the PIID entries that are assigned literally; the rest are
    keyed off `Props.PropXxx` names, which we record as symbolic so a human can
    see what exists without us guessing at numbers.
    """
    services: dict[str, dict] = {}

    siid_hits = list(re.finditer(r"_this\.SIID\s*=\s*(\d+)\s*;", js))
    for idx, m in enumerate(siid_hits):
        siid = int(m.group(1))
        # Bound this class to the next SIID assignment - a fixed-size window
        # bleeds prop definitions in from the following service.
        next_start = siid_hits[idx + 1].start() if idx + 1 < len(siid_hits) else len(js)
        # The class name is unreliable from the enclosing `function X()` -
        # minifiers rename it. `_classCallCheck(this, Name)` just above the
        # SIID assignment keeps the original name.
        head = js[max(0, m.start() - 600) : m.start()]
        name_match = None
        for pat in (r"_classCallCheck\d*\.default\)\(this,\s*(\w+)\)", r"function (\w+)\(\)"):
            found = re.findall(pat, head)
            if found:
                name_match = found[-1]
                break
        name = name_match or f"siid_{siid}"

        # scan forward from the SIID assignment for this class's maps
        body = js[m.end() : next_start]

        entry: dict = {"service": name, "siid": siid, "aiid": {}, "piid": {}, "piid_symbolic": {}}

        aiid_block = re.search(r"_this\.AIID\s*=\s*\{(.*?)\}", body, re.S)
        if aiid_block:
            for k, v in re.findall(r"(\w+)\s*:\s*(\d+)", aiid_block.group(1)):
                entry["aiid"][k] = int(v)

        piid_block = re.search(r"_this\.PIID\s*=\s*\{(.*?)\}", body, re.S)
        if piid_block:
            for k, v in re.findall(r"(\w+)\s*:\s*(\d+)", piid_block.group(1)):
                entry["piid"][k] = int(v)

        # _this.PIID[Props.PropFoo] = 12;
        for prop, val in re.findall(r"_this\.PIID\[_?\w*\.?Props\.(\w+)\]\s*=\s*(\d+)", body):
            entry["piid_symbolic"][prop] = int(val)

        if entry["aiid"] or entry["piid"] or entry["piid_symbolic"]:
            # keep the richest definition if a class appears more than once
            prev = services.get(name)
            if prev is None or (
                len(entry["piid_symbolic"]) + len(entry["piid"]) + len(entry["aiid"])
                > len(prev["piid_symbolic"]) + len(prev["piid"]) + len(prev["aiid"])
            ):
                services[name] = entry

    return services


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--plugins", default=str(DEFAULT_PLUGINS))
    ap.add_argument("--out", default=str(DEFAULT_DOMAIN_DIR / "profiles"))
    ap.add_argument("--print-services", action="store_true", help="dump the service map and exit")
    args = ap.parse_args()

    plugins_dir = Path(args.plugins)
    out_dir = Path(args.out)
    index_path = plugins_dir / "index.json"
    if not index_path.exists():
        raise SystemExit(f"No index at {index_path} - run scripts/fetch_plugins.py first")

    index = json.loads(index_path.read_text())
    out_dir.mkdir(parents=True, exist_ok=True)

    # ---- services (shared across models) -------------------------------
    common_zips = sorted((plugins_dir / "common").glob("*.zip")) if (plugins_dir / "common").exists() else []
    # Prefer the shared `dreame.vacuum.common` bundle - some models ship their
    # own bundle instead, which only describes that one device.
    shared = [p for p in common_zips if "vacuum.common" in p.name]
    if shared:
        common_zips = shared
    if common_zips:
        # biggest = most complete service coverage
        newest = max(common_zips, key=lambda p: p.stat().st_size)
        print(f"[+] services from {newest.name}")
        js = _read_bundle(newest)
        if js:
            services = extract_services(js)
            if args.print_services:
                for name, e in sorted(services.items(), key=lambda kv: kv[1]["siid"]):
                    print(f"  siid {e['siid']:<6} {name:<20} aiid={e['aiid']} piids={len(e['piid'])+len(e['piid_symbolic'])}")
                return 0
            payload = {
                "_generator": {"version": GENERATOR_VERSION, "source_bundle": newest.name},
                "services": services,
            }
            (out_dir / "_services.json").write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
            print(f"    -> _services.json ({len(services)} services)")
        else:
            print("    !! no JS bundle inside zip")
    else:
        print("[!] no common bundles downloaded - service map skipped")

    # ---- per-model capability manifests --------------------------------
    res_dir = plugins_dir / "resource"
    written = 0
    if res_dir.exists():
        for zip_path in sorted(res_dir.glob("*.zip")):
            model = zip_path.stem
            config = _read_model_config(zip_path)
            if config is None:
                print(f"{model:<32} no config.json in resource package")
                continue
            meta = index.get(model, {})
            files = meta.get("files", {}).get("resource", {})
            payload = {
                "_generator": {
                    "version": GENERATOR_VERSION,
                    "source_url": files.get("url"),
                    "source_md5": files.get("md5"),
                    "res_package_version": meta.get("res_package_version"),
                    "fetched_at": meta.get("fetched_at"),
                },
                "model": model,
                "capabilities": config,
            }
            (out_dir / f"{model}.json").write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
            print(f"{model:<32} {len(config)} capability flags")
            written += 1

    print(f"\n[+] wrote {written} model profile(s) to {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
