"""Faza 3: NEUTRAL schedule write test.

Reads the boiler's current schedule and sends back EXACTLY the same content,
then re-reads and compares. Schedule content does not change.

Tries POST, and only if the server says the method/route is wrong
(404/405), tries PUT once with the same body. Nothing else is sent.

Usage:  python -I tools/schedule_neutral_write.py <gw>
"""
from __future__ import annotations

import json
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from ariston_client import from_env  # noqa: E402


def main() -> int:
    gw = sys.argv[1]
    api = from_env()
    api.login()

    st, before = api.schedule(gw)
    st2, state_before = api.state(gw)
    if st != 200 or not isinstance(before, dict) or not before.get("plans"):
        print(f"ABORT: cannot read current schedule (HTTP {st}): {before}")
        return 1
    print("BEFORE schedule:", json.dumps(before))
    print("BEFORE state   :", json.dumps({k: state_before.get(k) for k in ("on", "mode", "reqTemp", "temp")}))

    path = f"velis/timeProgs/{gw}"
    result = None
    for method in ("POST", "PUT"):
        code, body = api._request(method, path, before)  # identical payload
        if code == 405:  # could also be token expiry in this API -> re-login & retry once
            api.login()
            code, body = api._request(method, path, before)
        print(f"{method} {path} -> HTTP {code}; response: {json.dumps(body)[:500]}")
        result = (method, code)
        if code not in (404, 405):
            break

    st, after = api.schedule(gw)
    _, state_after = api.state(gw)
    print("AFTER schedule :", json.dumps(after))
    print("AFTER state    :", json.dumps({k: state_after.get(k) for k in ("on", "mode", "reqTemp", "temp")}))
    same = after == before
    print("SCHEDULE UNCHANGED:", same)
    print("WRITE RESULT:", result)
    return 0 if same else 2


if __name__ == "__main__":
    sys.exit(main())
