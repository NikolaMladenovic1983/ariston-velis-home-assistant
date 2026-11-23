# Operations

Commands assume the repository is checked out at `~/ariston` on the server and Home Assistant runs as the
`homeassistant` container. Replace `<host>` with your Tailscale machine name (`<machine>.<tailnet>.ts.net`).

## Daily health check

```bash
cd ~/ariston && docker compose ps
docker logs --since 24h homeassistant 2>&1 | grep -E "ERROR" | tail -10
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8123/
```

Everything is fine when:

- `STATUS` shows `Up …`;
- there are no `ERROR` lines;
- the curl returns `200`.

The boiler keeps heating on its built-in schedule even while HA is down.

| Symptom | What to do |
|---|---|
| `ERROR` lines | read them; `ariston_schedule` errors usually mean the Ariston cloud was briefly unavailable (HTTP 500/429) |
| `000` or anything other than `200` | `docker compose restart`, wait ~30 s, check again |
| container not `Up` | `docker compose up -d` |

Energy accounting log lines:

- `Consumption series not comparable …` (WARNING): a batch was skipped. Investigate if it repeats.
- `Energy +x kWh …` (INFO): a batch was counted. To see these lines, enable INFO logging for
  `custom_components.ariston_schedule`.

## Restart

```bash
cd ~/ariston && docker compose restart
```

A restart takes about 30 s and has no effect on the boiler.

## Remote access (Tailscale Serve)

```bash
sudo tailscale serve --bg http://127.0.0.1:8123      # https://<host> inside your tailnet only
```

HA must trust the proxy. Run `tools/ha_http_config.py` inside the container; it sets `use_x_forwarded_for`,
`trusted_proxies` and `ip_ban`:

```bash
docker exec -i -e HA_TOKEN=<token> homeassistant python3 - proxy < tools/ha_http_config.py
docker exec -i -e HA_TOKEN=<token> homeassistant python3 - promote < tools/ha_http_config.py
```

## Google Home (optional)

1. In the Google Home Developer Console, create a cloud-to-cloud integration of type Switch, using the
   [HA manual Google Assistant guide](https://www.home-assistant.io/integrations/google_assistant/).
   No billing account is needed.
2. Put the project ID in `secrets.yaml` (`google_project_id`). Then create a service account with the
   HomeGraph API enabled and save its key as `ha-config/SERVICE_ACCOUNT.json` (this file is gitignored).
3. **Public endpoint via Tailscale Funnel.** Open the whole HA instance only while linking, then restrict
   the Funnel to the two paths Google needs:

   ```bash
   # while linking in the Google Home app:
   sudo tailscale funnel --bg --https=8443 http://127.0.0.1:8123
   # after linking — only the Google endpoints stay public:
   sudo tailscale funnel --https=8443 off
   sudo tailscale funnel --bg --https=8443 --set-path=/api/google_assistant http://127.0.0.1:8123/api/google_assistant
   sudo tailscale funnel --bg --https=8443 --set-path=/auth/token http://127.0.0.1:8123/auth/token
   ```

4. **Check it:**

   ```bash
   sudo tailscale funnel status | grep -E "Funnel on|google_assistant|auth/token"
   curl -s -o /dev/null -m 20 -w "%{http_code}\n" https://<host>:8443/      # expect 404 (HA itself not exposed)
   docker logs --since 24h homeassistant 2>&1 | grep -iE "google_assistant|homegraph" | grep -iE "error|warning"
   ```

   Don't curl `/api/google_assistant`: a request without a token counts as a failed login, and the IP is
   banned after 5.

5. **Family members:**
   - Invite them to your Google Home household and put "Boiler" in a room.
   - Alternatively, give each of them their own non-admin HA user and link again using the procedure above.

| Symptom | What to do |
|---|---|
| no `Funnel on` or the paths are missing | redo step 3 |
| `000` instead of `404` | `sudo systemctl restart tailscaled`, wait ~30 s |
| `homegraph` / `403` errors | the service-account key is invalid → create a new key |
| Google Home shows "offline" | check HA first (`200`), then the Funnel; relink in the Google Home app if needed |

## Schedule backups

- Every schedule write made by the integration saves the previous schedule to
  `ha-config/ariston_schedule_backups/` (the last 50 are kept).
- To undo the last change, use **Vrati prethodni** (undo) on the Raspored tab, or the
  `ariston_schedule.restore_backup` service.
- From outside HA, the read-only and backup tools in `tools/` use the credentials from `.env`:

```bash
python tools/backup_schedule.py
python tools/schedule_set.py <gateway_id> --restore backups/<file>.json
```

## Updating

1. Pin the new Home Assistant version in `docker-compose.yml`.
2. Run `docker compose pull && docker compose up -d`.
3. Check the logs.
4. Update the fustom integration separately (HACS or a new release zip).
