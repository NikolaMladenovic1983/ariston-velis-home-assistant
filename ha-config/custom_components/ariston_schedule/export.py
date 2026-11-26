"""Excel report: kWh and cost per tariff by month / day / hour of heating (external statistics, energy_stats.py).

The file is written to /config/www/bojler-<random token>/bojler_potrosnja.xlsx and served at /local/...
(/local is not behind HA login; the random token makes the URL unguessable, and HA is reachable only
inside the tailnet — the public Funnel exposes only the Google endpoints.)
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import secrets
from pathlib import Path
from typing import Any

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from . import xlsx_writer as xw
from .energy_stats import statistic_id
from .const import CONF_CHEAP_END, CONF_CHEAP_START, DEFAULT_CHEAP_END, DEFAULT_CHEAP_START, DOMAIN

_LOGGER = logging.getLogger(__name__)
# key -> statistic (booked on the hour the boiler heated, see energy_stats.py)
SERIES = {
    "kwh_cheap": statistic_id("energy_cheap"),
    "kwh_exp": statistic_id("energy_expensive"),
    "rsd_cheap": statistic_id("cost_cheap"),
    "rsd_exp": statistic_id("cost_expensive"),
}


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


def _columns(first: xw.Column, T: dict[str, str], cur: str) -> list[xw.Column]:
    return [
        first,
        xw.Column(T["kwh_cheap"], "kwh", 15, True),
        xw.Column(T["kwh_expensive"], "kwh", 15, True),
        xw.Column(T["kwh_total"], "kwh", 13, True),
        xw.Column(T["cur_cheap"].format(cur=cur), "money", 15, True),
        xw.Column(T["cur_expensive"].format(cur=cur), "money", 15, True),
        xw.Column(T["cur_total"].format(cur=cur), "money", 13, True),
        xw.Column(T["share"], "pct", 15),
    ]


I18N_DIR = Path(__file__).parent / "i18n_data"
LANGUAGE_SELECT = "input_select.bojler_jezik"


def _load_texts(hass: HomeAssistant) -> dict[str, Any]:
    """Excel texts in the language chosen in input_select.bojler_jezik (else the HA system language, else en)."""
    catalogs = {f.stem: json.loads(f.read_text(encoding="utf-8")) for f in I18N_DIR.glob("*.json")}
    st = hass.states.get(LANGUAGE_SELECT)
    by_name = {c["language_name"]: lang for lang, c in catalogs.items()}
    lang = by_name.get(st.state) if st else None
    if lang is None:
        sys_lang = hass.config.language or "en"
        lang = sys_lang if sys_lang in catalogs else sys_lang.split("-")[0]
    return catalogs.get(lang) or catalogs["en"]


async def async_export(hass: HomeAssistant, coord) -> dict[str, Any]:
    """Build the workbook and write it; returns {"url", "generated", "path"}."""
    ids = SERIES
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
    texts = await hass.async_add_executor_job(_load_texts, hass)
    T = texts["excel"]
    led = coord._ledger or {}
    prices = led.get("prices", {})
    o = coord.config_entry.options
    cs, ce = o.get(CONF_CHEAP_START, DEFAULT_CHEAP_START), o.get(CONF_CHEAP_END, DEFAULT_CHEAP_END)

    sheets = [
        xw.Sheet(T["sheet_month"], _columns(xw.Column(T["col_month"], "month", 16), T, cur),
                 _rows(_table(by_month, ids), lambda t: t.date()), totals_label=T["total"]),
        xw.Sheet(T["sheet_day"], _columns(xw.Column(T["col_date"], "date", 13), T, cur),
                 _rows(_table(by_day, ids), lambda t: t.date()), totals_label=T["total"]),
        xw.Sheet(T["sheet_hour"], _columns(xw.Column(T["col_hour"], "datetime", 18), T, cur),
                 [r for r in _rows(_table(by_hour, ids), lambda t: t) if r[3] or r[6]], totals_label=T["total"]),
        xw.Sheet(T["sheet_info"], [xw.Column(T["item"], "text", 36), xw.Column(T["value"], "text", 70)], [
            [T["created"], now.strftime("%Y-%m-%d %H:%M")],
            [T["cheap_hours"], f"{cs:02d}:00–{ce:02d}:00"],
            [T["price_cheap"].format(cur=cur), prices.get("cheap", "")],
            [T["price_expensive"].format(cur=cur), prices.get("expensive", "")],
            [T["total_since"], round(led.get("total", 0.0), 3)],
            [T["note"], T["note_text"].format(sheet=T["sheet_hour"])],
            [T["cost"], T["cost_text"]],
        ]),
    ]
    data = xw.build_xlsx(sheets)
    file_name = T["file_name"]

    store = _store(hass, coord.gw)
    meta = await store.async_load() or {}
    if not meta.get("token"):
        meta["token"] = secrets.token_urlsafe(16)
        await store.async_save(meta)
    folder = Path(hass.config.path("www", f"bojler-{meta['token']}"))

    def write() -> Path:
        folder.mkdir(parents=True, exist_ok=True)
        for old in folder.glob("*.xlsx"):  # previous language → other file name
            if old.name != file_name:
                old.unlink(missing_ok=True)
        tmp = folder / (file_name + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(folder / file_name)
        return folder / file_name

    path = await hass.async_add_executor_job(write)
    info = {
        "url": f"/local/bojler-{meta['token']}/{file_name}?v={int(now.timestamp())}",
        "generated": now.isoformat(),
        "path": str(path),
        "months": len(sheets[0].rows),
        "days": len(sheets[1].rows),
    }
    meta["info"] = info
    await store.async_save(meta)  # the dashboard link survives a restart
    _LOGGER.debug("Excel report written: %s", info)
    return info


def _store(hass: HomeAssistant, gw: str) -> Store[dict[str, Any]]:
    return Store(hass, 1, f"{DOMAIN}.{gw}.export")


async def async_load_info(hass: HomeAssistant, gw: str) -> dict[str, Any]:
    """Info about the last written report (empty if the file is gone)."""
    meta = await _store(hass, gw).async_load() or {}
    info = meta.get("info") or {}
    path = info.get("path")
    if not path or not await hass.async_add_executor_job(Path(path).exists):
        return {}
    return info
