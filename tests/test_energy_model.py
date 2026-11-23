"""Tests for energy accounting. Snapshots are real p=1 series recorded on 2026-11-22.
Run: python -I tests/test_energy_model.py  (or pytest)"""
import datetime as dt
import importlib.util
import pathlib
import sys

_P = pathlib.Path(__file__).resolve().parent.parent / "ha-config/custom_components/ariston_schedule/energy_model.py"
_spec = importlib.util.spec_from_file_location("energy_model", _P)
em = importlib.util.module_from_spec(_spec)
sys.modules["energy_model"] = em
_spec.loader.exec_module(em)

TZ = dt.timezone(dt.timedelta(hours=1))
Z = [0.0]
# 13:21 (and 14:16 — shifted by one with a zero)
S1321 = Z * 13 + [1.575, 0, 0.075, 0.725, 0, 0, 1.05, 1.75, 0, 0.05, 0]
S1416 = S1321[1:] + [0.0]
S1556 = S1416                      # 1h40 later: series did NOT move
S1607 = S1556[1:] + [0.475]        # one shift, new value
S1806 = S1607[1:] + [0.825]        # 2 hours later, only one shift
NOW_API = [0.0] * 10 + [1.575, 0, 0.075, 0.725, 0, 0, 1.05, 1.75, 0, 0.05, 0, 0, 0.475, 0.825]


def cheap(h):
    return 0 <= h < 6


def approx(a, b):
    return abs(a - b) < 1e-6


def test_snapshots_match_api():
    assert S1806 == NOW_API


def test_no_double_count_when_series_stalls():
    assert em.new_energy(S1416, S1556) == (0.0, 0)
    assert em.new_energy(S1321, S1416)[0] == 0.0


def test_new_values_counted_once():
    kwh, s = em.new_energy(S1556, S1607)
    assert s == 1 and approx(kwh, 0.475)
    kwh, s = em.new_energy(S1607, S1806)
    assert s == 1 and approx(kwh, 0.825)
    total = sum(em.new_energy(a, b)[0] for a, b in [(S1321, S1416), (S1416, S1556), (S1556, S1607), (S1607, S1806)])
    assert approx(total, 1.3)


def test_current_slot_growing_without_shift():
    a = S1607[:-1] + [0.2]
    b = S1607[:-1] + [0.475]
    assert approx(em.new_energy(a, b)[0], 0.275)


def test_shift_and_growth_together():
    a = S1607[:-1] + [0.2]
    b = S1607[1:-1] + [0.475, 0.3]
    kwh, s = em.new_energy(a, b)
    assert s == 1 and approx(kwh, 0.275 + 0.3)


def test_multi_shift_catch_up():
    b = S1556[3:] + [0.4, 0.0, 0.6]
    kwh, s = em.new_energy(S1556, b)
    assert s == 3 and approx(kwh, 1.0)


def test_empty_prev_counts_everything_and_length_mismatch():
    assert approx(em.new_energy([0.0] * 24, NOW_API)[0], sum(NOW_API))
    assert em.new_energy([0.0] * 7, NOW_API) == (0.0, None)


def t(h, m=0):
    return dt.datetime(2026, 11, 22, h, m, tzinfo=TZ)


def test_allocate_night_heating_is_cheap_even_if_reported_late():
    segs = [(t(3), t(5, 30))]                      # 2.5 h heating in the night
    c, x, rest = em.allocate(segs, 3.75, 1.5, t(8), cheap)
    assert approx(c, 3.75) and approx(x, 0) and rest == []


def test_allocate_split_across_boundary_and_fifo_rest():
    segs = [(t(5), t(7)), (t(15), t(15, 30))]
    c, x, rest = em.allocate(segs, 1.5, 1.5, t(16), cheap)   # 1 h: 05:00-06:00 cheap
    assert approx(c, 1.5) and approx(x, 0)
    assert rest == [(t(6), t(7)), (t(15), t(15, 30))]
    c, x, rest = em.allocate(rest, 1.5, 1.5, t(16), cheap)   # next hour 06-07 expensive
    assert approx(c, 0) and approx(x, 1.5) and rest == [(t(15), t(15, 30))]


def test_allocate_without_segments_uses_current_tariff():
    c, x, rest = em.allocate([], 1.3, 1.5, t(19), cheap)
    assert approx(x, 1.3) and c == 0
    c, x, _ = em.allocate([], 0.5, 1.5, t(2), cheap)
    assert approx(c, 0.5)


def test_allocate_partial_cover_and_stale_drop():
    old = (t(0) - dt.timedelta(hours=40), t(0) - dt.timedelta(hours=39))
    c, x, rest = em.allocate([old, (t(4), t(4, 30))], 1.5, 1.5, t(20), cheap)
    assert approx(c, 0.75) and approx(x, 0.75) and rest == []


def test_overnight_batches_are_plausible_and_counted():
    """Real 22./23.11: each batch appeared ~10 min after the previous poll and was wrongly rejected in v0.2."""
    seq = [S1806]
    for v in (2.975, 1.125, 0.975, 0.0, 0.0, 2.35, 0.0):
        seq.append(seq[-1][1:] + [v])
    total = 0.0
    for a, b in zip(seq, seq[1:]):
        kwh, s = em.new_energy(a, b)
        assert em.plausible(a, b, s, hours_since=10 / 60)
        total += kwh
    assert approx(total, 7.425)
    assert seq[-1][-10:] == [0.0, 0.475, 0.825, 2.975, 1.125, 0.975, 0.0, 0.0, 2.35, 0.0]  # as recorded 23.11 08:11


def test_plausible_rejects_full_replacement():
    other = [0.3] * 24
    kwh, s = em.new_energy(S1806, other)
    assert s == 24 and not em.plausible(S1806, other, s, hours_since=0.2)
    assert em.plausible(S1806, other, s, hours_since=30)
    assert not em.plausible(S1806, [0.0] * 7, None, 1)


def test_estimate_power():
    assert em.estimate_power(1, 1) == em.DEFAULT_POWER_KW
    assert approx(em.estimate_power(6, 4), 1.5)
    assert em.estimate_power(30, 5) == em.POWER_MAX_KW


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
