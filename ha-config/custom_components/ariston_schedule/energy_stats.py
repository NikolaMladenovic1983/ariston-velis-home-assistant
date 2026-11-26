"""Hourly consumption/cost as external statistics, booked on the hour the boiler actually heated.

The cumulative sensors can only add energy at the moment Ariston reports it (1–3 h late), so in long-term
statistics a 23:01 heating appeared at 00:00 of the next day. These statistics are imported for the real
heating hours instead (and re-imported when a late batch changes an earlier hour). Statistic ids:
  ariston_schedule:energy_cheap / energy_expensive / energy_total   (kWh)
  ariston_schedule:cost_cheap   / cost_expensive   / cost_total     (currency)
They are used by the dashboard, the Energy dashboard and the Excel report.
"""
from __future__ import annotations

from homeassistant.components.recorder.models import StatisticData, StatisticMeanType, StatisticMetaData
from homeassistant.components.recorder.statistics import async_add_external_statistics
from homeassistant.core import HomeAssistant

from . import energy_model as em
from .const import DOMAIN

NAMES = {
    "energy_cheap": "Boiler energy – cheap tariff",
    "energy_expensive": "Boiler energy – expensive tariff",
    "energy_total": "Boiler energy",
    "cost_cheap": "Boiler cost – cheap tariff",
    "cost_expensive": "Boiler cost – expensive tariff",
    "cost_total": "Boiler cost",
}


def statistic_id(name: str) -> str:
    return f"{DOMAIN}:{name}"


def async_push(hass: HomeAssistant, hourly: dict[str, list[float]], since_key: str | None = None) -> None:
    """(Re-)import all hours from `since_key` on (all hours when None)."""
    currency = hass.config.currency
    since = em.dt.datetime.fromisoformat(since_key) if since_key else None
    for name, rows in em.cumulative(hourly).items():
        energy = name.startswith("energy")
        meta: StatisticMetaData = {
            "source": DOMAIN,
            "statistic_id": statistic_id(name),
            "name": NAMES[name],
            "unit_of_measurement": "kWh" if energy else currency,
            "unit_class": "energy" if energy else None,
            "has_sum": True,
            "mean_type": StatisticMeanType.NONE,
        }
        data = [StatisticData(start=start, state=total, sum=total) for start, total in rows
                if since is None or start >= since]
        if data:
            async_add_external_statistics(hass, meta, data)
