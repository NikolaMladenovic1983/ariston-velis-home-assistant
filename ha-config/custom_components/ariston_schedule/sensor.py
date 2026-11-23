"""Sensors for ariston_schedule."""
from __future__ import annotations

import datetime as dt
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from . import schedule_model as sm
from .const import CONF_CHEAP_END, CONF_CHEAP_START, DEFAULT_CHEAP_END, DEFAULT_CHEAP_START, DOMAIN
from .coordinator import AristonScheduleCoordinator


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    coord: AristonScheduleCoordinator = entry.runtime_data
    async_add_entities([
        ScheduleSensor(coord),
        NextSlotSensor(coord),
        Consumption24hSensor(coord),
        CheapConsumption24hSensor(coord),
        CheapShareSensor(coord),
        Consumption7dSensor(coord),
        CumulativeEnergySensor(coord, "total", "Ukupna potrošnja"),
        CumulativeEnergySensor(coord, "cheap", "Ukupna potrošnja jeftina tarifa"),
        CumulativeEnergySensor(coord, "expensive", "Ukupna potrošnja skupa tarifa"),
        CumulativeCostSensor(coord, "cost_cheap", "Trošak jeftina tarifa"),
        CumulativeCostSensor(coord, "cost_expensive", "Trošak skupa tarifa"),
        CumulativeCostSensor(coord, "cost_total", "Trošak ukupno"),
        ExportSensor(coord),
    ])


class _Base(CoordinatorEntity[AristonScheduleCoordinator], SensorEntity):
    _attr_has_entity_name = True

    def __init__(self, coord: AristonScheduleCoordinator, key: str, name: str) -> None:
        super().__init__(coord)
        self._attr_unique_id = f"{coord.gw}_{key}"
        self._attr_name = name
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coord.gw)},
            name="Bojler raspored",
            manufacturer="Ariston",
            model="Velis Pro WiFi 80L",
        )

    def _cheap_hours(self) -> tuple[int, int]:
        o = self.coordinator.config_entry.options
        return o.get(CONF_CHEAP_START, DEFAULT_CHEAP_START), o.get(CONF_CHEAP_END, DEFAULT_CHEAP_END)

    def _is_cheap(self, hour: int) -> bool:
        return self.coordinator.is_cheap_hour(hour)


class ScheduleSensor(_Base):
    """State = next slot; attributes = whole weekly schedule."""

    _attr_icon = "mdi:calendar-clock"

    def __init__(self, coord: AristonScheduleCoordinator) -> None:
        super().__init__(coord, "schedule", "Raspored")

    @property
    def native_value(self) -> str | None:
        nxt = sm.next_slot(self.coordinator.data.week, dt_util.now())
        if not nxt:
            return "prazan"
        when, slot = nxt
        return f"{sm.DAY_NAMES_SR[(when.weekday() + 1) % 7][:3]} {slot.describe()}"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        d = self.coordinator.data
        start, end = self._cheap_hours()
        return {
            "raspored": sm.describe_week(d.week),
            "plans": d.schedule.get("plans", []),
            "poslednja_izmena": d.last_write,
            "jeftina_tarifa": f"{start:02d}:00–{end:02d}:00",
        }


class NextSlotSensor(_Base):
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:clock-start"

    def __init__(self, coord: AristonScheduleCoordinator) -> None:
        super().__init__(coord, "next_slot", "Sledeći termin")

    @property
    def native_value(self) -> dt.datetime | None:
        nxt = sm.next_slot(self.coordinator.data.week, dt_util.now())
        return nxt[0] if nxt else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        nxt = sm.next_slot(self.coordinator.data.week, dt_util.now())
        return {"termin": nxt[1].describe()} if nxt else {}


class _EnergyBase(_Base):
    # Rolling windows (24h / 7 days) built from the energy ledger — not meters, so no state_class.
    # The Ariston hourly series has no timestamps and lags (see energy_model.py), so it is NOT used for hours.
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_suggested_display_precision = 2


