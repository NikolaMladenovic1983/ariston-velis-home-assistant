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


def test_allocate_partial_cover_spreads_over_seen_heating():
    """Power estimate too high (1.5 kWh needs 1 h, only 30 min seen): the whole batch goes to the heating
    that was seen (cheap night), not half of it to the tariff of the moment it was reported."""
    old = (t(0) - dt.timedelta(hours=40), t(0) - dt.timedelta(hours=39))
    a = em.allocate([old, (t(4), t(4, 30))], 1.5, 1.5, t(20), cheap)
    assert approx(a.cheap, 1.5) and approx(a.expensive, 0) and a.rest == []
    assert a.hours == {t(4): [1.5, 0.0]}


def test_allocate_books_on_heating_hours():
    a = em.allocate([(t(23, 0) - dt.timedelta(days=1), t(23, 42) - dt.timedelta(days=1))], 0.975, 1.5, t(0, 6), cheap)
    assert approx(a.expensive, 0.975) and list(a.hours) == [t(23) - dt.timedelta(days=1)]


def test_allocate_drops_stale_leftover():
    evening = (t(18, 57) - dt.timedelta(days=1), t(19, 34) - dt.timedelta(days=1))
    night = (t(3, 39), t(4, 0))
    a = em.allocate([evening, night], 0.55, 1.72, t(4, 0), cheap, stale_before=t(4, 0) - dt.timedelta(hours=4, minutes=10))
    assert approx(a.cheap, 0.55) and a.expensive == 0
    assert all(s >= night[0] for s, _ in a.rest)   # evening leftover is gone


def _replay(batches, heating, power=1.72, poll=dt.timedelta(minutes=10)):
    """Replay real batches against real heating sessions, as the coordinator does (open segment cut at `now`)."""
    queue, used_until, out = [], None, []
    for when, kwh in batches:
        for s, e in heating:
            s = max(s, used_until) if used_until else s
            if s < min(e, when):
                queue.append((s, min(e, when)))
        used_until = when
        a = em.allocate(queue, kwh, power, when, cheap, stale_before=when - poll - em.REPORT_LAG)
        queue = a.rest
        out.append(a)
    return out


def d(day, h, m=0):
    return dt.datetime(2026, 11, day, h, m, tzinfo=TZ)


def test_real_24_to_26_nov():
    """Heating sessions from binary_sensor.bojler_greje and batches from the ledger, 24.–26.11.2026.
    v0.2 billed 0.24 + 0.55 + 0.37 kWh of night heating as expensive and 0.59 kWh of evening heating as cheap."""
    heating = [(d(23, 20, 5), d(23, 20, 56)), (d(24, 2, 54), d(24, 5, 43)), (d(25, 4, 0), d(25, 5, 34)),
               (d(25, 18, 57), d(25, 19, 34)), (d(25, 23, 1), d(25, 23, 43)), (d(26, 3, 39), d(26, 5, 41))]
    batches = [(d(23, 22, 8), 1.275), (d(24, 4, 4), 1.7), (d(24, 6, 6), 2.55), (d(25, 4, 7), 0.025),
               (d(25, 6, 9), 2.325), (d(25, 20, 3), 0.875), (d(26, 0, 6), 0.975), (d(26, 4, 0), 0.55),
               (d(26, 6, 2), 2.525)]
    res = _replay(batches, heating)
    expected_cheap = [False, True, True, True, True, False, False, True, True]
    for (when, kwh), a, is_cheap in zip(batches, res, expected_cheap):
        assert approx(a.cheap + a.expensive, kwh)
        assert approx(a.cheap if is_cheap else a.expensive, kwh), (when, a.cheap, a.expensive)
    # booked on the hour of heating: 23:01-23:43 on 25.11. (not 00:00 on 26.11.)
    assert list(res[6].hours) == [d(25, 23)]
    assert all(h.day == 26 and h.hour in (3, 4, 5) for a in res[7:] for h in a.hours)


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


def test_hourly_book_and_cumulative():
    hourly = {}
    em.book(hourly, {d(25, 23): [0.0, 0.975]}, 3.5, 14)
    em.book(hourly, {d(26, 3): [0.4, 0.0], d(26, 4): [0.15, 0.0]}, 3.5, 14)
    em.book(hourly, {d(26, 4): [1.0, 0.0]}, 3.5, 14)                 # late batch adds to an earlier hour
    assert list(hourly) == ["2026-11-25T22:00:00+00:00", "2026-11-26T02:00:00+00:00", "2026-11-26T03:00:00+00:00"]
    c = em.cumulative(hourly)
    assert [v for _, v in c["energy_total"]] == [0.975, 1.375, 2.525]
    assert [v for _, v in c["energy_cheap"]] == [0.0, 0.4, 1.55]
    assert approx(c["cost_total"][-1][1], 0.975 * 14 + 1.55 * 3.5)
    assert c["energy_total"][0][0] == dt.datetime(2026, 11, 25, 22, tzinfo=dt.UTC)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
