/**
 * Dashboard strategy "custom:bojler" — the whole Bojler dashboard, generated at runtime in the language of
 * the user's Home Assistant profile (en, de, fr, it, es, ru, sr-Latn, sr; fallback English).
 *
 * Use it in a dashboard:      strategy: { type: custom:bojler }
 * Force a language (testing): strategy: { type: custom:bojler, language: de }
 *
 * Texts: ./i18n.js — generated from i18n/<lang>.yaml by tools/i18n_build.py (do not edit i18n.js by hand).
 * Served by the ariston_schedule integration at /ariston_schedule_frontend/ and loaded on every page.
 */
const VERSION_QUERY = new URL(import.meta.url).search; // cache-busting ?v=<hash> of this file
const { CATALOGS } = await import(`./i18n.js${VERSION_QUERY}`);

export const DASH = "/bojler-dash";
const THEME = "Bojler";

/** Pick the catalog for a HA language code ("de", "de-CH", "sr-Latn", "sr", "pt-BR" → en …). */
export function pickLanguage(code) {
  if (!code) return "en";
  if (CATALOGS[code]) return code;
  const base = code.split("-")[0];
  if (base === "sr") return /latn/i.test(code) ? "sr-Latn" : "sr";
  return CATALOGS[base] ? base : "en";
}

/** Replace {name} placeholders. */
const fmt = (s, p = {}) => s.replace(/\{(\w+)\}/g, (m, k) => (k in p ? p[k] : m));
/** A Jinja string literal (JSON is valid Jinja). */
const J = (s) => JSON.stringify(s);

const nav = (path) => ({ action: "navigate", navigation_path: `${DASH}/${path}` });
const perform = (action, extra = {}) => ({ action: "perform-action", perform_action: action, ...extra });
const confirm = (tap, text) => ({ ...tap, confirmation: { text } });
const vis = (entity, state) => [{ condition: "state", entity, state }];
const visNot = (entity, state_not) => [{ condition: "state", entity, state_not }];
const heading = (text, icon, extra = {}) => ({ type: "heading", heading: text, heading_style: "title", icon, ...extra });
const full = { columns: "full" };

