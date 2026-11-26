/**
 * Live tariff cards: "custom:bojler-tariff-table" and "custom:bojler-tariff-chart".
 *
 * They follow the period picker (energy-date-selection on the same view), show kWh and cost per tariff
 * side by side and refresh every minute. Data: the integration's statistics booked on the hour the boiler
 * actually heated (SERIES below). buildBuckets still adds any part of the period total that hourly rows do
 * not contain to the current bucket (harmless when there is none).
 */
const VERSION_QUERY = new URL(import.meta.url).search;
const { CATALOGS, languageOf } = await import(`./common.js${VERSION_QUERY}`);

// Statistics booked on the hour the boiler actually heated (imported by the integration, energy_stats.py).
// The cumulative sensors add energy only when Ariston reports it (1–3 h late), i.e. in the wrong hour.
export const SERIES = {
  kwh: { cheap: "ariston_schedule:energy_cheap", exp: "ariston_schedule:energy_expensive" },
  cost: { cheap: "ariston_schedule:cost_cheap", exp: "ariston_schedule:cost_expensive" },
};
const REFRESH_MS = 60_000;
const FALLBACK = { cheap: "#43a047", exp: "#ef6c00" };

// ------------------------------------------------------------------ pure helpers (unit-tested)

/** Bucket size for a period: ≤ 2 days → hour, ≤ 35 days → day, else month. */
export function bucketPeriod(start, end) {
  const days = (end - start) / 86_400_000;
  return days <= 2 ? "hour" : days <= 35 ? "day" : "month";
}

/** Local bucket start for a timestamp. */
export function bucketStart(ms, period) {
  const d = new Date(ms);
  if (period === "hour") d.setMinutes(0, 0, 0);
  else if (period === "day") d.setHours(0, 0, 0, 0);
  else { d.setDate(1); d.setHours(0, 0, 0, 0); }
  return d.getTime();
}

function nextBucket(ms, period) {
  const d = new Date(ms);
  if (period === "hour") d.setHours(d.getHours() + 1);
  else if (period === "day") d.setDate(d.getDate() + 1);
  else d.setMonth(d.getMonth() + 1);
  return d.getTime();
}

/**
 * Every bucket of [start, end) with cheap/exp kWh from long-term rows, plus the part of the period total
 * that long-term statistics do not contain yet (current hour) added to the bucket of `now`.
 * rows: {cheap: [{start, change}], exp: [...]}, totals: {cheap, exp} (whole period, incl. 5-minute data).
 */
export function buildBuckets(start, end, period, rows, totals, now = Date.now()) {
  const buckets = [];
  const index = new Map();
  for (let t = bucketStart(start, period); t < end; t = nextBucket(t, period)) {
    index.set(t, buckets.length);
    buckets.push({ t, cheap: 0, exp: 0 });
  }
  for (const k of ["cheap", "exp"]) {
    for (const r of rows[k] || []) {
      const i = index.get(bucketStart(r.start, period));
      if (i !== undefined) buckets[i][k] += r.change || 0;
    }
    const known = buckets.reduce((s, b) => s + b[k], 0);
    const live = (totals[k] || 0) - known;
    if (live > 0.0005 && buckets.length) {
      const i = index.get(bucketStart(Math.min(Math.max(now, start), end - 1), period));
      buckets[i ?? buckets.length - 1][k] += live;
    }
  }
  return buckets;
}

/** Nice axis maximum (1, 2, 2.5, 5 × 10^n). */
export function niceMax(v) {
  if (!(v > 0)) return 1;
  const p = 10 ** Math.floor(Math.log10(v));
  for (const m of [1, 2, 2.5, 5, 10]) if (v <= m * p) return m * p;
  return 10 * p;
}

/** Y axis with round ticks: step from (1, 2, 2.5, 5) × 10^n, at most ~5 ticks. */
export function axis(v) {
  const step = niceMax(Math.max(v, 1e-9) / 4);
  return { step, max: Math.max(step, step * Math.ceil(v / step - 1e-9)) };
}

// ------------------------------------------------------------------ data

async function fetchTotals(hass, start, end) {
  const call = (id) => hass.callWS({
    type: "recorder/statistic_during_period",
    statistic_id: id,
    fixed_period: { start_time: new Date(start).toISOString(), end_time: new Date(end).toISOString() },
    types: ["change"],
  }).then((r) => r?.change || 0);
  const [kc, kx, cc, cx] = await Promise.all([
    call(SERIES.kwh.cheap), call(SERIES.kwh.exp), call(SERIES.cost.cheap), call(SERIES.cost.exp),
  ]);
  return { kwh: { cheap: kc, exp: kx }, cost: { cheap: cc, exp: cx } };
}

