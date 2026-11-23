"""Minimal READ-ONLY client for the Ariston NET cloud API.

Only performs the login POST and GET requests. It has no methods that send
commands to the water heater.
"""
from __future__ import annotations

import json
import pathlib
import urllib.error
import urllib.request

BASE = "https://www.ariston-net.remotethermo.com/api/v2/"
USER_AGENT = "RestSharp/106.11.7.0"
ENV_FILE = pathlib.Path(__file__).resolve().parent.parent / ".env"


def load_env(path: pathlib.Path = ENV_FILE) -> dict[str, str]:
    env: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


class AristonReadOnly:
    def __init__(self, user: str, password: str, timeout: int = 20):
        self._user = user
        self._password = password
        self._timeout = timeout
        self._token: str | None = None

    def _request(self, method: str, path: str, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(BASE + path, data=data, method=method)
        req.add_header("User-Agent", USER_AGENT)
        req.add_header("Content-Type", "application/json")
        if self._token:
            req.add_header("ar.authToken", self._token)
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                txt = resp.read().decode("utf-8", "ignore")
                return resp.status, (json.loads(txt) if txt else None)
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "ignore")[:300]
        except Exception as e:  # timeouts, DNS, ...
            return -1, repr(e)

    def login(self) -> None:
        st, data = self._request(
            "POST", "accounts/login", {"usr": self._user, "pwd": self._password}
        )
        if st != 200 or not isinstance(data, dict) or "token" not in data:
            raise RuntimeError(f"Login failed: HTTP {st}")
        self._token = data["token"]

    def get(self, path: str):
        if not self._token:
            self.login()
        st, data = self._request("GET", path)
        if st == 405:  # token expired -> re-login once
            self.login()
            st, data = self._request("GET", path)
        return st, data

    # convenience readers
    def plants(self):
        return self.get("velis/plants")

    def state(self, gw: str):
        return self.get(f"velis/medPlantData/{gw}")

    def showers(self, gw: str):
        return self.get(f"velis/plantData/{gw}")

    def settings(self, gw: str):
        return self.get(f"velis/medPlantData/{gw}/plantSettings")

    def schedule(self, gw: str):
        return self.get(f"velis/timeProgs/{gw}")

    def consumption(self, gw: str):
        return self.get(f"remote/reports/{gw}/consSequencesApi8?usages=Dhw")

    def features(self, gw: str):
        return self.get(f"remote/plants/{gw}/features")


def from_env() -> AristonReadOnly:
    env = load_env()
    return AristonReadOnly(env["ARISTON_USER"], env["ARISTON_PASS"])
