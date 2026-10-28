/* Übersetzung der Oberfläche – wie gettext: Der deutsche Text ist der Schlüssel, die englischen Texte stehen in
   locales/en.json und kommen vom Server mit (/i18n.js = I18N-Kopf + diese Datei). Platzhalter: tr('{n} Bilder', {n}).
   Sprache: Auswahl im Browser gespeichert, sonst Browsersprache; ein Wechsel lädt die Seite neu.
   Feste Texte im HTML übersetzt i18nApply() (Textknoten, placeholder/title/aria-label/alt, Seitentitel),
   Texte aus dem Code laufen über tr(). Elemente mit data-no-i18n bleiben unverändert. */
(function () {
  const DICT = I18N.en || {};

  function detect() {
    try {
      const saved = localStorage.getItem(I18N.key);
      if (saved === 'de' || saved === 'en') return saved;
    } catch (e) { /* kein Speicher (privates Fenster) */ }
    const nav = (navigator.languages && navigator.languages[0]) || navigator.language || 'de';
    return nav.toLowerCase().startsWith('de') ? 'de' : 'en';
  }

  const LANG = detect();
  document.documentElement.lang = LANG;

  function tr(text, vars) {
    let out = LANG === 'en' ? (DICT[text] ?? text) : text;
    if (vars) out = out.replace(/\{(\w+)\}/g, (m, k) => (k in vars ? String(vars[k] ?? '') : m));
    return out;
  }

  function setLang(l) {
    try { localStorage.setItem(I18N.key, l); } catch (e) { /* egal */ }
    location.reload();
  }

  const norm = s => s.replace(/\s+/g, ' ').trim();
  const SKIP = new Set(['SCRIPT', 'STYLE', 'TEXTAREA', 'CODE', 'PRE']);
  let applied = false;

  function i18nApply(root) {
    if (!root) { if (applied) return; applied = true; root = document.body; }
    if (LANG === 'de' || !root) return;
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
      acceptNode: n => (n.parentElement && (SKIP.has(n.parentElement.tagName) || n.parentElement.closest('[data-no-i18n]'))
        ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT),
    });
    const nodes = [];
    while (walker.nextNode()) nodes.push(walker.currentNode);
    for (const n of nodes) {
      const key = norm(n.nodeValue);
      if (key && DICT[key]) {
        const lead = n.nodeValue.match(/^\s*/)[0], tail = n.nodeValue.match(/\s*$/)[0];
        n.nodeValue = lead + DICT[key] + tail;
      }
    }
    for (const e of root.querySelectorAll('[placeholder],[title],[aria-label],[alt]')) {
      if (e.closest('[data-no-i18n]')) continue;
      for (const a of ['placeholder', 'title', 'aria-label', 'alt']) {
        const v = e.getAttribute(a);
        if (v && DICT[norm(v)]) e.setAttribute(a, DICT[norm(v)]);
      }
    }
    if (DICT[norm(document.title)]) document.title = DICT[norm(document.title)];
  }

  /** Knopf zum Umschalten (zeigt die jeweils andere Sprache) */
  function langSwitch(className) {
    const b = document.createElement('button');
    b.type = 'button';
    b.className = className || 'lang-switch';
    b.textContent = LANG === 'de' ? 'EN' : 'DE';
    b.title = LANG === 'de' ? 'English' : 'Deutsch';
    b.setAttribute('aria-label', b.title);
    b.onclick = () => setLang(LANG === 'de' ? 'en' : 'de');
    return b;
  }

  // Server-Meldungen in derselben Sprache: Kopfzeile bei jeder Anfrage an den eigenen Server
  const origFetch = window.fetch.bind(window);
  window.fetch = (input, init) => {
    const url = typeof input === 'string' ? input : (input && input.url) || '';
    if (/^https?:/i.test(url) && !url.startsWith(location.origin)) return origFetch(input, init);
    init = Object.assign({}, init);
    const h = new Headers(init.headers || (typeof input !== 'string' && input.headers) || {});
    if (!h.has(I18N.header)) h.set(I18N.header, LANG);
    init.headers = h;
    return origFetch(input, init);
  };

  Object.assign(window, {
    LANG, LOCALE: LANG === 'de' ? 'de-DE' : 'en-GB', tr, setLang, i18nApply, langSwitch,
  });
  document.addEventListener('DOMContentLoaded', () => i18nApply());
})();
