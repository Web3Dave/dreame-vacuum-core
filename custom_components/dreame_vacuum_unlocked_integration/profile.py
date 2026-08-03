"""Device profiles: what a service *is* vs. what a model *has*.

Three separate concerns, deliberately not collapsed into one - conflating them
is how you end up creating a "fluffing roller life" sensor on a vacuum with no
fluffing roller:

  1. VOCABULARY  (_services.json, generated, merged across models)
     "siid 32 is FluffingRoller; piid 1 is its remaining life."
     A dictionary. Knowing a word exists says nothing about this device.

  2. FEATURE FLAGS  (<model>.json, generated, per model)
     Dreame's own published manifest - ~125 flags like `supportDrySpeed`.
     Good for gating optional UI/behaviour and version thresholds. It is NOT
     a service inventory: there is no flag that says "has a fluffing roller".

  3. PRESENCE  (runtime probe, not in any file)
     Whether *this unit* actually implements a property. Unsupported reads
     come back `code: -1`; supported ones return `code: 0` plus a value.
     This is the only trustworthy source, so it stays a runtime check.

Both generated files ship with the integration, so setup never depends on
Dreame's servers being reachable. Regenerate them with scripts/.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

from .const import PROFILES_DIR, SERVICES_PROFILE

_LOGGER = logging.getLogger(__name__)

_PROFILE_ROOT = Path(__file__).parent / PROFILES_DIR


@dataclass
class ServiceDef:
    """One MIoT service, from the generated vocabulary."""

    name: str
    siid: int
    aiid: dict[str, int] = field(default_factory=dict)
    piid: dict[str, int] = field(default_factory=dict)

    def action(self, name: str) -> int | None:
        return self.aiid.get(name)

    def prop(self, name: str) -> int | None:
        return self.piid.get(name)


@dataclass
class DeviceProfile:
    """Vocabulary + feature flags for one model.

    `capabilities` is whatever Dreame published for the model, verbatim.
    `has_service`/`prop_id` answer vocabulary questions only - use the
    coordinator's probe results to decide what actually exists.
    """

    model: str
    services: dict[str, ServiceDef]
    capabilities: dict
    source: dict = field(default_factory=dict)

    _by_siid: dict[int, ServiceDef] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        self._by_siid = {s.siid: s for s in self.services.values()}

    # -- vocabulary -------------------------------------------------------
    def service(self, name: str) -> ServiceDef | None:
        return self.services.get(name)

    def service_by_siid(self, siid: int) -> ServiceDef | None:
        return self._by_siid.get(siid)

    def has_service(self, name: str) -> bool:
        """Is this service in the vocabulary? NOT 'does this device have it'."""
        return name in self.services

    def prop_id(self, service: str, prop: str) -> tuple[int, int] | None:
        svc = self.services.get(service)
        if svc is None:
            return None
        piid = svc.prop(prop)
        return None if piid is None else (svc.siid, piid)

    def action_id(self, service: str, action: str) -> tuple[int, int] | None:
        svc = self.services.get(service)
        if svc is None:
            return None
        aiid = svc.action(action)
        return None if aiid is None else (svc.siid, aiid)

    # -- feature flags ----------------------------------------------------
    def flag(self, name: str, default=None):
        return self.capabilities.get(name, default)

    def supports(self, name: str) -> bool:
        """Truthiness of a published flag.

        Absent means "this model's manifest doesn't mention it", which is
        different from False - the two models we've profiled share only 78 of
        their 125/97 flags, so absence is common and not an error.
        """
        return bool(self.capabilities.get(name))

    def version_flag(self, name: str) -> int | None:
        """Some flags are firmware gates (e.g. autoCarpetCleanVersion: 1300)."""
        val = self.capabilities.get(name)
        try:
            return int(val)
        except (TypeError, ValueError):
            return None

    @property
    def profiled(self) -> bool:
        """False when we shipped no manifest for this model."""
        return bool(self.capabilities)


def _load_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return None
    except (json.JSONDecodeError, OSError) as err:
        _LOGGER.error("Could not read profile %s: %s", path.name, err)
        return None


def load_services() -> dict[str, ServiceDef]:
    """Load the generated service vocabulary."""
    raw = _load_json(_PROFILE_ROOT / SERVICES_PROFILE)
    if not raw:
        _LOGGER.warning(
            "No %s shipped - falling back to an empty vocabulary. "
            "Regenerate with scripts/extract_profiles.py",
            SERVICES_PROFILE,
        )
        return {}

    services: dict[str, ServiceDef] = {}
    for name, entry in (raw.get("services") or {}).items():
        try:
            siid = int(entry["siid"])
        except (KeyError, TypeError, ValueError):
            continue
        # The generator emits literal piids and symbolic (Props.PropX) ones
        # separately; downstream code only cares about the merged view.
        piid = {**(entry.get("piid") or {}), **(entry.get("piid_symbolic") or {})}
        services[name] = ServiceDef(
            name=entry.get("service", name),
            siid=siid,
            aiid={k: int(v) for k, v in (entry.get("aiid") or {}).items()},
            piid={k: int(v) for k, v in piid.items()},
        )
    return services


def load_profile(model: str) -> DeviceProfile:
    """Vocabulary + this model's flags.

    An unprofiled model is not an error: it gets the shared vocabulary and no
    flags, and the coordinator's probe then determines what it can actually
    do. That is how an unknown vacuum degrades gracefully instead of failing
    to set up.
    """
    services = load_services()
    raw = _load_json(_PROFILE_ROOT / f"{model}.json")

    if raw is None:
        _LOGGER.info(
            "No capability manifest shipped for %s - relying on runtime probing. "
            "Add it to scripts/models.txt and regenerate to improve support.",
            model,
        )
        return DeviceProfile(model=model, services=services, capabilities={})

    return DeviceProfile(
        model=model,
        services=services,
        capabilities=raw.get("capabilities") or {},
        source=raw.get("_generator") or {},
    )


def available_models() -> list[str]:
    """Models we ship a manifest for."""
    if not _PROFILE_ROOT.is_dir():
        return []
    return sorted(p.stem for p in _PROFILE_ROOT.glob("*.json") if p.name != SERVICES_PROFILE)
