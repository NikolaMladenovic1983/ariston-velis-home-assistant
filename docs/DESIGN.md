# Design notes

Findings and decisions behind this setup. Everything here was verified against a Velis Pro WiFi 80 L
(`sys=4` VELIS, `wheType=8` Lux, `wheModelType=27`, firmware 25.04.00 / 014.115.27).

## 1. The device

- The Wi-Fi module is an Espressif ESP32. A full TCP port scan showed **no open ports**, so there is no local
  API. The boiler only keeps an outgoing MQTT/TLS connection to the Ariston cloud.
- So the only safe way to control it is the **Ariston NET cloud API**, the same one the official app uses.

## 2. Ariston NET API (`https://www.ariston-net.remotethermo.com/api/v2/`)

Login: `POST accounts/login {"usr", "pwd"}` → token in the `ar.authToken` response header.
User-Agent `RestSharp/106.11.7.0`. Re-login on HTTP 401/405. HTTP 429 means you are polling too often.

| Endpoint | Content |
|---|---|
| `GET velis/plants` | devices (`gw` = gateway ID), firmware, settings |
| `GET velis/medPlantData/{gw}` | `on`, `mode`, `temp`, `reqTemp`, `procReqTemp`, `antiLeg`, `heatReq`, `eco`, `rmTm`, `avShw` |
| `GET velis/plantData/{gw}` | showers: `avShw`, `maxAvShw`, `reqShw`, `state`, `rmTm` |
| `GET velis/medPlantData/{gw}/plantSettings` | `MedMaxSetpointTemperature` (65–80), `MedAntilegionellaOnOff`, `MedHeatingRate` |
| `GET / POST velis/timeProgs/{gw}` | **built-in schedule** (see below) |
| `GET remote/reports/{gw}/consSequencesApi8?usages=Dhw` | consumption series (see [Energy accounting](#energy-accounting)) |
| `GET remote/plants/{gw}/features` | `dhwProgSupported`, `hasMetering`, `hasDhwTimeProgTemperatures` |

Modes (Lux): `MANUAL = 1`, `PROGRAM = 5`, `BOOST = 9`.

### Schedule (time program)

The fustom library cannot read or write the Velis schedule, but the endpoint works.
`GET velis/timeProgs/{gw}` returns:

```json
{"plans": [{"days": [1, 2, 3, 4, 5, 6, 0],
            "shws": [{"time": "06:00:00", "qty": -1, "temp": 75.0}]}]}
```

- `days`: 0 = Sunday … 6 = Saturday. Days with the same slots are grouped into one plan.
- A slot is **either** temperature-based (`temp` > 0, `qty` = -1) **or** showers-based (`qty` 1–5, `temp` 0).
- The time means *"water ready at this time"*: the boiler decides when to start heating on its own.
- **Writing:** `POST velis/timeProgs/{gw}` with the same structure → `{"success": true}`. This was verified
  in three steps: first a neutral write of the unchanged schedule, then a visible change checked in the
  official app, then a restore.

The integration's write path is: backup (last 50 kept in `/config/ariston_schedule_backups`) → validate →
POST → re-read → compare. Pure logic lives in `schedule_model.py` (tested).

## 3. Safety rules

1. No firmware, hardware or network changes to the boiler. Rejected options: a local MQTT broker
   (fustom/broker-ari, which also fails TLS validation on newer firmware), ESP32 flashing, UART sniffing,
   opening the unit.
2. Only values that the official app allows.
3. Back up before every schedule write and verify after it.
4. **Anti-legionella stays on.** No automation switches it off, the dashboard shows it as text only, and an
   alert fires if it is switched off by any means.
5. `MedMaxSetpointTemperature` is never changed by automations.

## 4. Dual tariff

The time in a slot means "ready by". A single slot **06:00 → 75 °C** makes the boiler heat during the cheap
night tariff (00:00–06:00) and carry the heat through the day. Manual heating (dashboard button or Google
Home) heats immediately and then returns to PROGRAM. This happens when the target is reached, when the water
is already hot enough, when the boiler stops heating, or after 4 h at the latest. Without it the boiler
would keep holding the manual temperature all day at the expensive tariff.

<a id="energy-accounting"></a>
## 5. Energy accounting

### The problem

`consSequencesApi8` returns, among others, `p=1`: **24 hourly values without any timestamps**. Snapshots
taken over a day showed that:

- the series does **not** advance every hour. In one case 4 hours passed with only 2 shifts, and in another
  20 minutes of continuous heating changed nothing;
- new data arrives in batches, roughly two hours of heating at once (for example +2.975 kWh appeared
  10 minutes after the previous poll);
- `p=2` (7 days) lags as well.

Mapping "last element = current hour" therefore labels values with the wrong hours, **counts already
counted values again** (they slide into "new" hours), misses fresh consumption, and assigns energy to the
wrong tariff. The fustom "total energy" sensor is a rolling 24 h value labelled as "since midnight", so it
cannot be used for the Energy dashboard either.

### The solution (`energy_model.py`)

1. **Total kWh: snapshot diff.** Compare the previous and the current series and find the shift `s`. In a
   valid shift no overlapping slot decreases. Among the valid shifts, pick the one with the most identical
   non-zero slots, with ties going to the smallest shift. New energy = `sum(current) − sum(previous[s:])`.
   Each kWh is counted exactly once, regardless of the delay. Only a meaningless comparison is rejected: the
   whole window replaced with no overlap while the previous window had data. A time-based plausibility
   check ("kWh ≤ power × time since last poll") was tried and wrongly rejected every real batch.
2. **Tariff split: heating segments.** HA records wall-clock heating periods from `binary_sensor.bojler_greje`.
   Each new batch consumes these segments FIFO (kWh ÷ power = hours) and is split by the tariff of those
   minutes. The heating power is learned from the data (kWh ÷ heating hours, limited to 1–2.5 kW, default
   1.5 kW). If no segment is known (for example HA was down), the remainder goes to the tariff of the moment
   it was observed.
3. **Cost.** kWh × tariff price **at the moment of counting** (from `input_number` helpers). It is stored in
   the ledger (`.storage/ariston_schedule.<gw>.energy`), so it never resets on restart. Energy's own
   `*_cost` sensors do reset, which broke a template that summed them.
4. The 24 h / 7-day / cheap-share sensors are built from the ledger batches. The raw series is kept only as
   an attribute (`ariston_serija`) for diagnostics.

Limitations: energy shows up in the hour Ariston reports it (1–3 h late), not the hour of heating. Daily and
monthly totals are correct, except around midnight.

### Statistics

The cumulative sensors are `total_increasing` (kWh) and `total` (cost, a monetary sensor). The Energy
dashboard uses them directly: grid source = kWh sensor, `stat_cost` = cost sensor.

Some HA cards take the **first recorded value as the baseline**. If the ledger already has a value when the
statistics start, add an initial zero row (`recorder/import_statistics`). Otherwise "this month" misses the
starting amount.

## 6. Excel export

The HA frontend's "Download data" always produces CSV and cannot be changed. The integration therefore
writes its own `.xlsx` with `xlsx_writer.py`, which needs no dependencies. The workbook has these sheets:
by month, by day, by hour, and info. Each has a frozen header, filters, real date and number types, and a
totals row with `SUM` formulas.

- The file is written to `/config/www/bojler-<random token>/<localized file name>.xlsx`.
- The link is in the `url` attribute of `sensor.bojler_raspored_excel_izvestaj`.
- `/local` is not behind HA login. The random token makes the URL unguessable, so keep HA reachable only
  through your private network.

## 7. Dashboard

- The dashboard has tabs instead of collapsible sections. Sections toggled by `input_boolean` helpers are
  shared by every device and need a server round-trip per tap.
- The gauge shows the temperature. Segment labels would replace the number, so the gauge has none.
- "Cilj" (target) is the real target:
  - in MANUAL/BOOST mode, the set temperature;
  - in PROGRAM mode, the temperature of the next slot. Here the water heater's `temperature` attribute is an
    inactive manual value, and the fustom "program requested temperature" shows 16 °C between slots.
- `binary_sensor.bojler_formular_razlikuje` warns when the schedule form differs from the schedule stored in
  the boiler, before anyone presses Apply.
- **Live tariff cards** (`custom:bojler-tariff-table`, `custom:bojler-tariff-chart`, in `frontend/bojler-cards.js`)
  replace `energy-sources-table` / `energy-usage-graph`, which read only completed hours (long-term statistics).
  - They follow the same period picker, using the collection `hass.connection._energy_<dashboard path>`.
  - They read `recorder/statistic_during_period`, which includes 5-minute statistics, and refresh every minute.
  - The chart adds the part not yet in hourly statistics to the bar of the current hour/day/month.
- Energy cards have no color per source. The theme sets `--energy-grid-consumption-color-0/-1` and defines
  both `light` and `dark` modes, because a theme without `modes` forces light mode.

## 8. Internationalisation

- **Single source:** `i18n/<lang>.yaml` (en, de, fr, it, es, ru, sr-Latn), with `en` as the reference.
  `tools/i18n_build.py` validates the catalogs (same keys, same `{placeholders}`) and generates every
  output. Serbian Cyrillic (`sr`) is transliterated from `sr-Latn`. Placeholders, units (`°C`, `kWh`, `h`),
  brand names, paths and code samples stay in Latin script.
- **Dashboard:** the YAML dashboard only says `strategy: {type: custom:bojler}`.
  - The integration serves `frontend/bojler-dashboard.js` + `frontend/i18n.js` as a static path and
    registers it with `frontend.add_extra_js_url`.
  - The strategy builds the whole dashboard at runtime in the language of the user's profile
    (`hass.locale.language`, with English as the fallback). Every card gets an explicit translated name.
  - View paths, entity IDs and actions are identical in every language, which a test checks.
- **Integration:**
  - Entity and device names, config/options flows, services and exceptions use standard HA translations
    (`translation_key`). Errors are raised with translation keys (`ScheduleError(code, **placeholders)`).
  - Entity IDs are fixed in code, so they don't depend on the HA system language.
  - Attributes are language-neutral (days `mon`…`sun`).
- **Backend texts** (notifications, persistent notifications, the Excel report): the backend does not know
  each user's language, so they use `input_select.bojler_jezik`. Notifications use a generated Jinja macro
  (`custom_templates/bojler_i18n.jinja`); the Excel export reads `i18n_data/<lang>.json`.

## 9. Google Home

- Only `switch.bojler_rucno_grejanje` is exposed, as "Boiler". Turning it on from Google always heats to
  70 °C: an automation on the `google_assistant_command` event sets the target slider before the command runs.
- **Never use `optimistic: true`** on that template switch. It sets `assumed_state`, and Google then treats
  the switch as `commandOnlyOnOff`, which shows no state.
- The Ariston cloud needs 3–5 s to change the mode, but Google waits at most 2 s. The switch therefore shows
  the last request (`input_boolean.bojler_rucno_zahtev`) for 90 s, and the real mode after that.
- **Report State** (HomeGraph API with a service account) is required. Without it, Google Home shows the
  old state for about 30 s after every command.

## 10. Home Assistant quirks (2026.11)

- HTTP and reverse-proxy settings live in `.storage/http`, and a YAML `http:` block is ignored. Change them
  via websocket `http/config/configure` + `http/config/promote` (`tools/ha_http_config.py`).
- Requests to `/api/google_assistant` without a token count as failed logins, and `ip_ban` blocks the IP
  after 5. Don't health-check that path.
