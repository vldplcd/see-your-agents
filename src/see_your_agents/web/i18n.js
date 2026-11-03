// The UI language and its strings (docs/i18n.md). Loaded in <head> before every other script: it picks the
// language (prefs.lang, else English) and writes in /i18n/<lang>.js, which sets window.SYA_I18N (that language's
// strings over English). The written script runs before the parser goes on, so t() works in every script that
// follows, figures.js included, even at load time. Switching the language reloads the page.
(() => {
  let lang = "";
  try { lang = JSON.parse(localStorage.getItem("sya.prefs"))?.lang || ""; } catch (e) { /* storage unavailable */ }
  if (!/^[a-z]{2,3}(-[A-Z]{2})?$/.test(lang)) lang = "en";
  document.documentElement.lang = lang;
  if (window.SYA_I18N_ALL) { window.SYA_I18N = SYA_I18N_ALL[lang] || SYA_I18N_ALL.en; return; }  // the demo
  const v = new URL(document.currentScript.src).search;  // ?v=<version>.<instance>, as page() wrote it
  document.write(`<script src="i18n/${lang}.js${v}"><\/script>`);
})();

// t(key, params): the string in the page's language. {name} takes params.name; a plural string (an object of
// Intl.PluralRules categories) picks its form by params.n. A key no catalog has shows as itself, and the
// console says so once. The result is text: in HTML, esc() it when params come from data (docs/i18n.md).
const t = (() => {
  const missing = new Set(), rules = {};
  return (key, params = {}) => {
    const cat = window.SYA_I18N;
    let s = cat?.strings[key];
    if (s == null) {
      if (!missing.has(key)) { missing.add(key); console.warn(`i18n: no string for "${key}"`); }
      return key;
    }
    if (typeof s === "object") s = s[(rules[cat.lang] ||= new Intl.PluralRules(cat.lang)).select(params.n)] ?? s.other;
    return String(s).replace(/\{(\w+)\}/g, (m, name) => (name in params ? String(params[name]) : m));
  };
})();

// Static markup (index.html, a feature's header) in the page's language: data-i18n="key" sets the element's
// text and keeps its child elements before it; data-i18n-title, -aria-label, -placeholder set those attributes.
function translate(root) {
  for (const el of root.querySelectorAll("[data-i18n]")) {
    for (const n of [...el.childNodes]) if (n.nodeType === Node.TEXT_NODE) n.remove();
    el.append(t(el.dataset.i18n));
  }
  for (const attr of ["title", "aria-label", "placeholder"]) {
    for (const el of root.querySelectorAll(`[data-i18n-${attr}]`)) el.setAttribute(attr, t(el.getAttribute(`data-i18n-${attr}`)));
  }
}