async function fetchRows(hass, start, end, period, ids = SERIES.kwh) {
  const res = await hass.callWS({
    type: "recorder/statistics_during_period",
    start_time: new Date(start).toISOString(),
    end_time: new Date(end).toISOString(),
    statistic_ids: [ids.cheap, ids.exp],
    period,
    types: ["change"],
  });
  return { cheap: res[ids.cheap] || [], exp: res[ids.exp] || [] };
}

/** The last `n` calendar months including the current one. */
export function monthsRange(n = 12, now = Date.now()) {
  const e = new Date(now); e.setDate(1); e.setHours(0, 0, 0, 0); e.setMonth(e.getMonth() + 1);
  const s = new Date(e); s.setMonth(s.getMonth() - n);
  return { start: s.getTime(), end: e.getTime() };
}

function defaultRange() {
  const s = new Date(); s.setDate(1); s.setHours(0, 0, 0, 0);
  const e = new Date(s); e.setMonth(e.getMonth() + 1);
  return { start: s.getTime(), end: e.getTime() };
}

// ------------------------------------------------------------------ base card

const Base = globalThis.HTMLElement ?? class {};

class BojlerTariffCard extends Base {
  setConfig(config) {
    this._config = { ...config };
  }

  set hass(hass) {
    const first = !this._hass;
    this._hass = hass;
    if (first) this._start();
  }

  connectedCallback() {
    if (this._hass) this._start();
  }

  disconnectedCallback() {
    clearInterval(this._timer);
    clearInterval(this._attach);
    this._unsub?.();
    this._timer = this._attach = this._unsub = undefined;
    this._ro?.disconnect();
  }

  get T() {
    return CATALOGS[languageOf(this._config, this._hass)];
  }

  _color(k) {
    const v = getComputedStyle(this).getPropertyValue(`--energy-grid-consumption-color-${k === "cheap" ? 0 : 1}`).trim();
    return v || FALLBACK[k];
  }

  /** The period picker's collection: "_energy_<dashboard path>" by default (or config.collection_key). */
  _collection() {
    const conn = this._hass?.connection;
    if (!conn) return undefined;
    if (this._config.collection_key) return conn[`_${this._config.collection_key}`];
    const dash = (globalThis.location?.pathname || "").split("/")[1];
    return conn[`_energy_${dash}`] || conn._energy;
  }

  _start() {
    if (this._timer || !this.isConnected) return;
    this._range = this._range || defaultRange();
    if (this._config.range === "months") {          // fixed: last N months, independent of the picker
      this._refresh();
      return;
    }
    this._timer = setInterval(() => this._refresh(), REFRESH_MS);
    // follow the period picker (energy-date-selection on this view creates the collection)
    const tryAttach = () => {
      const coll = this._collection();
      if (!coll || this._unsub) return !!this._unsub;
      this._unsub = coll.subscribe((data) => {
        if (data?.start) {
          this._range = { start: +data.start, end: +(data.end || defaultRange().end) };
          this._refresh();
        }
      });
      return true;
    };
    if (!tryAttach()) {
      let tries = 0;
      this._attach = setInterval(() => {
        if (tryAttach() || ++tries > 30) clearInterval(this._attach);
      }, 1000);
    }
    this._refresh();
  }

  async _refresh() {
    if (this._config?.range === "months") this._range = monthsRange(this._config.months || 12);
    if (!this._hass || !this._range) return;
    const { start, end } = this._range;
    try {
      await this._load(start, end);
      this._render();
    } catch (err) {
      this._renderError(err);
    }
  }

