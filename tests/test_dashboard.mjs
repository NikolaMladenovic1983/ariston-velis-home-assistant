// Dashboard strategy test (Node >= 18):  node tests/test_dashboard.mjs
// Builds the dashboard in every language and checks that no text is missing and that the structure
// (tabs, paths, entities) is identical in all languages.
import assert from "node:assert/strict";
import { pathToFileURL } from "node:url";
import path from "node:path";

const dir = path.resolve(import.meta.dirname, "../ha-config/custom_components/ariston_schedule/frontend");
const { CATALOGS } = await import(pathToFileURL(path.join(dir, "i18n.js")).href);
const mod = await import(pathToFileURL(path.join(dir, "bojler-dashboard.js")).href + "?v=test");
const { BojlerDashboardStrategy, pickLanguage } = mod;

const langs = Object.keys(CATALOGS);
assert.deepEqual(langs.sort(), ["de", "en", "es", "fr", "it", "ru", "sr", "sr-Latn"]);

// language selection
assert.equal(pickLanguage("de-CH"), "de");
assert.equal(pickLanguage("sr-Latn"), "sr-Latn");
assert.equal(pickLanguage("sr"), "sr");
assert.equal(pickLanguage("pt-BR"), "en");
assert.equal(pickLanguage(undefined), "en");

const entitiesOf = (o, acc = new Set()) => {
  if (Array.isArray(o)) o.forEach((x) => entitiesOf(x, acc));
  else if (o && typeof o === "object") {
    for (const [k, v] of Object.entries(o)) {
      if (k === "entity" || k === "entity_id") acc.add(v);
      entitiesOf(v, acc);
    }
  }
  return acc;
};

let reference;
for (const lang of langs) {
  const hass = { locale: { language: lang }, config: { currency: "RSD" } };
  const dash = await BojlerDashboardStrategy.generate({ type: "custom:bojler" }, hass);
  const json = JSON.stringify(dash);
  assert.ok(!json.includes("undefined"), `${lang}: 'undefined' in output`);
  assert.ok(!/\{(?!\{|%|"|')[a-z_]+\}/.test(json.replace(/\{\{[^}]*\}\}/g, "")), `${lang}: unreplaced {placeholder}`);
  assert.deepEqual(dash.views.map((v) => v.path), ["bojler", "potrosnja", "raspored", "podesavanja"]);
  assert.equal(dash.title, CATALOGS[lang].title);
  const ents = [...entitiesOf(dash)].sort();
  if (!reference) reference = ents;
  assert.deepEqual(ents, reference, `${lang}: different entities`);
}

// explicit override wins over the profile language
const forced = await BojlerDashboardStrategy.generate({ language: "ru" }, { locale: { language: "en" } });
assert.equal(forced.views[0].title, CATALOGS.ru.tab_home);

console.log(`ok dashboard strategy: ${langs.length} languages, ${reference.length} entities`);

// ---- live tariff cards: pure helpers
const cards = await import(pathToFileURL(path.join(dir, "bojler-cards.js")).href + "?v=test");
const { bucketPeriod, buildBuckets, niceMax, bucketStart, axis } = cards;
const day = new Date(2026, 10, 23).getTime();
assert.equal(bucketPeriod(day, day + 86_400_000), "hour");
assert.equal(bucketPeriod(day, day + 30 * 86_400_000), "day");
assert.equal(bucketPeriod(day, day + 365 * 86_400_000), "month");
assert.deepEqual([0.4, 2.35, 12, 154].map(niceMax), [0.5, 2.5, 20, 200]);

// today, hourly: long-term rows up to 21h; 1.275 kWh (expensive) counted at 22:08 is only in the period total
const h = (n) => day + n * 3_600_000;
const rows = { cheap: [{ start: h(4), change: 2.35 }], exp: [] };
const b = buildBuckets(day, day + 86_400_000, "hour", rows, { cheap: 2.35, exp: 1.275 }, h(22) + 600_000);
assert.equal(b.length, 24);
assert.equal(b[4].cheap, 2.35);
assert.equal(b[22].exp, 1.275);                         // live remainder lands in the current hour
assert.equal(b.reduce((s, x) => s + x.cheap + x.exp, 0), 3.625);
// a month view puts the same remainder into today's bar
const m0 = new Date(2026, 10, 1).getTime(), m1 = new Date(2026, 11, 1).getTime();
const bm = buildBuckets(m0, m1, "day", { cheap: [], exp: [{ start: day - 86_400_000, change: 10 }] }, { cheap: 0, exp: 11.275 }, h(22));
assert.equal(bm.length, 30);
assert.equal(bm[21].exp, 10);
assert.ok(Math.abs(bm[22].exp - 1.275) < 1e-9);
assert.equal(bucketStart(h(22) + 600_000, "day"), day);
assert.deepEqual(axis(2.35), { step: 1, max: 3 });
assert.deepEqual(axis(11.275), { step: 5, max: 15 });
assert.deepEqual(axis(154), { step: 50, max: 200 });
console.log("ok live tariff card helpers");
