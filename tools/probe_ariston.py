"""Read-only diagnostic: dump everything the API returns for each boiler.

Usage:  python -I tools/probe_ariston.py [out.json]
"""
from __future__ import annotations

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from ariston_client import from_env  # noqa: E402


def main() -> int:
    out = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else \
        pathlib.Path(__file__).resolve().parent / "probe_result.json"
    api = from_env()
    api.login()
    results = {}
    st, plants = api.plants()
    results["velis/plants"] = {"status": st, "data": plants}
    for p in plants if isinstance(plants, list) else []:
        gw = p["gw"]
        print(f"gw={gw} name={p.get('name')} sys={p.get('sys')} wheType={p.get('wheType')}")
        for path in (f"velis/medPlantData/{gw}", f"velis/plantData/{gw}",
                     f"velis/medPlantData/{gw}/plantSettings", f"velis/timeProgs/{gw}",
                     f"remote/plants/{gw}/features",
                     f"remote/reports/{gw}/consSequencesApi8?usages=Dhw"):
            code, data = api.get(path)
            results[path] = {"status": code, "data": data}
            print(f"  GET {path} -> {code}")
    out.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Saved {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
