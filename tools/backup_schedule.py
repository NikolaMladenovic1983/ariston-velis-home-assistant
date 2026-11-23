"""Save the boiler's current built-in schedule + state to backups/ (read-only).

Usage:  python -I tools/backup_schedule.py
"""
from __future__ import annotations

import datetime as dt
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from ariston_client import from_env  # noqa: E402

BACKUP_DIR = pathlib.Path(__file__).resolve().parent.parent / "backups"


def main() -> int:
    api = from_env()
    api.login()
    st, plants = api.plants()
    if st != 200 or not plants:
        print(f"Cannot list plants: HTTP {st}")
        return 1
    BACKUP_DIR.mkdir(exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y-%m-%d_%H%M%S")
    for p in plants:
        gw = p["gw"]
        snap = {"taken_at": stamp, "gw": gw, "name": p.get("name"),
                "wheType": p.get("wheType"), "fw": p.get("gwFwVer")}
        for key, fn in (("schedule", api.schedule), ("state", api.state),
                        ("settings", api.settings)):
            code, data = fn(gw)
            snap[key] = data if code == 200 else {"error": code}
        out = BACKUP_DIR / f"schedule_{gw}_{stamp}.json"
        out.write_text(json.dumps(snap, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"Saved {out}")
        for plan in (snap["schedule"] or {}).get("plans", []):
            slots = ", ".join(f"{s['time'][:5]} -> {s['temp']:g} C" for s in plan.get("shws", []))
            print(f"  days {plan.get('days')}: {slots}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
