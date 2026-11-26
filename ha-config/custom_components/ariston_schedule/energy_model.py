"""Energy accounting from the Ariston cloud consumption series (pure logic, no Home Assistant imports).

Why this exists (analysis 2026-11-22):
  The cloud endpoint `consSequencesApi8` returns p=1 as 24 hourly values WITHOUT timestamps, and the series
  does NOT advance every wall-clock hour — it only shifts when the boiler uploads new data (snapshots showed
  e.g. 4 hours elapsed but only 2 shifts). Mapping "last element = current hour" therefore mislabels hours,
  moves already-counted values into "new" hours (double counting) and attributes energy to the wrong tariff.

Approach:
  1. TOTAL kWh: compare consecutive snapshots, find how far the series shifted (`series_shift`) and count only
     what is new (`new_energy`). Each kWh is counted exactly once, whatever the lag.
  2. TARIFF split: Home Assistant itself records when the boiler was heating (wall-clock segments). Each new
     batch of kWh is matched FIFO against unconsumed heating time (kWh / power = hours) and split by the
     tariff of those minutes (`allocate`). Leftovers of heating reported long ago are dropped first. If no
     heating time is known (HA was down), the batch goes to the hour and tariff of the moment it was observed.
  3. HOURS: the same allocation says on which wall-clock hours the kWh were used (`Allocation.hours`); the
     ledger keeps them per hour (`book`, `cumulative`) and they are imported as statistics (energy_stats.py).
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Callable, Sequence

EPS = 1e-4
DEFAULT_POWER_KW = 1.5          # Velis Pro 80 nominal heating power
POWER_MIN_KW, POWER_MAX_KW = 1.0, 2.5
SEGMENT_MAX_AGE = dt.timedelta(hours=30)
REPORT_LAG = dt.timedelta(hours=4)  # Ariston reports heating within ~1–3 h (observed 22.–26.11.)


def series_shift(prev: Sequence[float], cur: Sequence[float]) -> int | None:
    """How many positions `cur` moved relative to `prev` (0..n). None if lengths differ.

    Valid shift: every overlapping slot in `cur` is >= the matching slot in `prev` (finished slots never
    shrink; the newest slot may still grow). Among valid shifts choose the one with the most identical
    non-zero slots; ties -> smallest shift.
    """
    n = len(cur)
    if n == 0 or len(prev) != n:
        return None
    best: tuple[int, int] | None = None
    for s in range(n + 1):
        overlap = n - s
        if any(cur[i] < prev[s + i] - EPS for i in range(overlap)):
            continue
        score = sum(1 for i in range(overlap) if prev[s + i] > EPS and abs(cur[i] - prev[s + i]) <= EPS)
        if best is None or score > best[0]:
            best = (score, s)
    return best[1] if best else n


def new_energy(prev: Sequence[float], cur: Sequence[float]) -> tuple[float, int | None]:
    """kWh that appeared in `cur` since `prev` (>= 0) and the detected shift."""
    s = series_shift(prev, cur)
    if s is None:
        return 0.0, None
    return max(0.0, sum(cur) - sum(prev[s:])), s


def plausible(prev: Sequence[float], cur: Sequence[float], shift: int | None, hours_since: float) -> bool:
    """Reject only a comparison that makes no sense.

    NOT time-based: the boiler uploads hours of heating at once (22.11: +2.975 kWh appeared 10 min after the
    previous poll), so "kWh <= power x time since last poll" wrongly rejected every real batch.
    Rejected: different lengths, or the whole window replaced (no overlap) although the previous window had
    data and less than 20 h passed — that would count the entire window again.
    """
    if shift is None:
        return False
    if shift >= len(cur) and sum(prev) > EPS and hours_since < 20:
        return False
    return True


def estimate_power(kwh_seen: float, hours_seen: float) -> float:
    """Average heating power learned from data; default until there is enough of it."""
    if kwh_seen < 3 or hours_seen < 2:
        return DEFAULT_POWER_KW
    return min(POWER_MAX_KW, max(POWER_MIN_KW, kwh_seen / hours_seen))


def _hour_parts(start: dt.datetime, end: dt.datetime) -> list[tuple[dt.datetime, float]]:
    """[start, end) cut at hour boundaries: (hour start, hours in that hour)."""
    out = []
    t = start
    while t < end:
        hour = t.replace(minute=0, second=0, microsecond=0)
        nxt = min(end, hour + dt.timedelta(hours=1))
        out.append((hour, (nxt - t).total_seconds() / 3600))
        t = nxt
    return out


def _split_by_tariff(start: dt.datetime, end: dt.datetime, is_cheap: Callable[[int], bool]) -> tuple[float, float]:
    """Hours of [start, end) in cheap / expensive tariff (split at hour boundaries, local time)."""
    cheap = exp = 0.0
    for hour, h in _hour_parts(start, end):
        if is_cheap(hour.hour):
            cheap += h
        else:
            exp += h
    return cheap, exp


Segment = tuple[dt.datetime, dt.datetime]


@dataclass
class Allocation:
    cheap: float                                   # kWh
    expensive: float                               # kWh
    rest: list[Segment]                            # heating time not matched yet (FIFO queue)
    hours: dict[dt.datetime, list[float]] = field(default_factory=dict)  # heating hour -> [cheap, expensive] kWh

    def __iter__(self):  # (cheap, expensive, rest) unpacking, as before
        return iter((self.cheap, self.expensive, self.rest))


def allocate(
    segments: list[Segment],
    kwh: float,
    power_kw: float,
    now: dt.datetime,
    is_cheap: Callable[[int], bool],
    stale_before: dt.datetime | None = None,
) -> Allocation:
    """Split `kwh` into cheap / expensive using heating segments FIFO and book it on the hours of heating.

    Segments must be local-time aware datetimes, oldest first. Dropped before matching:
      * segments older than SEGMENT_MAX_AGE,
      * segments that ended before `stale_before` (= previous poll − REPORT_LAG): Ariston has reported that
        heating long ago, so what is left of it is only the error of the power estimate. Keeping it moved
        night heating time into the next evening batch and evening time into the next night (26.11.).
    The batch is spread over the matched heating time in proportion (the power estimate only decides how
    much heating time one batch covers). Only if no heating time is known at all (HA was down) the batch is
    booked on the current hour and its tariff.
    """
    segs = [(s, e) for s, e in segments
            if e > now - SEGMENT_MAX_AGE and e > s and (stale_before is None or e >= stale_before)]
    need_h = kwh / power_kw if power_kw > 0 else 0.0
    taken: list[Segment] = []
    rest: list[Segment] = []
    for i, (s, e) in enumerate(segs):
        if need_h <= 1e-9:
            rest = segs[i:]
            break
        take_h = min((e - s).total_seconds() / 3600, need_h)
        cut = s + dt.timedelta(hours=take_h)
        taken.append((s, cut))
        need_h -= take_h
        if cut < e:
            rest = [(cut, e)] + segs[i + 1:]
            break
    parts = [p for s, e in taken for p in _hour_parts(s, e)]
    covered_h = sum(h for _, h in parts)
    if kwh <= 0:
        return Allocation(0.0, 0.0, rest)
    hours: dict[dt.datetime, list[float]] = {}
    if covered_h <= 1e-9:
        parts, covered_h = [(now.replace(minute=0, second=0, microsecond=0), 1.0)], 1.0
    cheap = exp = 0.0
    for hour, h in parts:
        part = kwh * h / covered_h
        slot = hours.setdefault(hour, [0.0, 0.0])
        if is_cheap(hour.hour):
            slot[0] += part
            cheap += part
        else:
            slot[1] += part
            exp += part
    return Allocation(cheap, exp, rest, hours)


# ---------------------------------------------------------------- per-hour ledger (booked on the heating hour)
# ledger["hourly"] = {"<UTC hour ISO>": [kWh cheap, kWh expensive, cost cheap, cost expensive]}
HOURLY_KEYS = ("energy_cheap", "energy_expensive", "cost_cheap", "cost_expensive")


def hour_key(hour: dt.datetime) -> str:
    return hour.astimezone(dt.UTC).replace(minute=0, second=0, microsecond=0).isoformat()


def book(hourly: dict[str, list[float]], hours: dict[dt.datetime, list[float]],
         price_cheap: float, price_exp: float) -> None:
    """Add an allocation's per-hour kWh (and their cost at the given prices) to the hourly ledger."""
    for hour, (c, x) in hours.items():
        row = hourly.setdefault(hour_key(hour), [0.0, 0.0, 0.0, 0.0])
        row[0] += c
        row[1] += x
        row[2] += c * price_cheap
        row[3] += x * price_exp


def cumulative(hourly: dict[str, list[float]]) -> dict[str, list[tuple[dt.datetime, float]]]:
    """Per statistic: [(UTC hour start, running sum)], oldest first. Also energy_total / cost_total."""
    out: dict[str, list[tuple[dt.datetime, float]]] = {k: [] for k in (*HOURLY_KEYS, "energy_total", "cost_total")}
    sums = [0.0] * 4
    for key in sorted(hourly, key=dt.datetime.fromisoformat):
        start = dt.datetime.fromisoformat(key)
        sums = [a + b for a, b in zip(sums, hourly[key])]
        for i, name in enumerate(HOURLY_KEYS):
            out[name].append((start, round(sums[i], 4)))
        out["energy_total"].append((start, round(sums[0] + sums[1], 4)))
        out["cost_total"].append((start, round(sums[2] + sums[3], 4)))
    return out
