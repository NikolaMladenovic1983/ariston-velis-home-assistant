"""Tests for the pure schedule model. Run: python -I -m pytest tests -q  (or python -I tests/test_schedule_model.py)"""
import datetime as dt
import importlib.util
import pathlib
import sys

_P = pathlib.Path(__file__).resolve().parent.parent / "ha-config/custom_components/ariston_schedule/schedule_model.py"
_spec = importlib.util.spec_from_file_location("schedule_model", _P)
sm = importlib.util.module_from_spec(_spec)
sys.modules["schedule_model"] = sm
_spec.loader.exec_module(sm)

CURRENT = {"plans": [{"days": [1, 2, 3, 4, 5, 6, 0], "shws": [
    {"time": "07:00:00", "qty": -1, "temp": 70.0},
    {"time": "20:00:00", "qty": -1, "temp": 55.0}]}]}


def test_roundtrip_current_device_schedule():
    assert sm.week_to_api(sm.week_from_api(CURRENT)) == CURRENT


def test_parse_days_groups_and_names():
    assert sm.parse_days("radni") == [1, 2, 3, 4, 5]
    assert sm.parse_days(["vikend"]) == [6, 0]
    assert sm.parse_days(["pon", "Sreda", 0]) == [1, 3, 0]


def test_set_days_groups_into_two_plans():
    week = sm.week_from_api(CURRENT)
    week = sm.set_days(week, sm.parse_days("vikend"), [sm.parse_slot({"time": "09:00", "temp": 65})])
    api = sm.week_to_api(week)
    assert api["plans"][0]["days"] == [1, 2, 3, 4, 5]
    assert api["plans"][1] == {"days": [6, 0], "shws": [{"time": "09:00:00", "qty": -1, "temp": 65.0}]}


def test_showers_slot_and_validation():
    assert sm.parse_slot({"time": "6:30", "showers": 2}).to_api() == {"time": "06:30:00", "qty": 2, "temp": 0.0}
    for bad in ({"time": "25:00", "temp": 60}, {"time": "06:00", "temp": 90},
                {"time": "06:00", "temp": 60, "showers": 2}, {"time": "06:00"}):
        try:
            sm.parse_slot(bad)
        except sm.ScheduleError:
            continue
        raise AssertionError(f"should fail: {bad}")


def test_duplicate_times_rejected():
    week = sm.set_days(sm.week_from_api(CURRENT), [1], [sm.parse_slot({"time": "06:00", "temp": 60}),
                                                         sm.parse_slot({"time": "06:00", "temp": 70})])
    try:
        sm.week_to_api(week)
    except sm.ScheduleError:
        return
    raise AssertionError("duplicate should fail")


def test_next_slot():
    tz = dt.timezone(dt.timedelta(hours=1))
    week = sm.week_from_api(CURRENT)
    when, slot = sm.next_slot(week, dt.datetime(2026, 11, 22, 13, 0, tzinfo=tz))  # Sunday 13:00
    assert when == dt.datetime(2026, 11, 22, 20, 0, tzinfo=tz) and slot.temp == 55
    when, slot = sm.next_slot(week, dt.datetime(2026, 11, 22, 21, 0, tzinfo=tz))
    assert when == dt.datetime(2026, 11, 23, 7, 0, tzinfo=tz) and slot.temp == 70


def test_normalize_ignores_grouping():
    split = {"plans": [{"days": [1, 2, 3, 4, 5], "shws": CURRENT["plans"][0]["shws"]},
                       {"days": [6, 0], "shws": CURRENT["plans"][0]["shws"]}]}
    assert sm.normalize_api(split) == sm.normalize_api(CURRENT)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
