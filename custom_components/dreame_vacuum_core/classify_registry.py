"""Native classification entities, fed by a webhook instead of MQTT.

The companion add-on used to broadcast classification results over MQTT
discovery, which meant a broker, credentials, and Home Assistant's discovery
all had to line up before a single entity appeared - and any one of those
being wrong failed with no visible error. This replaces that path entirely:
a webhook is registered once with Home Assistant's own webhook component,
its URL is pushed to the add-on on every startup (see
coordinator.async_register_with_companion), and the add-on POSTs results to
it directly. Nothing for a person to install or configure.

Entities are created lazily, the first time a given classifier_id is seen -
there is no way to know a classifier's class list ahead of time, since that
is authored entirely in the add-on's own UI.
"""
from __future__ import annotations

import logging

from aiohttp import web
from homeassistant.components import webhook
from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.network import NoURLAvailableError
from homeassistant.util import dt as dt_util

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

_DATA_WEBHOOK_ID = f"{DOMAIN}_classify_webhook_id"
_DATA_REGISTRY = f"{DOMAIN}_classify_registry"


def _slug(value: str) -> str:
    """A class name as an entity unique_id suffix - stable across a
    classifier being renamed, since it is derived from the class name, not
    the classifier's own name."""
    cleaned = "".join(c if (c.isalnum() or c in "-_") else "_" for c in value.strip())
    return cleaned.strip("_").lower()[:48] or "class"


def _device_info(classifier_id: str, name: str) -> dict:
    return {
        "identifiers": {(DOMAIN, f"classify_{classifier_id}")},
        "name": name,
        "model": "Snapshot classification",
        "manufacturer": "Dreame Vacuum Companion",
    }


class ClassificationStateSensor(SensorEntity):
    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_icon = "mdi:tag-text-outline"

    def __init__(self, classifier_id: str, name: str) -> None:
        self._attr_unique_id = f"dreame_classify_{classifier_id}"
        self._attr_device_info = _device_info(classifier_id, name)
        self._attr_name = "State"
        self._attr_native_value: str | None = None
        self._attr_extra_state_attributes: dict = {}

    def update_result(self, label: str, score: float, tag_id: str, filename: str, ran_at: int) -> None:
        self._attr_native_value = label
        self._attr_extra_state_attributes = {
            "score": score, "tag": tag_id, "filename": filename, "ran_at": ran_at,
        }
        if self.hass is not None:
            self.async_write_ha_state()


class ClassificationUpdatedSensor(SensorEntity):
    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, classifier_id: str, name: str) -> None:
        self._attr_unique_id = f"dreame_classify_{classifier_id}_last_updated"
        self._attr_device_info = _device_info(classifier_id, name)
        self._attr_name = "Last updated"
        self._attr_native_value = None

    def update_result(self, ran_at: int) -> None:
        self._attr_native_value = dt_util.utc_from_timestamp(ran_at)
        if self.hass is not None:
            self.async_write_ha_state()


class ClassificationClassBinarySensor(BinarySensorEntity):
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, classifier_id: str, name: str, class_name: str) -> None:
        self._attr_unique_id = f"dreame_classify_{classifier_id}_{_slug(class_name)}"
        self._attr_device_info = _device_info(classifier_id, name)
        self._attr_name = class_name
        self._attr_is_on = False

    def update_state(self, is_on: bool) -> None:
        self._attr_is_on = is_on
        if self.hass is not None:
            self.async_write_ha_state()


