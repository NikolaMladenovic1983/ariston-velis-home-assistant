"""Async client for the Ariston NET cloud API (only what ariston_schedule needs)."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import aiohttp

from .const import API_URL, USER_AGENT

_LOGGER = logging.getLogger(__name__)


class AristonAuthError(Exception):
    """Wrong credentials."""


class AristonApiError(Exception):
    """Communication/API error."""


class AristonScheduleApi:
    def __init__(self, session: aiohttp.ClientSession, username: str, password: str) -> None:
        self._session = session
        self._username = username
        self._password = password
        self._token: str | None = None
        self._lock = asyncio.Lock()

    async def _raw(self, method: str, path: str, body: Any = None, auth: bool = True) -> tuple[int, Any]:
        headers = {"User-Agent": USER_AGENT, "Content-Type": "application/json"}
        if auth and self._token:
            headers["ar.authToken"] = self._token
        try:
            async with self._session.request(
                method, API_URL + path, json=body, headers=headers,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.content_type == "application/json":
                    data = await resp.json()
                else:
                    data = await resp.text()
                return resp.status, data
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise AristonApiError(f"{method} {path}: {err}") from err

    async def login(self) -> None:
        status, data = await self._raw(
            "POST", "accounts/login", {"usr": self._username, "pwd": self._password}, auth=False
        )
        if status in (400, 401, 403) or (status == 200 and not isinstance(data, dict)):
            raise AristonAuthError("Pogrešan email ili lozinka")
        if status != 200 or "token" not in data:
            raise AristonApiError(f"login HTTP {status}")
        self._token = data["token"]

    async def request(self, method: str, path: str, body: Any = None) -> Any:
        async with self._lock:
            if not self._token:
                await self.login()
            status, data = await self._raw(method, path, body)
            if status in (401, 405):  # token expired -> re-login once
                await self.login()
                status, data = await self._raw(method, path, body)
            if status == 429:
                raise AristonApiError("Ariston ograničenje broja zahteva (429) — pokušaj kasnije")
            if status != 200:
                raise AristonApiError(f"{method} {path} -> HTTP {status}: {str(data)[:200]}")
            return data

    async def plants(self) -> list[dict[str, Any]]:
        return await self.request("GET", "velis/plants") or []

    async def get_schedule(self, gw: str) -> dict[str, Any]:
        return await self.request("GET", f"velis/timeProgs/{gw}")

    async def set_schedule(self, gw: str, schedule: dict[str, Any]) -> None:
        data = await self.request("POST", f"velis/timeProgs/{gw}", schedule)
        if not (isinstance(data, dict) and data.get("success")):
            raise AristonApiError(f"Ariston nije potvrdio upis rasporeda: {data}")

    async def get_consumption(self, gw: str) -> list[dict[str, Any]]:
        return await self.request("GET", f"remote/reports/{gw}/consSequencesApi8?usages=Dhw") or []
