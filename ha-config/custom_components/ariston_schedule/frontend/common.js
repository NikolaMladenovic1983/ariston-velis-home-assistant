// Shared by the dashboard strategy and the custom cards: catalogs + language selection.
const VERSION_QUERY = new URL(import.meta.url).search;
export const { CATALOGS } = await import(`./i18n.js${VERSION_QUERY}`);

/** Pick the catalog for a HA language code ("de", "de-CH", "sr-Latn", "sr", "pt-BR" → en …). */
export function pickLanguage(code) {
  if (!code) return "en";
  if (CATALOGS[code]) return code;
  const base = code.split("-")[0];
  if (base === "sr") return /latn/i.test(code) ? "sr-Latn" : "sr";
  return CATALOGS[base] ? base : "en";
}

/** Language for a card/strategy: explicit config.language wins over the user's profile language. */
export function languageOf(config, hass) {
  return pickLanguage(config?.language || hass?.locale?.language || hass?.language || "en");
}
