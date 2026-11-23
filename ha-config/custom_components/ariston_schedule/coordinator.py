"""Coordinator: polls schedule + consumption, performs safe schedule writes."""
from __future__ import annotations

import datetime as dt
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from . import energy_model as em
from . import schedule_model as sm
from .api import AristonApiError, AristonScheduleApi
from .const import (
    BACKUP_DIR_NAME,
    CONF_CHEAP_END,
    CONF_CHEAP_START,
    CONF_GW,
    CONF_HEATING_ENTITY,
    CONF_PRICE_CHEAP,
    CONF_PRICE_EXPENSIVE,
    DEFAULT_CHEAP_END,
    DEFAULT_CHEAP_START,
    DEFAULT_HEATING_ENTITY,
    DEFAULT_PRICE_CHEAP,
    DEFAULT_PRICE_EXPENSIVE,
    DOMAIN,
    MAX_BACKUPS,
    SCAN_INTERVAL,
    translated_error,
)

_LOGGER = logging.getLogger(__name__)


@dataclass
class ScheduleData:
    schedule: dict[str, Any]
    week: sm.Week
    series_24: list[float] = field(default_factory=list)  # raw p=1 series — NO timestamps (see energy_model)
    daily_7d: list[float] = field(default_factory=list)
    last_write: str | None = None
    energy: dict[str, float] = field(default_factory=dict)  # cumulative kWh + RSD: total/cheap/expensive/cost_*