class Consumption24hSensor(_EnergyBase):
    _attr_icon = "mdi:lightning-bolt"

    def __init__(self, coord: AristonScheduleCoordinator) -> None:
        super().__init__(coord, "energy_24h", "Potrošnja poslednja 24h")

    @property
    def native_value(self) -> float:
        return round(sum(self.coordinator.recent_energy(24)), 3)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        cheap, exp = self.coordinator.recent_energy(24)
        return {
            "jeftina_kwh": round(cheap, 3),
            "skupa_kwh": round(exp, 3),
            "procena_snage_kw": round(self.coordinator.power_estimate, 2),
            # raw Ariston series: 24 values WITHOUT timestamps, lagging by hours — diagnostics only
            "ariston_serija": self.coordinator.data.series_24,
        }


class CheapConsumption24hSensor(_EnergyBase):
    _attr_icon = "mdi:cash-check"

    def __init__(self, coord: AristonScheduleCoordinator) -> None:
        super().__init__(coord, "energy_cheap_24h", "Potrošnja u jeftinoj tarifi 24h")

    @property
    def native_value(self) -> float:
        return round(self.coordinator.recent_energy(24)[0], 3)


class CheapShareSensor(_Base):
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:percent-circle"
    _attr_suggested_display_precision = 0

    def __init__(self, coord: AristonScheduleCoordinator) -> None:
        super().__init__(coord, "cheap_share_24h", "Udeo jeftine tarife 24h")

    @property
    def native_value(self) -> float | None:
        cheap, exp = self.coordinator.recent_energy(24)
        if cheap + exp < 0.01:
            return None
        return round(100 * cheap / (cheap + exp), 1)


class Consumption7dSensor(_EnergyBase):
    _attr_icon = "mdi:calendar-week"

    def __init__(self, coord: AristonScheduleCoordinator) -> None:
        super().__init__(coord, "energy_7d", "Potrošnja 7 dana")

    @property
    def native_value(self) -> float:
        return round(sum(self.coordinator.recent_energy(24 * 7)), 3)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"ariston_po_danu_najstariji_prvi": self.coordinator.data.daily_7d}


class CumulativeEnergySensor(_Base):
    """Monotonic kWh counter: every kWh reported by the boiler counted once (Energy dashboard / statistics)."""

    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_suggested_display_precision = 2
    _attr_icon = "mdi:counter"

    def __init__(self, coord: AristonScheduleCoordinator, kind: str, name: str) -> None:
        super().__init__(coord, f"energy_cumulative_{kind}", name)
        self._kind = kind

    @property
    def native_value(self) -> float | None:
        return self.coordinator.data.energy.get(self._kind)


class CumulativeCostSensor(_Base):
    """Cumulative cost; each kWh is priced with the tariff price valid when it was counted.

    Persisted in the ledger, so it never resets on restart (Energy's own *_cost sensors do).
    """

    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_state_class = SensorStateClass.TOTAL
    _attr_suggested_display_precision = 2
    _attr_icon = "mdi:cash-multiple"

    def __init__(self, coord: AristonScheduleCoordinator, kind: str, name: str) -> None:
        super().__init__(coord, kind, name)
        self._kind = kind
        self._attr_native_unit_of_measurement = coord.hass.config.currency

    @property
    def native_value(self) -> float | None:
        e = self.coordinator.data.energy
        if self._kind == "cost_total":
            return round(e.get("cost_cheap", 0.0) + e.get("cost_expensive", 0.0), 2)
        return e.get(self._kind)


class ExportSensor(_Base):
    """When the Excel report was last generated; attribute `url` = download link."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:microsoft-excel"

    def __init__(self, coord: AristonScheduleCoordinator) -> None:
        super().__init__(coord, "export_xlsx", "Excel izveštaj")

    @property
    def native_value(self) -> dt.datetime | None:
        g = self.coordinator.export_info.get("generated")
        return dt.datetime.fromisoformat(g) if g else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        i = self.coordinator.export_info
        return {"url": i.get("url"), "meseci": i.get("months"), "dana": i.get("days")}
