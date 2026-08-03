"""Constants for Dreame Vacuum Unlocked Integration."""

from __future__ import annotations

DOMAIN = "dreame_vacuum_unlocked_integration"

# --- config entry keys ---------------------------------------------------
CONF_USERNAME = "username"
CONF_PASSWORD = "password"
CONF_COUNTRY = "country"
CONF_DID = "did"
CONF_MODEL = "model"
CONF_NAME = "name"

# Camera/streaming lives in the companion add-on because Tencent's XP2P
# libraries are x86_64-only. Keeping it out of process is what lets this
# integration stay pure Python and run on ARM (Pi, HA Green/Yellow).
CONF_COMPANION_HOST = "companion_host"
CONF_COMPANION_PORT = "companion_port"
CONF_COMPANION_TOKEN = "companion_token"
CONF_CAMERA_PIN = "camera_pin"
CONF_ENABLE_CAMERA = "enable_camera"

DEFAULT_COMPANION_PORT = 8099
COUNTRIES = ["eu", "cn", "us", "ru", "sg", "kr"]

# --- device keep-alive ---------------------------------------------------
# The device treats this as "a client is watching me" and stops sending
# non-essential data when it lapses, so it must be refreshed continuously
# while the integration is loaded. The official app uses ~25s; we match it.
#
# NOTE: distinct from the *camera* keep-alive (siid 10001 / aiid 1 / piid 6),
# which the companion add-on owns while a stream is running. Two different
# mechanisms - conflating them silently breaks one of them.
SIID_DEVICE_KEEP_ALIVE = 14
PIID_DEVICE_KEEP_ALIVE = 4
KEEP_ALIVE_INTERVAL = 25

# --- polling -------------------------------------------------------------
# Push (MQTT properties_changed) is the primary path; polling only
# reconciles. Intervals adapt so an idle vacuum isn't hammered while an
# active one stays responsive.
POLL_ACTIVE = 3
POLL_IDLE = 10
POLL_FAIL_SHORT = 5
POLL_FAIL_LONG = 30
PROPERTY_BATCH_SIZE = 15

# --- profiles ------------------------------------------------------------
PROFILES_DIR = "profiles"
SERVICES_PROFILE = "_services.json"
