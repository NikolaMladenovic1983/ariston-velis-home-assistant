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
     tariff of those minutes (`allocate`). If no heating time is known (HA was down), the remainder goes to the
     tariff of the moment the batch was observed.
"""
from __future__ import annotations

import datetime as dt
from typing import Callable, Sequence

EPS = 1e-4
DEFAULT_POWER_KW = 1.5          # Velis Pro 80 nominal heating power
POWER_MIN_KW, POWER_MAX_KW = 1.0, 2.5
SEGMENT_MAX_AGE = dt.timedelta(hours=30)


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


def _split_by_tariff(start: dt.datetime, end: dt.datetime, is_cheap: Callable[[int], bool]) -> tuple[float, float]:
    """Hours of [start, end) in cheap / expensive tariff (split at hour boundaries, local time)."""
    cheap = exp = 0.0
    t = start
    while t < end:
        nxt = min(end, (t.replace(minute=0, second=0, microsecond=0) + dt.timedelta(hours=1)))
        h = (nxt - t).total_seconds() / 3600
        if is_cheap(t.hour):
            cheap += h
        else:
            exp += h
        t = nxt
    return cheap, exp


def allocate(
    segments: list[tuple[dt.datetime, dt.datetime]],
    kwh: float,
    power_kw: float,
    now: dt.datetime,
    is_cheap: Callable[[int], bool],
) -> tuple[float, float, list[tuple[dt.datetime, dt.datetime]]]:
    """Split `kwh` into (cheap, expensive) using heating segments FIFO; returns the unconsumed segments.

    Segments must be local-time aware datetimes, oldest first. Segments older than SEGMENT_MAX_AGE are dropped.
    """
    segs = [(s, e) for s, e in segments if e > now - SEGMENT_MAX_AGE and e > s]
    need_h = kwh / power_kw if power_kw > 0 else 0.0
    cheap_h = exp_h = 0.0
    rest: list[tuple[dt.datetime, dt.datetime]] = []
    for i, (s, e) in enumerate(segs):
        if need_h <= 1e-9:
            rest = segs[i:]
            break
        dur_h = (e - s).total_seconds() / 3600
        take_h = min(dur_h, need_h)
        cut = s + dt.timedelta(hours=take_h)
        c, x = _split_by_tariff(s, cut, is_cheap)
        cheap_h += c
        exp_h += x
        need_h -= take_h
        if cut < e:
            rest = [(cut, e)] + segs[i + 1:]
            break
    covered_h = cheap_h + exp_h
    total_h = kwh / power_kw if power_kw > 0 else 0.0
    if total_h <= 0:
        return 0.0, 0.0, rest
    cheap = kwh * cheap_h / total_h
    exp = kwh * exp_h / total_h
    remainder = max(0.0, kwh - kwh * covered_h / total_h)
    if remainder > EPS:
        if is_cheap(now.hour):
            cheap += remainder
        else:
            exp += remainder
    return cheap, exp, rest