  _shell(title, body, extraHeader = "") {
    if (!this.shadowRoot) this.attachShadow({ mode: "open" });
    this.shadowRoot.innerHTML = `
      <style>
        :host { display: block; }
        ha-card { padding: 16px; height: 100%; box-sizing: border-box; }
        .head { display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; }
        h2 { margin: 0; font-size: 24px; font-weight: 400; color: var(--primary-text-color); line-height: 32px; }
        .chip { border: 1px solid var(--divider-color); border-radius: 8px; padding: 2px 8px; font-size: 14px;
                font-weight: 500; color: var(--primary-text-color); white-space: nowrap; }
        table { width: 100%; border-collapse: collapse; color: var(--primary-text-color); font-size: 14px; }
        th { text-align: left; font-weight: 500; color: var(--primary-text-color); padding: 8px 4px; }
        th.n, td.n { text-align: right; white-space: nowrap; }
        td { padding: 12px 4px; border-top: 1px solid var(--divider-color); }
        tr.total td { font-weight: 500; }
        .sw { width: 32px; height: 18px; border-radius: 4px; border: 1px solid; display: inline-block; vertical-align: middle; }
        .name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 0; width: 100%; }
        .empty, .err { color: var(--secondary-text-color); font-size: 14px; padding: 8px 0; }
        svg text { fill: var(--secondary-text-color); font-size: 11px; font-family: inherit; }
        .legend { display: flex; justify-content: center; gap: 16px; margin-top: 8px; font-size: 13px;
                  color: var(--primary-text-color); flex-wrap: wrap; }
        .dot { width: 12px; height: 12px; border-radius: 50%; display: inline-block; margin-right: 6px; vertical-align: -1px; }
      </style>
      <ha-card>
        <div class="head"><h2>${title}</h2>${extraHeader}</div>
        ${body}
      </ha-card>`;
  }

  _renderError(err) {
    this._shell(this._config.title || "", `<div class="err">${String(err?.message || err)}</div>`);
  }

  _fmt(v, digits = 2, minDigits = 0) {
    return new Intl.NumberFormat(this._hass?.locale?.language || "en",
      { minimumFractionDigits: minDigits, maximumFractionDigits: digits }).format(v);
  }

  /** kWh always with 2 decimals, so values in one table line up ("1,90" next to "2,15"). */
  _kwh(v) {
    return `${this._fmt(v, 2, 2)} kWh`;
  }

  _money(v) {
    const cur = this._hass?.config?.currency;
    try {
      return new Intl.NumberFormat(this._hass?.locale?.language || "en",
        cur ? { style: "currency", currency: cur, maximumFractionDigits: 2 } : { maximumFractionDigits: 2 }).format(v);
    } catch {
      return `${this._fmt(v)} ${cur || ""}`.trim();
    }
  }

  getCardSize() { return 4; }
}

// ------------------------------------------------------------------ table

export class BojlerTariffTable extends BojlerTariffCard {
  async _load(start, end) {
    this._totals = await fetchTotals(this._hass, start, end);
  }

  _render() {
    const T = this.T;
    const { kwh, cost } = this._totals;
    const row = (k, name) => {
      const c = this._color(k);
      return `<tr><td><span class="sw" style="background:${c}55;border-color:${c}"></span></td>
        <td class="name">${name}</td><td class="n">${this._kwh(kwh[k])}</td><td class="n">${this._money(cost[k])}</td></tr>`;
    };
    this._shell(this._config.title || T.table_title, `
      <table>
        <tr><th></th><th>${T.col_source}</th><th class="n">${T.col_energy}</th><th class="n">${T.col_cost}</th></tr>
        ${row("cheap", T.cheap)}
        ${row("exp", T.expensive)}
        <tr class="total"><td></td><td>${T.total_row}</td>
          <td class="n">${this._kwh(kwh.cheap + kwh.exp)}</td><td class="n">${this._money(cost.cheap + cost.exp)}</td></tr>
      </table>`);
  }

  getGridOptions() { return { columns: 12, min_columns: 6, rows: "auto" }; }
}

// ------------------------------------------------------------------ chart

export class BojlerTariffChart extends BojlerTariffCard {
  async _load(start, end) {
    const period = bucketPeriod(start, end);
    const metric = this._config.metric === "cost" ? "cost" : "kwh";
    const [rows, totals] = await Promise.all([
      fetchRows(this._hass, start, end, period, SERIES[metric]), fetchTotals(this._hass, start, end)]);
    this._period = period;
    this._metric = metric;
    this._buckets = buildBuckets(start, end, period, rows, totals[metric]);
  }

  _start() {
    super._start();
    if (!this._ro && globalThis.ResizeObserver) {
      this._ro = new ResizeObserver(() => this._buckets && this._render());
      this._ro.observe(this);
    }
  }

