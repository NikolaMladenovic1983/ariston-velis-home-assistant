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