class ClassificationRegistry:
    """Owns every classification entity and turns a webhook payload into
    entity updates, creating entities the first time a classifier_id or
    class name is seen."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self._sensor_add: AddEntitiesCallback | None = None
        self._binary_add: AddEntitiesCallback | None = None
        self._classifiers: dict[str, dict] = {}

    def register_sensor_adder(self, add_entities: AddEntitiesCallback) -> None:
        self._sensor_add = add_entities

    def register_binary_adder(self, add_entities: AddEntitiesCallback) -> None:
        self._binary_add = add_entities

    async def async_handle_result(self, payload: dict) -> None:
        try:
            classifier_id = str(payload["classifier_id"])
            name = str(payload["name"]) or classifier_id
            classes = [str(c) for c in payload["classes"]]
            label = str(payload["label"])
            score = float(payload["score"])
            tag_id = str(payload["tag_id"])
            filename = str(payload["filename"])
            ran_at = int(payload["ran_at"])
        except (KeyError, TypeError, ValueError):
            _LOGGER.warning("Ignoring malformed classification push: %s", payload)
            return

        if self._sensor_add is None or self._binary_add is None:
            _LOGGER.debug(
                "Classification result for %s arrived before entity platforms "
                "finished loading - dropped, the next result will create it",
                classifier_id,
            )
            return

        entry = self._classifiers.get(classifier_id)
        new_sensors = []
        new_binaries = []
        if entry is None:
            state_entity = ClassificationStateSensor(classifier_id, name)
            updated_entity = ClassificationUpdatedSensor(classifier_id, name)
            entry = {"state": state_entity, "updated": updated_entity, "classes": {}}
            new_sensors += [state_entity, updated_entity]
            self._classifiers[classifier_id] = entry

        # A class dropped from the classifier's list is left in place rather
        # than removed - a stray off-forever binary sensor is a smaller
        # surprise than one that vanishes out from under an automation.
        for class_name in classes:
            if class_name not in entry["classes"]:
                sensor = ClassificationClassBinarySensor(classifier_id, name, class_name)
                entry["classes"][class_name] = sensor
                new_binaries.append(sensor)

        # Values are set before add_entities() so the platform's own initial
        # state write (once hass actually attaches, a moment after
        # add_entities returns) already has the right numbers - each
        # update_*() call's own async_write_ha_state() is a safe no-op for
        # anything not attached yet.
        entry["state"].update_result(label, score, tag_id, filename, ran_at)
        entry["updated"].update_result(ran_at)
        for class_name, sensor in entry["classes"].items():
            sensor.update_state(class_name == label)

        if new_sensors:
            self._sensor_add(new_sensors)
        if new_binaries:
            self._binary_add(new_binaries)


async def async_setup_classify_webhook(hass: HomeAssistant) -> None:
    """Register the webhook once per Home Assistant run.

    Guarded the same way _async_serve_frontend guards the static path
    registration: multiple config entries (multiple vacuums) must not each
    try to register the same webhook_id.
    """
    if hass.data.get(_DATA_WEBHOOK_ID):
        return
    webhook_id = webhook.async_generate_id()
    hass.data[_DATA_WEBHOOK_ID] = webhook_id
    hass.data[_DATA_REGISTRY] = ClassificationRegistry(hass)
    webhook.async_register(
        hass, DOMAIN, "Dreame classification results", webhook_id, _handle_webhook
    )


async def _handle_webhook(hass: HomeAssistant, webhook_id: str, request: web.Request) -> web.Response:
    try:
        payload = await request.json()
    except ValueError:
        return web.Response(status=400)
    registry: ClassificationRegistry | None = hass.data.get(_DATA_REGISTRY)
    if registry is not None:
        await registry.async_handle_result(payload)
    return web.Response(status=200)


def get_registry(hass: HomeAssistant) -> ClassificationRegistry | None:
    return hass.data.get(_DATA_REGISTRY)


def classify_webhook_url(hass: HomeAssistant) -> str | None:
    """The full URL the add-on should POST classification results to, or
    None if Home Assistant has no usable URL yet (very early startup, or an
    instance with no internal/external URL configured at all)."""
    webhook_id = hass.data.get(_DATA_WEBHOOK_ID)
    if not webhook_id:
        return None
    try:
        return webhook.async_generate_url(hass, webhook_id)
    except NoURLAvailableError:
        return None
