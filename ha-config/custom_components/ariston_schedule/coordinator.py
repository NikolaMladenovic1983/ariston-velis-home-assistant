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
from . import energy_stats
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
        self._stats_pushed = False  # all hours are (re-)imported once after start-up

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
        if "hourly" not in led:  # v0.3: book on heating hours + corrected tariff split (once, from history)
            await self._async_rebuild_hourly(led)
        kwh, shift = em.new_energy(led["prev_series"], cur)
        last_seen = dt.datetime.fromisoformat(led["last_seen"]) if led.get("last_seen") else None
        changed_from: str | None = None
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
            # heating that ended long before the previous poll has been reported already (see allocate)
            stale_before = last_seen - em.REPORT_LAG if last_seen else None
            alloc = em.allocate(segs, kwh, power, now, self.is_cheap_hour, stale_before)
            cheap, exp, rest = alloc
            led["segments"] = [[a.isoformat(), b.isoformat()] for a, b in rest]
            p_cheap, p_exp = self._price("cheap"), self._price("expensive")
            led["total"] += kwh
            led["cheap"] += cheap
            led["expensive"] += exp
            led["cost_cheap"] += cheap * p_cheap
            led["cost_expensive"] += exp * p_exp
            em.book(led["hourly"], alloc.hours, p_cheap, p_exp)
            changed_from = min(em.hour_key(h) for h in alloc.hours) if alloc.hours else None
            led["kwh_seen"] += kwh
            led["batches"].append([now.isoformat(), round(cheap, 4), round(exp, 4)])
            _LOGGER.info("Energy +%.3f kWh (cheap %.3f, expensive %.3f, shift %s, power %.2f kW)",
                          kwh, cheap, exp, shift, power)
        keep_from = now - dt.timedelta(days=8)
        led["batches"] = [b for b in led["batches"] if dt.datetime.fromisoformat(b[0]) > keep_from]
        led["prev_series"] = list(cur)
        led["last_seen"] = now.isoformat()
        await self._store.async_save(led)
        if changed_from or not self._stats_pushed:
            energy_stats.async_push(self.hass, led["hourly"], changed_from if self._stats_pushed else None)
            self._stats_pushed = True
        return {k: round(led[k], 3) for k in ("total", "cheap", "expensive")} | {
            "cost_cheap": round(led["cost_cheap"], 2),
            "cost_expensive": round(led["cost_expensive"], 2),
        }

    async def _async_rebuild_hourly(self, led: dict[str, Any]) -> None:
        """Migration to v0.3: rebuild the per-hour ledger and the tariff split from what is recorded.

        * hours before the first batch (v0.1 era): from the cumulative sensors' hourly statistics;
        * every batch (kept 8 days): replayed against the heating sessions from the recorder history with
          the corrected allocation (stale leftovers dropped, booked on the heating hours).
        Totals stay the same; the cheap/expensive split and the costs are replaced by the replayed ones.
        """
        from homeassistant.components.recorder import get_instance, history  # noqa: PLC0415
        from homeassistant.components.recorder.statistics import statistics_during_period  # noqa: PLC0415
        from homeassistant.helpers import entity_registry as er  # noqa: PLC0415

        now = dt_util.now()
        p_cheap, p_exp = self._price("cheap"), self._price("expensive")
        batches = led["batches"]
        first = dt.datetime.fromisoformat(batches[0][0]) if batches else now
        first_hour = first.replace(minute=0, second=0, microsecond=0)
        reg = er.async_get(self.hass)
        ids = {k: reg.async_get_entity_id("sensor", DOMAIN, f"{self.gw}_energy_cumulative_{k}")
               for k in ("cheap", "expensive")}
        ent = self.config_entry.options.get(CONF_HEATING_ENTITY, DEFAULT_HEATING_ENTITY)
        rec = get_instance(self.hass)

        def load() -> tuple[dict, list]:
            stats = statistics_during_period(self.hass, first_hour - dt.timedelta(days=400), first_hour,
                                             {i for i in ids.values() if i}, "hour", None, {"change"})
            states = history.state_changes_during_period(
                self.hass, first - em.SEGMENT_MAX_AGE, now, ent, no_attributes=True,
                include_start_time_state=True).get(ent, [])
            return stats, states

        stats, states = await rec.async_add_executor_job(load)
        hourly: dict[str, list[float]] = {}
        for kind, idx in (("cheap", 0), ("expensive", 1)):
            for row in stats.get(ids[kind] or "", []):
                start = row["start"]
                start = (dt.datetime.fromtimestamp(start, dt.UTC) if isinstance(start, (int, float))
                         else dt_util.as_utc(start))
                change = float(row.get("change") or 0.0)
                if change > em.EPS:
                    hourly.setdefault(em.hour_key(start), [0.0] * 4)[idx] += change
        for r in hourly.values():
            r[2], r[3] = r[0] * p_cheap, r[1] * p_exp

        sessions: list[tuple[dt.datetime, dt.datetime]] = []
        on_since = None
        for st in states:
            t = dt_util.as_local(st.last_changed)
            if st.state == "on" and on_since is None:
                on_since = t
            elif st.state == "off" and on_since is not None:
                sessions.append((on_since, t))
                on_since = None

        power = em.estimate_power(led["kwh_seen"], led["hours_seen"])
        queue: list[tuple[dt.datetime, dt.datetime]] = []
        used_until: dt.datetime | None = None
        new_batches = []
        for iso, c, x in batches:
            when = dt_util.as_local(dt.datetime.fromisoformat(iso))
            for s, e in sessions:
                s = max(s, used_until) if used_until else s
                if s < min(e, when):
                    queue.append((s, min(e, when)))
            if on_since and on_since < when:  # heating was on while the batch arrived
                queue.append((max(on_since, used_until or on_since), when))
            used_until = when
            alloc = em.allocate(queue, c + x, power, when, self.is_cheap_hour,
                                stale_before=when - SCAN_INTERVAL - em.REPORT_LAG)
            queue = alloc.rest
            em.book(hourly, alloc.hours, p_cheap, p_exp)
            new_batches.append([iso, round(alloc.cheap, 4), round(alloc.expensive, 4)])

        kwh = sum(r[0] + r[1] for r in hourly.values())
        if abs(kwh - led["total"]) > 0.05:
            _LOGGER.warning("Energy rebuild: %.3f kWh in hours vs %.3f kWh counted - split kept", kwh, led["total"])
        else:
            for key, idx in (("cheap", 0), ("expensive", 1), ("cost_cheap", 2), ("cost_expensive", 3)):
                led[key] = sum(r[idx] for r in hourly.values())
            led["batches"] = new_batches
            # unmatched heating after the last batch stays in the queue for the next batch
            led["segments"] = [[a.isoformat(), b.isoformat()] for a, b in queue] + [
                [max(s, used_until or s).isoformat(), e.isoformat()]
                for s, e in sessions if used_until is None or e > used_until]
        led["hourly"] = hourly
        _LOGGER.info("Energy ledger v0.3: %d hours rebuilt, cheap %.3f / expensive %.3f kWh",
                     len(hourly), led["cheap"], led["expensive"])

    def recent_energy(self, hours: float) -> tuple[float, float]:
        """(cheap, expensive) kWh of heating in the last `hours` hours (by heating hour, not report time)."""
        if not self._ledger or "hourly" not in self._ledger:
            return 0.0, 0.0
        since = dt_util.now() - dt.timedelta(hours=hours)
        rows = [r for k, r in self._ledger["hourly"].items() if dt.datetime.fromisoformat(k) > since]
        return sum(r[0] for r in rows), sum(r[1] for r in rows)

    async def async_load_export_info(self) -> None:
        from .export import async_load_info  # noqa: PLC0415
        self.export_info = await async_load_info(self.hass, self.gw)

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
        except AristonApiError as err:
            raise UpdateFailed(str(err)) from err
        try:
            consumption = await self.api.get_consumption(self.gw)
        except AristonApiError as err:
            # the reports endpoint fails on its own now and then (HTTP 500): keep the schedule and the last
            # known energy; the next successful poll counts everything that was missed (snapshot diff)
            if self.data is None:
                raise UpdateFailed(str(err)) from err
            _LOGGER.warning("Consumption not available, keeping the last values: %s", err)
            prev = self.data
            return ScheduleData(schedule=schedule, week=sm.week_from_api(schedule), series_24=prev.series_24,
                                daily_7d=prev.daily_7d, last_write=self._last_write, energy=prev.energy)
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