  _label(t) {
    const lang = this._hass?.locale?.language || "en";
    const d = new Date(t);
    if (this._period === "hour") return new Intl.DateTimeFormat(lang, { hour: "2-digit", minute: "2-digit" }).format(d);
    if (this._period === "day") return new Intl.DateTimeFormat(lang, { day: "numeric", month: "short" }).format(d);
    return new Intl.DateTimeFormat(lang, { month: "short" }).format(d);
  }

  _render() {
    const T = this.T;
    const b = this._buckets || [];
    const total = b.reduce((s, x) => s + x.cheap + x.exp, 0);
    const cost = this._metric === "cost";
    const unit = cost ? (this._hass?.config?.currency || "") : "kWh";
    const val = (v) => (cost ? this._money(v) : this._kwh(v));
    const chip = `<span class="chip">${val(total)}</span>`;
    const W = Math.max(280, Math.round(this.clientWidth || 400) - 32);
    const H = 220, L = 36, B = 22, TOP = 14;
    const { max, step } = axis(Math.max(0, ...b.map((x) => x.cheap + x.exp)));
    const plotW = W - L - 4, plotH = H - TOP - B;
    const bw = plotW / Math.max(1, b.length);
    const y = (v) => TOP + plotH - (v / max) * plotH;
    const cc = this._color("cheap"), cx = this._color("exp");
    let svg = "";
    for (let v = 0; v <= max + 1e-9; v += step) {
      const yy = y(v);
      svg += `<line x1="${L}" x2="${W - 4}" y1="${yy}" y2="${yy}" stroke="var(--divider-color)" stroke-width="1"/>`;
      svg += `<text x="${L - 6}" y="${yy + 4}" text-anchor="end">${this._fmt(v, 2)}</text>`;
    }
    const labelEvery = Math.ceil(b.length / Math.max(2, Math.floor(plotW / 60)));
    b.forEach((x, i) => {
      const x0 = L + i * bw + bw * 0.15, w = Math.max(1, bw * 0.7);
      const tip = `${this._label(x.t)} — ${T.cheap}: ${val(x.cheap)}, ${T.expensive}: ${val(x.exp)}`;
      if (x.cheap > 0) svg += `<rect x="${x0}" y="${y(x.cheap)}" width="${w}" height="${y(0) - y(x.cheap)}" rx="2" fill="${cc}99" stroke="${cc}"><title>${tip}</title></rect>`;
      if (x.exp > 0) svg += `<rect x="${x0}" y="${y(x.cheap + x.exp)}" width="${w}" height="${y(x.cheap) - y(x.cheap + x.exp)}" rx="2" fill="${cx}99" stroke="${cx}"><title>${tip}</title></rect>`;
      if ((b.length - 1 - i) % labelEvery === 0) {
        const cxl = L + i * bw + bw / 2;            // keep edge labels inside the card
        const [lx, anchor] = cxl > W - 34 ? [W - 2, "end"] : cxl < L + 22 ? [L, "start"] : [cxl, "middle"];
        svg += `<text x="${lx}" y="${H - 6}" text-anchor="${anchor}">${this._label(x.t)}</text>`;
      }
    });
    svg += `<text x="${L}" y="${TOP - 4}" text-anchor="start">${unit}</text>`;
    const body = `
      <svg width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" role="img">${svg}</svg>
      ${total > 0 ? "" : `<div class="empty">${T.no_data}</div>`}
      <div class="legend"><span><i class="dot" style="background:${cc}"></i>${T.cheap}</span>
        <span><i class="dot" style="background:${cx}"></i>${T.expensive}</span></div>`;
    this._shell(this._config.title || T.usage_graph, body, chip);
  }

  getGridOptions() { return { columns: 12, min_columns: 6, rows: "auto" }; }
  getCardSize() { return 6; }
}

// ------------------------------------------------------------------ Excel report (download + refresh)

export const EXCEL_ENTITY = "sensor.bojler_raspored_excel_izvestaj";

/** "dd.mm. HH:MM" in local time (same format as before in the markdown card). */
export function shortTime(iso) {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  const p = (n) => String(n).padStart(2, "0");
  return `${p(d.getDate())}.${p(d.getMonth() + 1)}. ${p(d.getHours())}:${p(d.getMinutes())}`;
}

/** File name of the report URL ("/local/bojler-x/name.xlsx?v=1" → "name.xlsx"). */
export function fileNameOf(url) {
  return decodeURIComponent(String(url).split("?")[0].split("/").pop() || "report.xlsx");
}

