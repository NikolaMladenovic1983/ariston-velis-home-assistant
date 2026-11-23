"""Pure schedule model for Ariston Velis built-in time program (no HA imports).

API format (GET/POST velis/timeProgs/{gw}):
    {"plans": [{"days": [1,2,3,4,5], "shws": [{"time": "06:00:00", "qty": -1, "temp": 75.0}, ...]}, ...]}
days: 0 = Sunday, 1 = Monday ... 6 = Saturday.
A slot is EITHER temperature based (temp > 0, qty -1) OR showers based (qty 1..5, temp 0).
Slot semantics: water should be READY at that time (boiler decides when to start heating).
"""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from typing import Any

MIN_TEMP = 40.0
MAX_TEMP = 80.0
MAX_SHOWERS = 5
MAX_SLOTS_PER_DAY = 6  # conservative until verified on the device

# 0 = Sunday ... 6 = Saturday (Ariston convention)
DAY_ALIASES: dict[str, int] = {
    "sun": 0, "sunday": 0, "ned": 0, "nedelja": 0,
    "mon": 1, "monday": 1, "pon": 1, "ponedeljak": 1,
    "tue": 2, "tuesday": 2, "uto": 2, "utorak": 2,
    "wed": 3, "wednesday": 3, "sre": 3, "sreda": 3,
    "thu": 4, "thursday": 4, "cet": 4, "čet": 4, "cetvrtak": 4, "četvrtak": 4,
    "fri": 5, "friday": 5, "pet": 5, "petak": 5,
    "sat": 6, "saturday": 6, "sub": 6, "subota": 6,
}
DAY_GROUPS: dict[str, list[int]] = {
    "all": [1, 2, 3, 4, 5, 6, 0], "svi": [1, 2, 3, 4, 5, 6, 0],
    "workdays": [1, 2, 3, 4, 5], "radni": [1, 2, 3, 4, 5],
    "weekend": [6, 0], "vikend": [6, 0],
}
DAY_NAMES_SR = ["Nedelja", "Ponedeljak", "Utorak", "Sreda", "Četvrtak", "Petak", "Subota"]
WEEK_ORDER = [1, 2, 3, 4, 5, 6, 0]

_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?$")


class ScheduleError(ValueError):
    """Invalid schedule."""


@dataclass(frozen=True, order=True)
class Slot:
    time: str          # "HH:MM:SS"
    temp: float = 0.0  # > 0 -> temperature slot
    showers: int = 0   # > 0 -> showers slot

    def to_api(self) -> dict[str, Any]:
        if self.temp:
            return {"time": self.time, "qty": -1, "temp": float(self.temp)}
        return {"time": self.time, "qty": int(self.showers), "temp": 0.0}

    def describe(self) -> str:
        what = f"{self.temp:g} °C" if self.temp else f"{self.showers} tuš(a)"
        return f"{self.time[:5]} → {what}"


def parse_time(value: Any) -> str:
    if isinstance(value, dt.time):
        return value.strftime("%H:%M:%S")
    m = _TIME_RE.match(str(value).strip())
    if not m:
        raise ScheduleError(f"Neispravno vreme: {value!r} (očekivano HH:MM)")
    h, mi, s = int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)
    if not (0 <= h <= 23 and 0 <= mi <= 59 and 0 <= s <= 59):
        raise ScheduleError(f"Neispravno vreme: {value!r}")
    return f"{h:02d}:{mi:02d}:{s:02d}"


def parse_days(value: Any) -> list[int]:
    items = value if isinstance(value, (list, tuple)) else [value]
    days: list[int] = []
    for item in items:
        if isinstance(item, int) and not isinstance(item, bool):
            if not 0 <= item <= 6:
                raise ScheduleError(f"Neispravan dan: {item}")
            days.append(item)
            continue
        key = str(item).strip().lower()
        if key in DAY_GROUPS:
            days.extend(DAY_GROUPS[key])
        elif key in DAY_ALIASES:
            days.append(DAY_ALIASES[key])
        elif key.isdigit() and 0 <= int(key) <= 6:
            days.append(int(key))
        else:
            raise ScheduleError(f"Nepoznat dan: {item!r}")
    if not days:
        raise ScheduleError("Nije zadat nijedan dan")
    return sorted(set(days), key=WEEK_ORDER.index)


