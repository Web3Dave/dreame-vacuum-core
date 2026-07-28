"""Config flow: credentials -> pick a device -> optional camera.

Camera setup is a separate, skippable step on purpose. The companion add-on is
x86_64-only (Tencent's XP2P libraries have no ARM build), so on a Raspberry Pi
or HA Green the vacuum half must still work without it.
"""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .companion import CompanionClient
from .const import (
    CONF_CAMERA_PIN,
    CONF_COMPANION_HOST,
    CONF_COMPANION_PORT,
    CONF_COMPANION_TOKEN,
    CONF_COUNTRY,
    CONF_DID,
    CONF_ENABLE_CAMERA,
    CONF_MODEL,
    CONF_NAME,
    CONF_PASSWORD,
    CONF_USERNAME,
    COUNTRIES,
    DEFAULT_COMPANION_PORT,
    DOMAIN,
)
from .profile import available_models, load_profile
from .transport import DreameVacuumProtocol

_LOGGER = logging.getLogger(__name__)


class DreameVacuumCoreConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(entry: ConfigEntry) -> "DreameVacuumCoreOptionsFlow":
        return DreameVacuumCoreOptionsFlow()

    def __init__(self) -> None:
        self._account: dict[str, Any] = {}
        self._devices: list[dict] = []
        self._chosen: dict | None = None

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            devices = await self.hass.async_add_executor_job(
                self._login_and_list,
                user_input[CONF_USERNAME],
                user_input[CONF_PASSWORD],
                user_input[CONF_COUNTRY],
            )
            if devices is None:
                errors["base"] = "invalid_auth"
            elif not devices:
                errors["base"] = "no_devices"
            else:
                self._account = dict(user_input)
                self._devices = devices
                return await self.async_step_device()

        schema = vol.Schema(
            {
                vol.Required(CONF_USERNAME, default=(user_input or {}).get(CONF_USERNAME, "")): str,
                vol.Required(CONF_PASSWORD): str,
                vol.Required(CONF_COUNTRY, default=(user_input or {}).get(CONF_COUNTRY, "eu")): vol.In(
                    COUNTRIES
                ),
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    def _login_and_list(self, username: str, password: str, country: str) -> list[dict] | None:
        """Blocking - runs in the executor. None means auth failed."""
        protocol = DreameVacuumProtocol(
            username=username,
            password=password,
            country=country,
            prefer_cloud=True,
            account_type="dreame",
        )
        try:
            if not protocol.cloud.login():
                return None
            devices = protocol.cloud.get_devices() or {}
        except Exception as err:  # noqa: BLE001 - surfaced to the user as cannot_connect
            _LOGGER.debug("Device listing failed: %s", err)
            return None
        finally:
            try:
                protocol.disconnect()
            except Exception:  # noqa: BLE001
                pass

        out = []
        for rec in devices.get("page", {}).get("records", []):
            out.append(
                {
                    "did": str(rec.get("did")),
                    "name": rec.get("customName") or rec.get("model"),
                    "model": rec.get("model"),
                    "mac": rec.get("mac"),
                }
            )
        return out

    async def async_step_device(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        if user_input is not None:
            did = user_input[CONF_DID]
            self._chosen = next((d for d in self._devices if d["did"] == did), None)
            if self._chosen:
                await self.async_set_unique_id(did)
                self._abort_if_unique_id_configured()
                return await self.async_step_camera()

        options = {d["did"]: f"{d['name']} ({d['model']})" for d in self._devices}
        return self.async_show_form(
            step_id="device",
            data_schema=vol.Schema({vol.Required(CONF_DID): vol.In(options)}),
            description_placeholders={"count": str(len(options))},
        )

    async def async_step_camera(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Optional - skip by leaving the token blank."""
        assert self._chosen is not None
        errors: dict[str, str] = {}

        if user_input is not None:
            token = (user_input.get(CONF_COMPANION_TOKEN) or "").strip()
            if not token:
                return self._create(enable_camera=False)

            client = CompanionClient(
                async_get_clientsession(self.hass),
                user_input[CONF_COMPANION_HOST],
                int(user_input[CONF_COMPANION_PORT]),
                token,
            )
            # Check the token, not just reachability: /health is
            # unauthenticated, so validating against it would let a wrong
            # token through setup and only fail later on every real call.
            auth = await client.async_check_auth()
            if auth is None:
                errors["base"] = "companion_unreachable"
            elif auth is False:
                errors["base"] = "companion_bad_token"
            else:
                return self._create(
                    enable_camera=True,
                    companion_host=user_input[CONF_COMPANION_HOST],
                    companion_port=int(user_input[CONF_COMPANION_PORT]),
                    companion_token=token,
                    camera_pin=(user_input.get(CONF_CAMERA_PIN) or "").strip(),
                )

        model = self._chosen.get("model") or ""
        profile = load_profile(model)
        schema = vol.Schema(
            {
                vol.Optional(CONF_COMPANION_HOST, default="localhost"): str,
                vol.Optional(CONF_COMPANION_PORT, default=DEFAULT_COMPANION_PORT): int,
                vol.Optional(CONF_COMPANION_TOKEN, default=""): str,
                vol.Optional(CONF_CAMERA_PIN, default=""): str,
            }
        )
        return self.async_show_form(
            step_id="camera",
            data_schema=schema,
            errors=errors,
            description_placeholders={
                "model": model,
                "profile": "yes" if profile.profiled else "no",
                "known_models": str(len(available_models())),
            },
        )

    def _create(self, *, enable_camera: bool, **extra) -> FlowResult:
        assert self._chosen is not None
        data = {
            **self._account,
            CONF_DID: self._chosen["did"],
            CONF_MODEL: self._chosen.get("model"),
            CONF_NAME: self._chosen.get("name"),
            CONF_ENABLE_CAMERA: enable_camera,
        }
        if enable_camera:
            data.update(
                {
                    CONF_COMPANION_HOST: extra["companion_host"],
                    CONF_COMPANION_PORT: extra["companion_port"],
                    CONF_COMPANION_TOKEN: extra["companion_token"],
                    CONF_CAMERA_PIN: extra["camera_pin"],
                }
            )
        return self.async_create_entry(title=self._chosen.get("name") or self._chosen["did"], data=data)


class DreameVacuumCoreOptionsFlow(config_entries.OptionsFlow):
    """Edit the companion add-on settings after setup.

    Without this the API token could only be changed by deleting and
    re-adding the integration, which also loses entity ids and history.
    """

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        errors: dict[str, str] = {}
        cfg = {**self.config_entry.data, **self.config_entry.options}

        if user_input is not None:
            token = (user_input.get(CONF_COMPANION_TOKEN) or "").strip()

            if not token:
                # Clearing the token disables the camera rather than leaving a
                # half-configured client that 401s on every call.
                return self.async_create_entry(
                    data={**self.config_entry.options, CONF_ENABLE_CAMERA: False,
                          CONF_COMPANION_TOKEN: ""}
                )

            client = CompanionClient(
                async_get_clientsession(self.hass),
                user_input[CONF_COMPANION_HOST],
                int(user_input[CONF_COMPANION_PORT]),
                token,
            )
            auth = await client.async_check_auth()
            if auth is None:
                errors["base"] = "companion_unreachable"
            elif auth is False:
                errors["base"] = "companion_bad_token"
            else:
                return self.async_create_entry(
                    data={
                        **self.config_entry.options,
                        CONF_ENABLE_CAMERA: True,
                        CONF_COMPANION_HOST: user_input[CONF_COMPANION_HOST],
                        CONF_COMPANION_PORT: int(user_input[CONF_COMPANION_PORT]),
                        CONF_COMPANION_TOKEN: token,
                        CONF_CAMERA_PIN: (user_input.get(CONF_CAMERA_PIN) or "").strip(),
                    }
                )

        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_COMPANION_HOST, default=cfg.get(CONF_COMPANION_HOST, "localhost")
                ): str,
                vol.Optional(
                    CONF_COMPANION_PORT,
                    default=int(cfg.get(CONF_COMPANION_PORT, DEFAULT_COMPANION_PORT)),
                ): int,
                vol.Optional(
                    CONF_COMPANION_TOKEN, default=cfg.get(CONF_COMPANION_TOKEN, "")
                ): str,
                vol.Optional(CONF_CAMERA_PIN, default=cfg.get(CONF_CAMERA_PIN, "")): str,
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema, errors=errors)
