# Profile generation scripts

Dev-time tooling. **None of this runs on a user's Home Assistant** — it
produces static JSON that ships with the integration, so installs never depend
on Dreame's servers being reachable.

## Why

The Dreamehome app ships almost no device logic in its APK. Each vacuum's
screens, its `siid`/`piid`/`aiid` maps and its capability manifest are React
Native bundles the app downloads at runtime. That's why searching a decompiled
APK for device properties finds nothing.

Those bundles are the authoritative source for:

- **what each service is** — `siid → service`, plus action ids
- **what each model supports** — ~125 explicit capability flags per model

The alternative (what the older integration does) is inferring capabilities at
runtime by probing whether properties respond, plus a hand-maintained mapping
table. That works, but it's guesswork where this is a published fact.

## Usage

```bash
# 1. auth (a normal Dreame account; the discovery endpoint requires a token)
export DREAME_USERNAME='you@example.com'
export DREAME_PASSWORD='...'
export DREAME_COUNTRY='eu'

# 2. auth code lives in the companion add-on repo - point at it
export DREAME_LIB_PATH=../dreame-vacuum-companion/dreame_vacuum_companion

# 3. download bundles into ./plugins/ (untracked)
python3 scripts/fetch_plugins.py --models-file scripts/models.txt --from-account

# 4. bundles -> tracked profiles
python3 scripts/extract_profiles.py

# inspect the service map without writing anything
python3 scripts/extract_profiles.py --print-services
```

Add models to `models.txt` (one per line) to widen coverage.

## How discovery works

```
GET /dreame-product/upgrades/appplugin?model=<model>&appVer=160&os=1
```

Authenticated (returns `401 Missing token` otherwise). Takes **any** model id,
not just ones on your account, so profiles can be built for hardware you don't
own. Returns:

| Field | Meaning |
|---|---|
| `url` / `newUrl` | shared JS bundle — service/prop/action maps |
| `md5` | md5 of the **bundle file inside** the zip |
| `resPackageUrl` | model resource package — carries `config.json` |
| `resPackageZipMd5` | md5 of the **zip itself** |

> The two md5 fields mean different things. Verifying the common plugin's
> `md5` against the zip always fails — it describes the inner bundle. Both are
> checked correctly by `fetch_plugins.py`; this bit was found the hard way.

`os=0` is iOS, `os=1` Android. It only changes the common bundle URL — the
model resource package is byte-identical for both.

## Output

```
custom_components/dreame_vacuum_core/profiles/
  _services.json              siid -> service, aiid names, piid maps
  dreame.vacuum.r2579h.json   125 capability flags + provenance
  dreame.vacuum.r2338a.json    97 capability flags + provenance
```

Every profile records where it came from (source URL, md5, package version,
fetch time) so a future oddity can be traced to a specific bundle.

JSON rather than generated Python deliberately: these get regenerated whenever
Dreame ships an update, and a reviewable diff is the entire point.

## Known limitations

- **Coverage is partial.** Older models return no plugin data at all
  (`p2008`, `xiaomi.vacuum.c102gl`, `mova.vacuum.p2157` all came back empty).
  Reported, not treated as an error.
- **Some models ship their own bundle** instead of `dreame.vacuum.common`
  (e.g. `r2205`). `extract_profiles.py` prefers the shared one for the service
  map.
- **The service map is only as wide as the bundles you download.** Validated
  against the older integration's 370 hand-written mappings: 93% of its siids
  are covered, with no spurious entries. The gap (siids 17, 32–38, 40) is
  hardware modules absent from this bundle.
  **Next improvement: merge service maps across every downloaded common
  bundle** rather than picking one — that should close most of the remaining 7%.
- Extraction is regex over minified JS. It's validated against known-good
  values (`Monitor` and `Audio` were verified by hand against a real device),
  but a Dreame refactor could silently change the shape. The `--print-services`
  output is the quickest sanity check after any regeneration.

## dump_properties.py

Snapshots every property in the generated vocabulary and diffs two snapshots.
Written to find what the device changes when the app's live view is open,
since driving from the live view turns without running the brushes while
driving it any other way vacuums as it goes.

```
python scripts/dump_properties.py --out before.json     # idle
python scripts/dump_properties.py --out during.json     # stream running
python scripts/dump_properties.py --diff before.json during.json
```

Snapshots contain device state, not credentials, but they do identify the
device - keep them out of the repo.

## rotate_experiment.py

Runs the same 40 degree turn twice - once plain, once with a monitor session
open - snapshotting properties around each, to establish whether an open
camera session is what stops the robot vacuuming as it turns.

```
# fast, observational: two 180 degree turns, waits for you before each
python scripts/rotate_experiment.py --did <device id> \
    --degrees 180 --no-snapshots --pause

# full version, with the 242-property dumps and diffs
python scripts/rotate_experiment.py --did <device id>
```

Needs the companion package on PYTHONPATH for the camera calls:

```
PYTHONPATH=../dreame-vacuum-companion/dreame_vacuum_companion python scripts/...
```

Reads credentials from `.env` in the repo root (DREAME_USERNAME,
DREAME_PASSWORD, VIDEO_STREAM_PIN). Put the robot in open space first.
