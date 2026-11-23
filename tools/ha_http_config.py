"""Get / set / promote Home Assistant HTTP config via the websocket API.

Runs INSIDE the HA container (uses its aiohttp):
  docker exec -e HA_TOKEN=... homeassistant python3 - get|proxy|promote < tools/ha_http_config.py

  get      show stable/pending config
  proxy    stage a pending config = stable + reverse proxy settings (HA restarts)
  promote  make the pending config stable (after verifying it works)
"""
import asyncio
import json
import os
import sys

import aiohttp

URL = "ws://127.0.0.1:8123/api/websocket"
TRUSTED_PROXIES = ["172.16.0.0/12", "127.0.0.1"]


async def main(cmd: str) -> None:
    async with aiohttp.ClientSession() as s, s.ws_connect(URL) as ws:
        await ws.receive_json()
        await ws.send_json({"type": "auth", "access_token": os.environ["HA_TOKEN"]})
        auth = await ws.receive_json()
        if auth.get("type") != "auth_ok":
            sys.exit(f"auth failed: {auth}")

        async def call(i, payload):
            await ws.send_json({"id": i, **payload})
            while True:
                m = await ws.receive_json()
                if m.get("id") == i:
                    return m

        cur = await call(1, {"type": "http/config"})
        if cmd == "get":
            print(json.dumps(cur["result"], indent=1))
            return
        if cmd == "promote":
            print(json.dumps(await call(2, {"type": "http/config/promote"})))
            return
        if cmd == "proxy":
            cfg = {k: v for k, v in cur["result"]["stable"].items()
                   if k not in ("created_at", "error", "error_message")}
            cfg.update({"use_x_forwarded_for": True, "trusted_proxies": TRUSTED_PROXIES,
                        "ip_ban_enabled": True, "login_attempts_threshold": 5})
            print("staging:", json.dumps(cfg))
            print(json.dumps(await call(2, {"type": "http/config/configure", "config": cfg})))
            return
        sys.exit("usage: get|proxy|promote")


asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "get"))