export function buildDashboard(T, currency) {
  const cur = currency || "";

  // -------------------------------------------------------------------- 1. Boiler (status + control)
  const presets = [55, 60, 65, 70, 75].map((v) => ({
    type: "button",
    name: `${v}°`,
    show_icon: false,
    tap_action: perform("input_number.set_value", {
      target: { entity_id: "input_number.bojler_rucno_temp" },
      data: { value: v },
    }),
  }));
  const stat = (entity, name, icon, period) => ({
    type: "statistic",
    entity,
    name,
    icon,
    stat_type: "change",
    period: { calendar: { period } },
    grid_options: { columns: 6 },
    tap_action: nav("potrosnja"),
  });
  const modeBadge = (mode, icon) => ({
    type: "entity",
    entity: "water_heater.bojler",
    name: `${T.badge_mode}: ${T["mode_" + mode]}`,
    icon,
    show_name: true,
    show_state: false,
    show_icon: true,
    tap_action: { action: "none" },
    visibility: vis("water_heater.bojler", mode),
  });

  const home = {
    title: T.tab_home,
    path: "bojler",
    icon: "mdi:water-boiler",
    theme: THEME,
    type: "sections",
    max_columns: 2,
    badges: [
      { type: "entity", entity: "binary_sensor.bojler_greje", name: T.badge_heating, show_name: true,
        show_state: true, state_content: "state", color: "deep-orange" },
      modeBadge("PROGRAM", "mdi:calendar-clock"),
      modeBadge("MANUAL", "mdi:hand-back-right"),
      modeBadge("BOOST", "mdi:rocket-launch"),
      { type: "entity", entity: "sensor.bojler_raspored_sledeci_termin", name: T.badge_next, show_name: true,
        icon: "mdi:calendar-clock", tap_action: nav("raspored") },
    ],
    sections: [
      {
        type: "grid",
        cards: [
          heading(T.h_water, "mdi:water-thermometer"),
          {
            type: "gauge", entity: "sensor.bojler_temperatura_vode", name: T.gauge_name, unit: "°C",
            min: 20, max: 80, needle: true,
            segments: [{ from: 20, color: "#4a90d9" }, { from: 40, color: "#f0ad4e" },
                       { from: 50, color: "#e8732a" }, { from: 65, color: "#d9534f" }],
            grid_options: { columns: 6, rows: 3 },
          },
          {
            type: "vertical-stack",
            grid_options: { columns: 6 },
            cards: [
              { type: "tile", entity: "sensor.bojler_dostupni_tusevi", name: T.showers, icon: "mdi:shower-head", color: "blue" },
              // target of the next schedule slot (with its time) — or the manual target
              { type: "tile", entity: "sensor.bojler_cilj", name: T.target, state_content: ["state", "vreme"],
                color: "orange", visibility: vis("water_heater.bojler", "PROGRAM") },
              { type: "tile", entity: "sensor.bojler_cilj", name: T.target, color: "deep-orange",
                visibility: visNot("water_heater.bojler", "PROGRAM") },
              { type: "tile", entity: "sensor.bojler_preostalo_vreme", name: T.time_to_heat, icon: "mdi:timer-sand",
                color: "deep-orange", visibility: vis("binary_sensor.bojler_greje", "on") },
            ],
          },
        ],
      },
      {
        type: "grid",
        cards: [
          heading(T.h_manual, "mdi:fire", {
            badges: [{ type: "entity", entity: "input_number.bojler_rucno_temp", show_state: true, show_icon: true, color: "deep-orange" }],
          }),
          {
            type: "tile", entity: "switch.bojler_rucno_grejanje", name: T.manual_switch, state_content: "state",
            color: "deep-orange", grid_options: full,
            tap_action: confirm({ action: "toggle" }, T.manual_confirm),
            icon_tap_action: confirm({ action: "toggle" }, T.manual_confirm_short),
          },
          { type: "horizontal-stack", grid_options: full, cards: presets },
          {
            type: "tile", entity: "input_number.bojler_rucno_temp", name: T.heat_to, icon: "mdi:thermometer-chevron-up",
            color: "deep-orange", grid_options: full, features: [{ type: "numeric-input", style: "slider" }],
          },
        ],
      },
      {
        type: "grid",
        cards: [
          heading(T.h_cost, "mdi:cash-multiple", {
            tap_action: nav("potrosnja"),
            badges: [{ type: "entity", entity: "sensor.bojler_raspored_udeo_jeftine_tarife_24h", name: T.cheap_24h,
                       show_name: true, show_state: true, color: "green" }],
          }),
          stat("sensor.bojler_raspored_trosak_ukupno", T.today, "mdi:cash", "day"),
          stat("sensor.bojler_raspored_ukupna_potrosnja", T.today, "mdi:lightning-bolt", "day"),
          stat("sensor.bojler_raspored_trosak_ukupno", T.this_month, "mdi:cash-multiple", "month"),
          stat("sensor.bojler_raspored_ukupna_potrosnja", T.this_month, "mdi:lightning-bolt", "month"),
        ],
      },
    ],
  };

  // -------------------------------------------------------------------- 2. Consumption
  const monthGraph = (title, cheap, expensive) => ({
    type: "statistics-graph", title, grid_options: full, chart_type: "bar", period: "month",
    days_to_show: 365, stat_types: ["change"],
    entities: [{ entity: cheap, name: T.cheap, color: "green" }, { entity: expensive, name: T.expensive, color: "orange" }],
  });
  const excelEntity = "sensor.bojler_raspored_excel_izvestaj";
  const excelTime = "{{ as_timestamp(states(e)) | timestamp_custom('%d.%m. %H:%M') }}";
  const usage = {
    title: T.tab_usage,
    path: "potrosnja",
    icon: "mdi:chart-bar",
    theme: THEME,
    type: "sections",
    max_columns: 2,
    sections: [
      {
        type: "grid",
        cards: [
          heading(T.h_usage, "mdi:chart-bar"),
          { type: "energy-date-selection", grid_options: full },
          { type: "energy-sources-table", title: T.table_title, grid_options: full },
          { type: "energy-usage-graph", title: T.usage_graph, grid_options: full },
          {
            type: "markdown", grid_options: { columns: 9 },
            content:
              `{% set e = '${excelEntity}' %}{% set u = state_attr(e, 'url') %}` +
              `{% if u %}**[📥 ${T.excel_link}]({{ u }})**<br>` +
              `<small>${fmt(T.excel_desc, { time: excelTime })}</small>` +
              `{% else %}${T.excel_pending}{% endif %}`,
          },
          {
            type: "button", icon: "mdi:refresh", name: T.refresh, show_state: false,
            grid_options: { columns: 3, rows: 2 }, tap_action: perform("ariston_schedule.export_xlsx"),
          },
        ],
      },
      {
        type: "grid",
        cards: [
          heading(T.h_monthly, "mdi:calendar-month"),
          { ...stat("sensor.bojler_raspored_trosak_ukupno", T.this_month, "mdi:cash-multiple", "month"), tap_action: undefined },
          { ...stat("sensor.bojler_raspored_ukupna_potrosnja", T.this_month, "mdi:lightning-bolt", "month"), tap_action: undefined },
          monthGraph(fmt(T.cost_by_month, { cur }), "sensor.bojler_raspored_trosak_jeftina_tarifa",
                     "sensor.bojler_raspored_trosak_skupa_tarifa"),
          monthGraph(T.kwh_by_month, "sensor.bojler_raspored_ukupna_potrosnja_jeftina_tarifa",
                     "sensor.bojler_raspored_ukupna_potrosnja_skupa_tarifa"),
        ],
      },
      {
        type: "grid",
        cards: [
          heading(T.h_habits, "mdi:chart-timeline-variant"),
          { type: "tile", entity: "sensor.bojler_raspored_udeo_jeftine_tarife_24h", name: T.cheap_share, color: "green", grid_options: full },
          {
            type: "history-graph", title: T.temp_48h, grid_options: full, hours_to_show: 48,
            entities: [
              { entity: "sensor.bojler_temperatura_vode", name: T.water },
              { entity: "sensor.bojler_cilj", name: T.target },
              { entity: "binary_sensor.bojler_greje", name: T.heating },
            ],
          },
          { type: "markdown", grid_options: full, content: `<small>${T.lag_note}</small>` },
        ],
      },
    ],
  };

  // -------------------------------------------------------------------- 3. Schedule
  const sched = "sensor.bojler_raspored_raspored";
  const form = (title, g) => ({
    type: "entities", title, grid_options: full,
    entities: [
      { entity: `input_datetime.bojler_${g}_t1`, name: T.slot1 },
      { entity: `input_number.bojler_${g}_temp1`, name: T.temp1 },
      { entity: `input_boolean.bojler_${g}_t2_ukljucen`, name: T.second_slot },
      { type: "conditional", conditions: [{ entity: `input_boolean.bojler_${g}_t2_ukljucen`, state: "on" }],
        row: { entity: `input_datetime.bojler_${g}_t2`, name: T.slot2 } },
      { type: "conditional", conditions: [{ entity: `input_boolean.bojler_${g}_t2_ukljucen`, state: "on" }],
        row: { entity: `input_number.bojler_${g}_temp2`, name: T.temp2 } },
    ],
  });
  const schedButton = (name, icon, script, confirmText) => ({
    type: "button", name, icon, show_state: false, grid_options: { columns: 4, rows: 2 },
    tap_action: confirmText ? confirm(perform(script), confirmText) : perform(script),
  });
  const schedule = {
    title: T.tab_schedule,
    path: "raspored",
    icon: "mdi:calendar-clock",
    theme: THEME,
    type: "sections",
    max_columns: 2,
    sections: [
      {
        type: "grid",
        cards: [
          heading(T.h_schedule, "mdi:calendar-clock"),
          {
            type: "markdown", grid_options: full,
            content:
              `{% set r = state_attr('${sched}', 'raspored') or {} %}` +
              `{% set wd = r.get('mon', []) %}{% set we = r.get('sat', []) %}` +
              `{% if wd == we %}### ${T.every_day}: {{ wd | join(' · ') or '—' }}\n` +
              `{% else %}**${T.weekdays}:** {{ wd | join(' · ') or '—' }}<br>**${T.weekend}:** {{ we | join(' · ') or '—' }}{% endif %}\n\n` +
              `<small>${T.schedule_note} ${T.cheap_tariff}: {{ state_attr('${sched}', 'jeftina_tarifa') }}. ` +
              `${T.last_change}: {{ state_attr('${sched}', 'poslednja_izmena') or '—' }}</small>`,
          },
          { type: "tile", entity: "sensor.bojler_raspored_sledeci_termin", name: T.next_slot, grid_options: full },
        ],
      },
      {
        type: "grid",
        cards: [
          heading(T.h_edit, "mdi:pencil"),
          {
            type: "tile", entity: "binary_sensor.bojler_formular_razlikuje", name: T.form_diff, hide_state: true,
            icon: "mdi:alert", color: "amber", grid_options: full,
            visibility: vis("binary_sensor.bojler_formular_razlikuje", "on"),
            tap_action: confirm(perform("script.bojler_ucitaj_raspored"), T.form_diff_confirm),
          },
          { type: "markdown", grid_options: full, content: `<small>${T.form_diff_note}</small>`,
            visibility: vis("binary_sensor.bojler_formular_razlikuje", "on") },
          form(T.weekdays_title, "radni"),
          form(T.weekend_title, "vikend"),
          schedButton(T.load_current, "mdi:download", "script.bojler_ucitaj_raspored"),
          schedButton(T.apply, "mdi:upload", "script.bojler_primeni_raspored", T.apply_confirm),
          schedButton(T.undo, "mdi:undo", "script.bojler_vrati_prethodni_raspored", T.undo_confirm),
        ],
      },
    ],
  };

  // -------------------------------------------------------------------- 4. Settings
  const settings = {
    title: T.tab_settings,
    path: "podesavanja",
    icon: "mdi:cog",
    theme: THEME,
    type: "sections",
    max_columns: 2,
    sections: [
      {
        type: "grid",
        cards: [
          heading(T.h_prices, "mdi:cash"),
          {
            type: "entities", grid_options: full,
            entities: [
              { entity: "input_number.struja_cena_jeftina", name: fmt(T.price_cheap, { cur }) },
              { entity: "input_number.struja_cena_skupa", name: fmt(T.price_expensive, { cur }) },
              { entity: "input_select.bojler_jezik", name: T.notify_language, icon: "mdi:translate" },
            ],
          },
          { type: "markdown", grid_options: full, content: `<small>${T.price_note}</small>` },
          {
            type: "entities", title: T.totals_title, grid_options: full,
            entities: [
              { entity: "sensor.bojler_raspored_ukupna_potrosnja", name: T.total_kwh },
              { entity: "sensor.bojler_raspored_ukupna_potrosnja_jeftina_tarifa", name: T.total_cheap },
              { entity: "sensor.bojler_raspored_ukupna_potrosnja_skupa_tarifa", name: T.total_expensive },
              { entity: "sensor.bojler_raspored_trosak_ukupno", name: T.total_cost },
            ],
          },
        ],
      },
      {
        type: "grid",
        cards: [
          heading(T.h_advanced, "mdi:tune"),
          { type: "tile", entity: "water_heater.bojler", name: T.mode, grid_options: full,
            tap_action: { action: "none" }, icon_tap_action: { action: "none" } },
          { type: "button", name: T.back_to_schedule, icon: "mdi:calendar-clock", grid_options: { columns: 6, rows: 2 },
            tap_action: perform("script.bojler_program") },
          { type: "button", name: T.boost, icon: "mdi:rocket-launch", grid_options: { columns: 6, rows: 2 },
            tap_action: confirm(perform("script.bojler_boost"), T.boost_confirm) },
          { type: "tile", entity: "switch.bojler_ukljucen", name: T.power },
          { type: "tile", entity: "switch.bojler_eco", name: T.eco },
          { type: "tile", entity: "sensor.bojler_greske", name: T.errors, grid_options: full },
          // Text only — no tap and no more-info dialog (that dialog contains a toggle).
          {
            type: "markdown", grid_options: full,
            content:
              `**${T.antileg}:** {{ ${J(T.antileg_on)} if is_state('switch.bojler_antilegionela', 'on') else ${J(T.antileg_off)} }}<br>` +
              `**${T.max_temp}:** {{ states('number.bojler_max_temperatura') | int(0) }} °C<br>` +
              `<small>${T.readonly_note}</small>`,
          },
        ],
      },
    ],
  };

  return { title: T.title, views: [home, usage, schedule, settings] };
}

export class BojlerDashboardStrategy extends (globalThis.HTMLElement ?? class {}) {
  static async generate(config, hass) {
    const code = config.language || hass?.locale?.language || hass?.language || "en";
    const T = CATALOGS[pickLanguage(code)];
    return buildDashboard(T, hass?.config?.currency);
  }
}

if (globalThis.customElements && !customElements.get("ll-strategy-dashboard-bojler")) {
  customElements.define("ll-strategy-dashboard-bojler", BojlerDashboardStrategy);
}
