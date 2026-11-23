"""Change the boiler's built-in schedule (with automatic backup + verification).

Usage:
  python -I tools/schedule_set.py <gw> --move HH:MM HH:MM     # move a slot's time (all plans)
  python -I tools/schedule_set.py <gw> --restore <backup.json> # restore schedule from a backup file
  python -I tools/schedule_set.py <gw> --file <schedule.json>  # write {"plans": [...]} from a file

Every write: backup current schedule -> validate -> POST velis/timeProgs/{gw} -> re-read -> compare.
"""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import json
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from ariston_client import from_env  # noqa: E402

BACKUP_DIR = pathlib.Path(__file__).resolve().parent.parent / "backups"
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d:[0-5]\d$")


def hhmmss(s: str) -> str:
    return s if s.count(":") == 2 else s + ":00"


def validate(sched: dict) -> None:
    plans = sched.get("plans")
    assert isinstance(plans, list) and plans, "plans must be a non-empty list"
    for p in plans:
        assert p.get("days") and all(d in range(7) for d in p["days"]), f"bad days: {p.get('days')}"
        for s in p.get("shws", []):
            assert TIME_RE.match(s["time"]), f"bad time: {s['time']}"
            qty, temp = s.get("qty"), s.get("temp")
            if temp:  # temperature slot
                assert qty in (-1, 0), f"temperature slot must have qty -1/0: {s}"
                assert 40 <= temp <= 80, f"temp out of range 40-80: {s}"
            else:  # showers slot
                assert 1 <= qty <= 5, f"showers slot needs qty 1-5: {s}"
        times = [s["time"] for s in p.get("shws", [])]
        assert len(times) == len(set(times)), f"duplicate times in plan: {times}"


def fmt(sched: dict) -> str:
    out = []
    for p in sched.get("plans", []):
        slots = ", ".join(f"{s['time'][:5]}->{s['temp']:g}C" if s.get("temp") else f"{s['time'][:5]}->{s['qty']} tuša"
                          for s in p.get("shws", []))
        out.append(f"days {p['days']}: {slots}")
    return " | ".join(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("gw")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--move", nargs=2, metavar=("FROM", "TO"))
    g.add_argument("--restore")
    g.add_argument("--file")
    a = ap.parse_args()

    api = from_env()
    api.login()
    st, current = api.schedule(a.gw)
    if st != 200 or not isinstance(current, dict):
        print(f"ABORT: cannot read schedule (HTTP {st})")
        return 1

    BACKUP_DIR.mkdir(exist_ok=True)
    bfile = BACKUP_DIR / f"schedule_{a.gw}_{dt.datetime.now():%Y-%m-%d_%H%M%S}_pre-write.json"
    bfile.write_text(json.dumps({"gw": a.gw, "schedule": current}, indent=2), encoding="utf-8")
    print("backup :", bfile)
    print("before :", fmt(current))

    if a.move:
        src, dst = hhmmss(a.move[0]), hhmmss(a.move[1])
        new = copy.deepcopy(current)
        hits = 0
        for p in new["plans"]:
            for s in p["shws"]:
                if s["time"] == src:
                    s["time"] = dst
                    hits += 1
            p["shws"].sort(key=lambda s: s["time"])
        if not hits:
            print(f"ABORT: no slot at {src}")
            return 1
    else:
        raw = json.loads(pathlib.Path(a.restore or a.file).read_text(encoding="utf-8"))
        new = raw.get("schedule", raw)
        new = {"plans": new["plans"]}

    validate(new)
    if new == current:
        print("nothing to change")
        return 0
    print("sending:", fmt(new))
    code, body = api._request("POST", f"velis/timeProgs/{a.gw}", new)
    print(f"POST -> HTTP {code}: {body}")
    st, after = api.schedule(a.gw)
    print("after  :", fmt(after))
    ok = after == new
    print("VERIFIED:", ok)
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
