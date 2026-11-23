"""Ariston Velis built-in schedule (time program) for Home Assistant."""
from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path

import voluptuous as vol

from homeassistant.components.frontend import add_extra_js_url
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady, HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_call_later, async_track_time_change
from homeassistant.helpers.typing import ConfigType

from . import schedule_model as sm
from .api import AristonApiError, AristonAuthError, AristonScheduleApi
from .const import (
    DOMAIN,
    SERVICE_EXPORT_XLSX,
    SERVICE_REFRESH,
    SERVICE_RESTORE_BACKUP,
    SERVICE_SET_DAYS,
    SERVICE_SET_SCHEDULE,
    translated_error,
)
from .coordinator import AristonScheduleCoordinator

_LOGGER = logging.getLogger(__name__)
PLATFORMS = [Platform.SENSOR]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

type AristonScheduleConfigEntry = ConfigEntry[AristonScheduleCoordinator]

SLOT_SCHEMA = vol.Schema(
    {
        vol.Required("time"): cv.string,
        vol.Optional("temp"): vol.Coerce(float),
        vol.Optional("showers"): vol.Coerce(int),
    }
)
SET_DAYS_SCHEMA = vol.Schema(
    {
        vol.Required("days"): vol.Any(cv.string, [vol.Any(cv.string, int)]),
        vol.Required("slots"): [SLOT_SCHEMA],
    }
)
SET_SCHEDULE_SCHEMA = vol.Schema(
    {
        vol.Required("plans"): [
            vol.Schema(
                {
                    vol.Required("days"): vol.Any(cv.string, [vol.Any(cv.string, int)]),
                    vol.Required("slots"): [SLOT_SCHEMA],
                }
            )
        ]
    }
)
RESTORE_SCHEMA = vol.Schema({vol.Optional("file"): cv.string})


FRONTEND_DIR = Path(__file__).parent / "frontend"
FRONTEND_URL = "/ariston_schedule_frontend"


def _frontend_version() -> str:
    h = hashlib.sha1()
    for f in sorted(FRONTEND_DIR.glob("*.js")):
        h.update(f.read_bytes())
    return h.hexdigest()[:10]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Serve the dashboard strategy (multilingual dashboard) and register services once."""
    version = await hass.async_add_executor_job(_frontend_version)
    await hass.http.async_register_static_paths(
        [StaticPathConfig(FRONTEND_URL, str(FRONTEND_DIR), cache_headers=False)]
    )
    # loaded by every frontend session; defines the dashboard strategy "custom:bojler"
    add_extra_js_url(hass, f"{FRONTEND_URL}/bojler-dashboard.js?v={version}")

    def _coordinator() -> AristonScheduleCoordinator:
        entries = [e for e in hass.config_entries.async_loaded_entries(DOMAIN)]
        if not entries:
            raise translated_error("not_loaded")
        return entries[0].runtime_data

    def _slots(raw: list[dict]) -> list[sm.Slot]:
        try:
            return [sm.parse_slot(s) for s in raw]
        except sm.ScheduleError as err:
            raise translated_error(err.code, **err.placeholders) from err

    def _days(raw) -> list[int]:
        try:
            return sm.parse_days(raw)
        except sm.ScheduleError as err:
            raise translated_error(err.code, **err.placeholders) from err

    async def set_days(call: ServiceCall) -> ServiceResponse:
        coord = _coordinator()
        await coord.async_refresh()
        days = _days(call.data["days"])
        week = sm.set_days(coord.data.week, days, _slots(call.data["slots"]))
        return await coord.async_write_week(week, f"set_days {days}")

    async def set_schedule(call: ServiceCall) -> ServiceResponse:
        coord = _coordinator()
        week: sm.Week = {d: [] for d in range(7)}
        for plan in call.data["plans"]:
            week = sm.set_days(week, _days(plan["days"]), _slots(plan["slots"]))
        return await coord.async_write_week(week, "set_schedule")

    async def restore_backup(call: ServiceCall) -> ServiceResponse:
        coord = _coordinator()
        backups = await hass.async_add_executor_job(coord.list_backups)
        if not backups:
            raise translated_error("no_backups")
        name = call.data.get("file")
        path: Path | None = next((b for b in backups if b.name == name), None) if name else backups[-1]
        if path is None:
            raise translated_error("backup_missing", name=name, available=", ".join(b.name for b in backups[-10:]))
        raw = json.loads(await hass.async_add_executor_job(path.read_text, "utf-8"))
        week = sm.week_from_api(raw.get("schedule", raw))
        result = await coord.async_write_week(week, f"restore {path.name}")
        result["restored_from"] = path.name
        return result

    async def refresh(call: ServiceCall) -> None:
        await _coordinator().async_request_refresh()

    hass.services.async_register(DOMAIN, SERVICE_SET_DAYS, set_days, SET_DAYS_SCHEMA,
                                 supports_response=SupportsResponse.OPTIONAL)
    hass.services.async_register(DOMAIN, SERVICE_SET_SCHEDULE, set_schedule, SET_SCHEDULE_SCHEMA,
                                 supports_response=SupportsResponse.OPTIONAL)
    hass.services.async_register(DOMAIN, SERVICE_RESTORE_BACKUP, restore_backup, RESTORE_SCHEMA,
                                 supports_response=SupportsResponse.OPTIONAL)
    hass.services.async_register(DOMAIN, SERVICE_REFRESH, refresh)

    async def export_xlsx(call: ServiceCall) -> ServiceResponse:
        coord = _coordinator()
        await coord.async_export()
        return dict(coord.export_info)

    hass.services.async_register(DOMAIN, SERVICE_EXPORT_XLSX, export_xlsx,
                                 supports_response=SupportsResponse.OPTIONAL)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: AristonScheduleConfigEntry) -> bool:
    api = AristonScheduleApi(
        async_get_clientsession(hass), entry.data[CONF_USERNAME], entry.data[CONF_PASSWORD]
    )
    try:
        await api.login()
    except AristonAuthError as err:
        raise ConfigEntryAuthFailed(str(err)) from err
    except AristonApiError as err:
        raise ConfigEntryNotReady(str(err)) from err

    coordinator = AristonScheduleCoordinator(hass, entry, api)
    await coordinator.async_start_heat_tracking()
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_reload_on_options))

    # Excel report: shortly after start-up and every hour (after the consumption has been counted)
    async def _export(_now=None) -> None:
        try:
            await coordinator.async_export()
        except Exception:  # noqa: BLE001 — the report must never break the integration
            _LOGGER.exception("Excel report could not be generated")

    entry.async_on_unload(async_call_later(hass, 90, _export))
    entry.async_on_unload(async_track_time_change(hass, _export, minute=7, second=0))
    return True


async def _reload_on_options(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: AristonScheduleConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