/**
 * Why a custom card: Home Assistant's frontend treats every same-origin link as in-app navigation, so the
 * markdown link to /local/…xlsx only switched the dashboard view and nothing was downloaded. A link with the
 * `download` attribute is left alone by the router and the browser saves the file.
 */
export class BojlerExcelCard extends Base {
  setConfig(config) {
    this._config = { entity: EXCEL_ENTITY, ...config };
  }

  get T() {
    return CATALOGS[languageOf(this._config, this._hass)];
  }

  set hass(hass) {
    this._hass = hass;
    const st = hass.states[this._config.entity];
    const key = st ? `${st.state}|${st.attributes.url}` : "";
    if (key !== this._key || !this.shadowRoot) {
      this._key = key;
      this._render();
    }
  }

  async _refresh() {
    if (this._busy) return;
    this._busy = true;
    this._msg = "";
    this._render();
    try {
      await this._hass.callService("ariston_schedule", "export_xlsx", {});
      this._msg = this.T.excel_refreshed;
    } catch (err) {
      this._msg = this.T.excel_failed.replace("{error}", err?.message || String(err));
    }
    this._busy = false;
    this._render();
    clearTimeout(this._msgTimer);
    this._msgTimer = setTimeout(() => { this._msg = ""; this._render(); }, 5000);
  }

  _render() {
    if (!this._hass || !this._config) return;
    const T = this.T;
    const st = this._hass.states[this._config.entity];
    const url = st?.attributes?.url;
    if (!this.shadowRoot) this.attachShadow({ mode: "open" });
    const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
    const body = url
      ? `<a class="dl" href="${esc(url)}" download="${esc(fileNameOf(url))}" target="_blank" rel="noopener">
           <ha-icon icon="mdi:microsoft-excel"></ha-icon><span>${esc(T.excel_link)}</span></a>
         <div class="desc">${esc(T.excel_desc.replace("{time}", shortTime(st.state)))}</div>`
      : `<div class="desc">${esc(T.excel_pending)}</div>`;
    this.shadowRoot.innerHTML = `
      <style>
        ha-card { padding: 12px 16px; display: flex; align-items: center; gap: 12px; height: 100%; box-sizing: border-box; }
        .main { flex: 1; min-width: 0; }
        a.dl { display: inline-flex; align-items: center; gap: 8px; color: var(--primary-color); font-weight: 500;
               text-decoration: none; font-size: 16px; }
        a.dl:hover span { text-decoration: underline; }
        .desc { color: var(--secondary-text-color); font-size: 13px; margin-top: 4px; }
        .msg { color: var(--primary-text-color); font-size: 13px; margin-top: 4px; }
        button { display: flex; flex-direction: column; align-items: center; gap: 4px; background: none; border: 1px solid
                 var(--divider-color); border-radius: 12px; padding: 8px 12px; color: var(--primary-text-color);
                 font: inherit; font-size: 13px; cursor: pointer; min-width: 72px; }
        button[disabled] { opacity: 0.5; cursor: progress; }
      </style>
      <ha-card>
        <div class="main">${body}${this._msg ? `<div class="msg">${esc(this._msg)}</div>` : ""}</div>
        <button ${this._busy ? "disabled" : ""}><ha-icon icon="mdi:refresh"></ha-icon>${esc(T.refresh)}</button>
      </ha-card>`;
    this.shadowRoot.querySelector("button").addEventListener("click", () => this._refresh());
  }

  getCardSize() { return 2; }
  getGridOptions() { return { columns: 12, min_columns: 6, rows: "auto" }; }
}

if (globalThis.customElements) {
  if (!customElements.get("bojler-tariff-table")) customElements.define("bojler-tariff-table", BojlerTariffTable);
  if (!customElements.get("bojler-tariff-chart")) customElements.define("bojler-tariff-chart", BojlerTariffChart);
  if (!customElements.get("bojler-excel-card")) customElements.define("bojler-excel-card", BojlerExcelCard);
  window.customCards = window.customCards || [];
  for (const [type, name] of [["bojler-tariff-table", "Bojler – kWh/cost per tariff"], ["bojler-tariff-chart", "Bojler – consumption per tariff"], ["bojler-excel-card", "Bojler – Excel report"]]) {
    if (!window.customCards.some((c) => c.type === type)) window.customCards.push({ type, name });
  }
}