def parse_slot(raw: dict[str, Any]) -> Slot:
    time = parse_time(raw.get("time"))
    temp = float(raw.get("temp") or raw.get("temperature") or 0)
    showers = int(raw.get("showers") or 0)
    if "qty" in raw and not showers and not temp:
        showers = max(0, int(raw["qty"]))
    if temp and showers:
        raise ScheduleError(f"{time[:5]}: zadaj ILI temperaturu ILI broj tuševa, ne oba")
    if temp:
        if not MIN_TEMP <= temp <= MAX_TEMP:
            raise ScheduleError(f"{time[:5]}: temperatura {temp:g} van opsega {MIN_TEMP:g}–{MAX_TEMP:g} °C")
        return Slot(time=time, temp=temp)
    if not 1 <= showers <= MAX_SHOWERS:
        raise ScheduleError(f"{time[:5]}: broj tuševa mora biti 1–{MAX_SHOWERS}")
    return Slot(time=time, showers=showers)


def _slot_from_api(raw: dict[str, Any]) -> Slot:
    temp = float(raw.get("temp") or 0)
    if temp > 0:
        return Slot(time=parse_time(raw["time"]), temp=temp)
    return Slot(time=parse_time(raw["time"]), showers=int(raw.get("qty") or 0))


Week = dict[int, list[Slot]]


def week_from_api(api: dict[str, Any] | None) -> Week:
    week: Week = {d: [] for d in range(7)}
    for plan in (api or {}).get("plans", []):
        slots = [_slot_from_api(s) for s in plan.get("shws", [])]
        for d in plan.get("days", []):
            week[int(d)] = sorted(slots)
    return week


def validate_week(week: Week) -> None:
    for d, slots in week.items():
        times = [s.time for s in slots]
        if len(times) != len(set(times)):
            raise ScheduleError(f"{DAY_NAMES_SR[d]}: dva termina u isto vreme")
        if len(slots) > MAX_SLOTS_PER_DAY:
            raise ScheduleError(f"{DAY_NAMES_SR[d]}: najviše {MAX_SLOTS_PER_DAY} termina dnevno")
    if not any(week.values()):
        raise ScheduleError("Raspored ne može biti potpuno prazan")


def week_to_api(week: Week) -> dict[str, Any]:
    """Group days with identical slots into plans (same shape the official app uses)."""
    validate_week(week)
    groups: dict[tuple[Slot, ...], list[int]] = {}
    for d in WEEK_ORDER:
        groups.setdefault(tuple(sorted(week.get(d, []))), []).append(d)
    plans = [
        {"days": days, "shws": [s.to_api() for s in slots]}
        for slots, days in groups.items()
    ]
    return {"plans": plans}


def set_days(week: Week, days: list[int], slots: list[Slot]) -> Week:
    new = {d: list(v) for d, v in week.items()}
    for d in days:
        new[d] = sorted(slots)
    return new


def normalize_api(api: dict[str, Any]) -> dict[str, Any]:
    """Canonical form for comparison (independent of plan grouping/order)."""
    return week_to_api(week_from_api(api))


def describe_week(week: Week) -> dict[str, list[str]]:
    return {DAY_NAMES_SR[d]: [s.describe() for s in week[d]] for d in WEEK_ORDER}


def next_slot(week: Week, now: dt.datetime) -> tuple[dt.datetime, Slot] | None:
    """Next slot strictly after `now` (now is timezone-aware local time)."""
    for offset in range(8):
        day = now.date() + dt.timedelta(days=offset)
        ariston_day = (day.weekday() + 1) % 7  # python Mon=0 -> ariston Mon=1, Sun=0
        for slot in sorted(week.get(ariston_day, [])):
            h, m, s = (int(x) for x in slot.time.split(":"))
            when = dt.datetime.combine(day, dt.time(h, m, s), tzinfo=now.tzinfo)
            if when > now:
                return when, slot
    return None
