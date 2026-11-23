"""Excel report: kWh and cost per tariff by month / day / hour, from Home Assistant long-term statistics.

The file is written to /config/www/bojler-<random token>/bojler_potrosnja.xlsx and served at /local/...
(/local is not behind HA login; the random token makes the URL unguessable, and HA is reachable only
inside the tailnet — the public Funnel exposes only the Google endpoints.)
"""
from __future__ import annotations

import datetime as dt
import logging
import secrets
from pathlib import Path
from typing import Any

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from . import xlsx_writer as xw
from .const import CONF_CHEAP_END, CONF_CHEAP_START, DEFAULT_CHEAP_END, DEFAULT_CHEAP_START, DOMAIN

_LOGGER = logging.getLogger(__name__)
FILE_NAME = "bojler_potrosnja.xlsx"
# key -> unique_id suffix of our own sensors
SERIES = {
    "kwh_cheap": "energy_cumulative_cheap",
    "kwh_exp": "energy_cumulative_expensive",
    "rsd_cheap": "cost_cheap",
    "rsd_exp": "cost_expensive",
}


def _entity_ids(hass: HomeAssistant, gw: str) -> dict[str, str]:
    reg = er.async_get(hass)
    ids = {k: reg.async_get_entity_id("sensor", DOMAIN, f"{gw}_{uid}") for k, uid in SERIES.items()}
    missing = [k for k, v in ids.items() if not v]
    if missing:
        raise ValueError(f"Nedostaju senzori za izveštaj: {missing}")
    return ids  # type: ignore[return-value]


def _ts(row: dict[str, Any]) -> dt.datetime:
    start = row["start"]
    if isinstance(start, (int, float)):
        start = dt.datetime.fromtimestamp(start, dt.UTC)
    return dt_util.as_local(start).replace(tzinfo=None)


def _table(stats: dict[str, list[dict[str, Any]]], ids: dict[str, str]) -> list[tuple[dt.datetime, dict[str, float]]]:
    merged: dict[dt.datetime, dict[str, float]] = {}
    for key, sid in ids.items():
        for row in stats.get(sid, []):
            merged.setdefault(_ts(row), {})[key] = float(row.get("change") or 0.0)
    return sorted(merged.items())


def _rows(table, first_cell) -> list[list[Any]]:
    out = []
    for t, v in table:
        kc, kx = v.get("kwh_cheap", 0.0), v.get("kwh_exp", 0.0)
        rc, rx = v.get("rsd_cheap", 0.0), v.get("rsd_exp", 0.0)
        share = round(100 * kc / (kc + kx), 1) if kc + kx > 0.0005 else None
        out.append([first_cell(t), round(kc, 3), round(kx, 3), round(kc + kx, 3),
                    round(rc, 2), round(rx, 2), round(rc + rx, 2), share])
    return out


def _columns(first: xw.Column, currency: str) -> list[xw.Column]:
    return [
        first,
        xw.Column("kWh jeftina", "kwh", 13, True),
        xw.Column("kWh skupa", "kwh", 13, True),
        xw.Column("kWh ukupno", "kwh", 13, True),
        xw.Column(f"{currency} jeftina", "money", 13, True),
        xw.Column(f"{currency} skupa", "money", 13, True),
        xw.Column(f"{currency} ukupno", "money", 13, True),
        xw.Column("Udeo jeftine", "pct", 13),
    ]


async def async_export(hass: HomeAssistant, coord) -> dict[str, Any]:
    """Build the workbook and write it; returns {"url", "generated", "path"}."""
    ids = _entity_ids(hass, coord.gw)
    now = dt_util.now()
    since = dt_util.as_utc(now - dt.timedelta(days=800))
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    hours_since = dt_util.as_utc((month_start - dt.timedelta(days=1)).replace(day=1))  # from start of last month
    stat_ids = set(ids.values())
    rec = get_instance(hass)

    def query(start, period):
        return statistics_during_period(hass, start, None, stat_ids, period, None, {"change"})

    by_month = await rec.async_add_executor_job(query, since, "month")
    by_day = await rec.async_add_executor_job(query, since, "day")
    by_hour = await rec.async_add_executor_job(query, hours_since, "hour")

    cur = hass.config.currency or "RSD"
    led = coord._ledger or {}
    prices = led.get("prices", {})
    o = coord.config_entry.options
    cs, ce = o.get(CONF_CHEAP_START, DEFAULT_CHEAP_START), o.get(CONF_CHEAP_END, DEFAULT_CHEAP_END)

    sheets = [
        xw.Sheet("Po mesecima", _columns(xw.Column("Mesec", "month", 16), cur),
                 _rows(_table(by_month, ids), lambda t: t.date()), totals_label="Ukupno"),
        xw.Sheet("Po danima", _columns(xw.Column("Datum", "date", 13), cur),
                 _rows(_table(by_day, ids), lambda t: t.date()), totals_label="Ukupno"),
        xw.Sheet("Po satima", _columns(xw.Column("Sat (početak)", "datetime", 18), cur),
                 [r for r in _rows(_table(by_hour, ids), lambda t: t) if r[3] or r[6]], totals_label="Ukupno"),
        xw.Sheet("Info", [xw.Column("Stavka", "text", 34), xw.Column("Vrednost", "text", 60)], [
            ["Napravljeno", now.strftime("%d.%m.%Y %H:%M")],
            ["Jeftina tarifa", f"{cs:02d}:00–{ce:02d}:00"],
            [f"Cena jeftina ({cur}/kWh)", prices.get("cheap", "")],
            [f"Cena skupa ({cur}/kWh)", prices.get("expensive", "")],
            ["Ukupno od početka merenja (kWh)", round(led.get("total", 0.0), 3)],
            ["Napomena", "Ariston šalje potrošnju sa kašnjenjem 1–3 h; list „Po satima“ prikazuje sat kada je "
                         "potrošnja obračunata (samo sati sa potrošnjom). Dnevni i mesečni zbirovi su tačni, "
                         "tarifa je određena po stvarnom vremenu grejanja."],
            ["Trošak", "Svaki kWh po ceni tarife važećoj u trenutku obračuna."],
        ]),
    ]
    data = xw.build_xlsx(sheets)

    store: Store[dict[str, Any]] = Store(hass, 1, f"{DOMAIN}.{coord.gw}.export")
    meta = await store.async_load() or {}
    if not meta.get("token"):
        meta["token"] = secrets.token_urlsafe(16)
        await store.async_save(meta)
    folder = Path(hass.config.path("www", f"bojler-{meta['token']}"))

    def write() -> Path:
        folder.mkdir(parents=True, exist_ok=True)
        tmp = folder / (FILE_NAME + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(folder / FILE_NAME)
        return folder / FILE_NAME

    path = await hass.async_add_executor_job(write)
    info = {
        "url": f"/local/bojler-{meta['token']}/{FILE_NAME}?v={int(now.timestamp())}",
        "generated": now.isoformat(),
        "path": str(path),
        "months": len(sheets[0].rows),
        "days": len(sheets[1].rows),
    }
    _LOGGER.debug("Excel report written: %s", info)
    return info
