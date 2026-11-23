"""Config flow for ariston_schedule."""
from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import AristonApiError, AristonAuthError, AristonScheduleApi
from .const import (
    CONF_CHEAP_END,
    CONF_CHEAP_START,
    CONF_GW,
    DEFAULT_CHEAP_END,
    DEFAULT_CHEAP_START,
    DOMAIN,
)

USER_SCHEMA = vol.Schema({vol.Required(CONF_USERNAME): str, vol.Required(CONF_PASSWORD): str})


class AristonScheduleConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._creds: dict[str, str] = {}
        self._plants: dict[str, str] = {}

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            api = AristonScheduleApi(
                async_get_clientsession(self.hass), user_input[CONF_USERNAME], user_input[CONF_PASSWORD]
            )
            try:
                await api.login()
                plants = await api.plants()
            except AristonAuthError:
                errors["base"] = "invalid_auth"
            except AristonApiError:
                errors["base"] = "cannot_connect"
            else:
                self._plants = {p["gw"]: f"{p.get('name') or p['gw']} ({p['gw']})" for p in plants if p.get("gw")}
                if not self._plants:
                    errors["base"] = "no_devices"
                else:
                    self._creds = user_input
                    if len(self._plants) == 1:
                        return await self._create(next(iter(self._plants)))
                    return await self.async_step_device()
        return self.async_show_form(step_id="user", data_schema=USER_SCHEMA, errors=errors)

    async def async_step_device(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return await self._create(user_input[CONF_GW])
        return self.async_show_form(
            step_id="device", data_schema=vol.Schema({vol.Required(CONF_GW): vol.In(self._plants)})
        )

    async def _create(self, gw: str) -> ConfigFlowResult:
        await self.async_set_unique_id(gw)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title=f"Raspored {self._plants[gw]}", data={**self._creds, CONF_GW: gw}
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return AristonScheduleOptionsFlow()


class AristonScheduleOptionsFlow(OptionsFlow):
    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        opts = self.config_entry.options
        schema = vol.Schema(
            {
                vol.Required(CONF_CHEAP_START, default=opts.get(CONF_CHEAP_START, DEFAULT_CHEAP_START)):
                    vol.All(vol.Coerce(int), vol.Range(min=0, max=23)),
                vol.Required(CONF_CHEAP_END, default=opts.get(CONF_CHEAP_END, DEFAULT_CHEAP_END)):
                    vol.All(vol.Coerce(int), vol.Range(min=0, max=24)),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