class AristonScheduleCoordinator(DataUpdateCoordinator[ScheduleData]):
    config_entry: ConfigEntry

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, api: AristonScheduleApi) -> None:
        super().__init__(hass, _LOGGER, config_entry=entry, name=DOMAIN, update_interval=SCAN_INTERVAL)
        self.api = api
        self.gw: str = entry.data[CONF_GW]
        self.backup_dir = Path(hass.config.path(BACKUP_DIR_NAME))
        self._last_write: str | None = None
        self._store: Store[dict[str, Any]] = Store(hass, 1, f"{DOMAIN}.{self.gw}.energy")
        self._ledger: dict[str, Any] | None = None
        self.export_info: dict[str, Any] = {}

    def is_cheap_hour(self, hour: int) -> bool:
        o = self.config_entry.options
        start = o.get(CONF_CHEAP_START, DEFAULT_CHEAP_START)
        end = o.get(CONF_CHEAP_END, DEFAULT_CHEAP_END)
        return start <= hour < end if start < end else (hour >= start or hour < end)

    # ---------------- energy ledger ----------------
    # energy_model.py explains why the hourly series cannot be mapped to wall-clock hours.

    async def _ensure_ledger(self) -> dict[str, Any]:
        if self._ledger is None:
            led = await self._store.async_load() or {"total": 0.0, "cheap": 0.0, "expensive": 0.0}
            for key, default in (("segments", []), ("open_start", None), ("batches", []),
                                 ("kwh_seen", 0.0), ("hours_seen", 0.0), ("prices", {})):
                led.setdefault(key, default)
            self._ledger = led
        return self._ledger

    def _price(self, kind: str) -> float:
        """Current price per kWh from the input_number helper; last known value if unavailable."""
        opts = self.config_entry.options
        ent = (opts.get(CONF_PRICE_CHEAP, DEFAULT_PRICE_CHEAP) if kind == "cheap"
               else opts.get(CONF_PRICE_EXPENSIVE, DEFAULT_PRICE_EXPENSIVE))
        st = self.hass.states.get(ent)
        try:
            val = float(st.state) if st else None
        except ValueError:
            val = None
        prices = self._ledger["prices"]
        if val is not None:
            prices[kind] = val
        return float(prices.get(kind, 0.0))

    async def async_start_heat_tracking(self) -> None:
        """Record wall-clock heating segments from the heating binary sensor."""
        led = await self._ensure_ledger()
        ent = self.config_entry.options.get(CONF_HEATING_ENTITY, DEFAULT_HEATING_ENTITY)
        st = self.hass.states.get(ent)
        heating = st is not None and st.state == "on"
        if led["open_start"] and not heating:
            # HA restarted while heating: close at the last moment we know about
            self._close_segment(dt.datetime.fromisoformat(led.get("last_seen") or dt_util.now().isoformat()))
        elif heating and not led["open_start"]:
            led["open_start"] = dt_util.now().isoformat()

        @callback
        def _changed(event: Event[EventStateChangedData]) -> None:
            new = event.data["new_state"]
            if new is None or new.state not in ("on", "off"):
                return  # unavailable/unknown: keep the current segment as is
            if new.state == "on" and not led["open_start"]:
                led["open_start"] = dt_util.now().isoformat()
            elif new.state == "off" and led["open_start"]:
                self._close_segment(dt_util.now())
            self._store.async_delay_save(lambda: led, 5)

        self.config_entry.async_on_unload(async_track_state_change_event(self.hass, [ent], _changed))

    def _close_segment(self, end: dt.datetime) -> None:
        led = self._ledger
        start = dt.datetime.fromisoformat(led["open_start"])
        if end > start:
            led["segments"].append([start.isoformat(), end.isoformat()])
            led["hours_seen"] += (end - start).total_seconds() / 3600
        led["open_start"] = None

    def _migrate_v1(self, cur: list[float]) -> list[float]:
        """v1 counted slots labelled <= last_hour (label = current hour - offset); the rest is still uncounted."""
        led = self._ledger
        if not led.get("last_hour") or not cur:
            return list(cur)
        last = dt.datetime.fromisoformat(led["last_hour"])
        hour = dt_util.now().replace(minute=0, second=0, microsecond=0)
        n = len(cur)
        return [v if hour - dt.timedelta(hours=n - 1 - i) <= last else 0.0 for i, v in enumerate(cur)]

    async def _update_ledger(self, cur: list[float]) -> dict[str, float]:
        led = await self._ensure_ledger()
        now = dt_util.now()
        if "prev_series" not in led:
            led["prev_series"] = self._migrate_v1(cur)
            led["cost_cheap"] = led["cheap"] * self._price("cheap")
            led["cost_expensive"] = led["expensive"] * self._price("expensive")
            led.pop("last_hour", None)
            _LOGGER.info("Energy ledger v2 started: %s", {k: led[k] for k in ("total", "cheap", "expensive")})
        kwh, shift = em.new_energy(led["prev_series"], cur)
        last_seen = dt.datetime.fromisoformat(led["last_seen"]) if led.get("last_seen") else None
        hours_since = (now - last_seen).total_seconds() / 3600 if last_seen else 24.0
        if not em.plausible(led["prev_series"], cur, shift, hours_since):
            _LOGGER.warning("Consumption series not comparable (shift=%s, +%.3f kWh) — resync without counting",
                            shift, kwh)
            kwh = 0.0
        if kwh > em.EPS:
            if led["open_start"]:  # heating right now: the part up to now can be matched too
                self._close_segment(now)
                led["open_start"] = now.isoformat()
            segs = [(dt_util.as_local(dt.datetime.fromisoformat(a)), dt_util.as_local(dt.datetime.fromisoformat(b)))
                    for a, b in led["segments"]]
            power = em.estimate_power(led["kwh_seen"], led["hours_seen"])
            cheap, exp, rest = em.allocate(segs, kwh, power, now, self.is_cheap_hour)
            led["segments"] = [[a.isoformat(), b.isoformat()] for a, b in rest]
            led["total"] += kwh
            led["cheap"] += cheap
            led["expensive"] += exp
            led["cost_cheap"] += cheap * self._price("cheap")
            led["cost_expensive"] += exp * self._price("expensive")
            led["kwh_seen"] += kwh
            led["batches"].append([now.isoformat(), round(cheap, 4), round(exp, 4)])
            _LOGGER.info("Energy +%.3f kWh (cheap %.3f, expensive %.3f, shift %s, power %.2f kW)",
                          kwh, cheap, exp, shift, power)
        keep_from = now - dt.timedelta(days=8)
        led["batches"] = [b for b in led["batches"] if dt.datetime.fromisoformat(b[0]) > keep_from]
        led["prev_series"] = list(cur)
        led["last_seen"] = now.isoformat()
        await self._store.async_save(led)
        return {k: round(led[k], 3) for k in ("total", "cheap", "expensive")} | {
            "cost_cheap": round(led["cost_cheap"], 2),
            "cost_expensive": round(led["cost_expensive"], 2),
        }

    def recent_energy(self, hours: float) -> tuple[float, float]:
        """(cheap, expensive) kWh counted in the last `hours` hours."""
        if not self._ledger:
            return 0.0, 0.0
        since = dt_util.now() - dt.timedelta(hours=hours)
        bs = [b for b in self._ledger["batches"] if dt.datetime.fromisoformat(b[0]) > since]
        return sum(b[1] for b in bs), sum(b[2] for b in bs)

    async def async_export(self) -> None:
        from .export import async_export  # noqa: PLC0415 — recorder import only when needed
        self.export_info = await async_export(self.hass, self)
        self.async_update_listeners()

    @property
    def power_estimate(self) -> float:
        led = self._ledger or {}
        return em.estimate_power(led.get("kwh_seen", 0.0), led.get("hours_seen", 0.0))

    async def _async_update_data(self) -> ScheduleData:
        try:
            schedule = await self.api.get_schedule(self.gw)
            consumption = await self.api.get_consumption(self.gw)
        except AristonApiError as err:
            raise UpdateFailed(str(err)) from err
        series = self._series(consumption, 1)
        return ScheduleData(
            schedule=schedule,
            week=sm.week_from_api(schedule),
            series_24=series,
            daily_7d=self._series(consumption, 2),
            last_write=self._last_write,
            energy=await self._update_ledger(series),
        )

    @staticmethod
    def _series(consumption: list[dict[str, Any]], period: int) -> list[float]:
        for s in consumption:
            if s.get("p") == period:
                return [float(x) for x in s.get("v", [])]
        return []

    # ---------------- safe write ----------------

    def _backup(self, schedule: dict[str, Any], tag: str) -> Path:
        self.backup_dir.mkdir(exist_ok=True)
        path = self.backup_dir / f"{dt_util.now():%Y-%m-%d_%H%M%S}_{tag}.json"
        path.write_text(json.dumps({"gw": self.gw, "schedule": schedule}, indent=2), encoding="utf-8")
        backups = sorted(self.backup_dir.glob("*.json"))
        for old in backups[:-MAX_BACKUPS]:
            old.unlink(missing_ok=True)
        return path

    def list_backups(self) -> list[Path]:
        return sorted(self.backup_dir.glob("*.json")) if self.backup_dir.exists() else []

    async def async_write_week(self, week: sm.Week, reason: str) -> dict[str, Any]:
        """Backup current -> validate -> POST -> re-read -> verify."""
        try:
            new_api = sm.week_to_api(week)
        except sm.ScheduleError as err:
            raise translated_error(err.code, **err.placeholders) from err
        try:
            current = await self.api.get_schedule(self.gw)
        except AristonApiError as err:
            raise translated_error("read_failed", error=err) from err

        if sm.normalize_api(current) == sm.normalize_api(new_api):
            return {"changed": False, "schedule": sm.describe_week(sm.week_from_api(current))}

        backup = await self.hass.async_add_executor_job(self._backup, current, "pre-write")
        _LOGGER.info("Writing schedule (%s). Backup: %s. New: %s", reason, backup.name, new_api)
        try:
            await self.api.set_schedule(self.gw, new_api)
            after = await self.api.get_schedule(self.gw)
        except AristonApiError as err:
            raise translated_error("write_failed", error=err, backup=backup.name) from err

        if sm.normalize_api(after) != sm.normalize_api(new_api):
            raise translated_error("verify_failed", backup=backup.name)
        self._last_write = f"{dt_util.now():%Y-%m-%d %H:%M} ({reason})"
        await self.async_request_refresh()
        return {
            "changed": True,
            "backup": backup.name,
            "before": sm.describe_week(sm.week_from_api(current)),
            "after": sm.describe_week(sm.week_from_api(after)),
        }
