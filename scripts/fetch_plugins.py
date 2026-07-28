#!/usr/bin/env python3
"""Download Dreame's per-model plugin bundles.

The Dreamehome app ships almost no device logic in the APK - each vacuum's
screens, its siid/piid/aiid maps and its capability manifest are React Native
bundles fetched at runtime. This script resolves and downloads those bundles
so `extract_profiles.py` can turn them into the static profiles we ship.

Downloads land in an untracked directory; nothing here runs on a user's
Home Assistant.

Usage:
    export DREAME_USERNAME=... DREAME_PASSWORD=... DREAME_COUNTRY=eu
    python3 scripts/fetch_plugins.py --models-file scripts/models.txt
    python3 scripts/fetch_plugins.py --from-account      # models you own
    python3 scripts/fetch_plugins.py --models dreame.vacuum.r2579h

Not every model has a resource package - older hardware often returns nothing.
That is reported, not treated as failure.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import zipfile
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dreame_api import DreameApi  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = REPO_ROOT / "plugins"


def md5_file(path: Path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def md5_inner_bundle(path: Path) -> str | None:
    """md5 of the largest .bundle inside a zip.

    The two md5 fields the API returns mean different things: resource
    packages advertise `resPackageZipMd5` (the zip itself), while common
    plugins advertise `md5` of the *bundle file inside* the zip. Verified
    against real downloads - checking the zip for the latter always fails.
    """
    try:
        with zipfile.ZipFile(path) as zf:
            names = [n for n in zf.namelist() if n.endswith((".bundle", ".jsbundle"))]
            if not names:
                return None
            biggest = max(names, key=lambda n: zf.getinfo(n).file_size)
            h = hashlib.md5()
            with zf.open(biggest) as fh:
                for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                    h.update(chunk)
            return h.hexdigest()
    except (zipfile.BadZipFile, OSError):
        return None


def verify(path: Path, expected: str | None, mode: str) -> tuple[bool, str]:
    if not expected:
        return True, "no md5 published"
    actual = md5_file(path) if mode == "zip" else md5_inner_bundle(path)
    if actual is None:
        return False, "could not compute md5"
    if actual != expected:
        return False, f"md5 mismatch ({mode})"
    return True, "verified"


def download(url: str, dest: Path, expected_md5: str | None = None, md5_mode: str = "zip") -> tuple[bool, str]:
    """Returns (ok, note). Skips the download when a valid copy already exists."""
    if dest.exists():
        ok, _ = verify(dest, expected_md5, md5_mode)
        if ok:
            return True, "cached"
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        with requests.get(url, stream=True, timeout=180) as r:
            r.raise_for_status()
            tmp = dest.with_suffix(dest.suffix + ".part")
            with open(tmp, "wb") as fh:
                for chunk in r.iter_content(1024 * 256):
                    fh.write(chunk)
            tmp.replace(dest)
    except Exception as err:  # noqa: BLE001 - report and continue to next model
        return False, f"download failed: {err}"

    ok, note = verify(dest, expected_md5, md5_mode)
    return ok, ("downloaded" if ok else note)


def load_models(args, api: DreameApi) -> list[str]:
    models: list[str] = []
    if args.models:
        models.extend(args.models)
    if args.models_file:
        path = Path(args.models_file)
        if not path.exists():
            raise SystemExit(f"models file not found: {path}")
        for line in path.read_text().splitlines():
            line = line.split("#", 1)[0].strip()
            if line:
                models.append(line)
    if args.from_account:
        for dev in api.get_devices():
            model = dev.get("model")
            if model:
                models.append(model)
                print(f"  account device: {model} ({dev.get('customName') or 'unnamed'})")
    # de-dupe, preserve order
    seen, ordered = set(), []
    for m in models:
        if m not in seen:
            seen.add(m)
            ordered.append(m)
    return ordered


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--models", nargs="*", help="explicit model ids")
    ap.add_argument("--models-file", help="file with one model id per line (# comments ok)")
    ap.add_argument("--from-account", action="store_true", help="include models on the logged-in account")
    ap.add_argument("--out", default=str(DEFAULT_OUT), help=f"download dir (default: {DEFAULT_OUT})")
    ap.add_argument("--app-ver", type=int, default=160)
    ap.add_argument("--os", dest="os_id", type=int, default=1, choices=[0, 1], help="0=iOS 1=Android")
    ap.add_argument("--delay", type=float, default=0.5, help="seconds between API calls")
    args = ap.parse_args()

    username = os.environ.get("DREAME_USERNAME")
    password = os.environ.get("DREAME_PASSWORD")
    country = os.environ.get("DREAME_COUNTRY", "eu")
    if not username or not password:
        raise SystemExit("Set DREAME_USERNAME and DREAME_PASSWORD in the environment")

    out_dir = Path(args.out)
    print(f"[+] logging in ({country})...")
    api = DreameApi(username, password, country)

    models = load_models(args, api)
    if not models:
        raise SystemExit("No models specified (use --models, --models-file or --from-account)")
    print(f"[+] {len(models)} model(s) to resolve\n")

    index_path = out_dir / "index.json"
    index = {}
    if index_path.exists():
        try:
            index = json.loads(index_path.read_text())
        except Exception:  # noqa: BLE001 - a corrupt index shouldn't block a re-fetch
            index = {}

    stats = {"resource": 0, "common": 0, "no_data": 0, "errors": 0}

    for model in models:
        try:
            info = api.get_app_plugin(model, args.app_ver, args.os_id)
        except Exception as err:  # noqa: BLE001
            print(f"{model:<32} ERROR {err}")
            stats["errors"] += 1
            continue
        finally:
            time.sleep(args.delay)

        if not info:
            print(f"{model:<32} no plugin data (older model / not published)")
            stats["no_data"] += 1
            index[model] = {"fetched_at": int(time.time()), "available": False}
            continue

        entry = {
            "fetched_at": int(time.time()),
            "available": True,
            "app_ver": args.app_ver,
            "os": args.os_id,
            "plugin_version": info.get("version"),
            "res_package_version": info.get("resPackageVersion"),
            "files": {},
        }

        # model-specific resource package - carries config.json (capabilities)
        res_url = info.get("resPackageUrl")
        if res_url:
            dest = out_dir / "resource" / f"{model}.zip"
            ok, note = download(res_url, dest, info.get("resPackageZipMd5"), md5_mode="zip")
            entry["files"]["resource"] = {
                "url": res_url,
                "md5": info.get("resPackageZipMd5"),
                "path": str(dest.relative_to(out_dir)),
                "ok": ok,
                "note": note,
            }
            if ok:
                stats["resource"] += 1
            else:
                stats["errors"] += 1

        # shared bundle - the JS with the service/prop/action maps
        common_url = info.get("newUrl") or info.get("url")
        if common_url:
            name = common_url.rsplit("/", 1)[-1]
            dest = out_dir / "common" / name
            ok, note = download(common_url, dest, info.get("md5"), md5_mode="inner_bundle")
            entry["files"]["common"] = {
                "url": common_url,
                "md5": info.get("md5"),
                "path": str(dest.relative_to(out_dir)),
                "ok": ok,
                "note": note,
            }
            if ok:
                stats["common"] += 1
            else:
                stats["errors"] += 1

        index[model] = entry
        bits = [k for k, v in entry["files"].items() if v.get("ok")]
        notes = ", ".join(f"{k}:{v['note']}" for k, v in entry["files"].items())
        print(f"{model:<32} {'+'.join(bits) or 'none':<18} {notes}")

    out_dir.mkdir(parents=True, exist_ok=True)
    index_path.write_text(json.dumps(index, indent=2, sort_keys=True) + "\n")

    print(
        f"\n[+] resource pkgs: {stats['resource']}  common: {stats['common']}  "
        f"no data: {stats['no_data']}  errors: {stats['errors']}"
    )
    print(f"[+] index: {index_path}")
    print("[+] next: python3 scripts/extract_profiles.py")
    return 1 if stats["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
