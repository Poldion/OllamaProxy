"""Übersetzung – wie gettext: Der deutsche Text ist der Schlüssel, `locales/en.json` enthält die englischen Texte
der Oberfläche und der Server-Meldungen. Fehlt eine Übersetzung, bleibt der deutsche Text stehen.

- Oberfläche: `/i18n.js` liefert das Wörterbuch mit `static/i18n.js` aus, Platzhalter `{name}`.
- Server-Meldungen entstehen auf Deutsch; `translate()` übersetzt sie, wenn die Anfrage Englisch wünscht (Kopfzeile
  der Oberfläche, sonst `Accept-Language`). Meldungen mit eingesetzten Werten werden über Muster mit `{x}` erkannt.
- Prüfen: `python3 scripts/check_i18n.py` (fehlende Übersetzungen, falsche Platzhalter; läuft in den Tests).
"""
import json
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
LANGS = ("de", "en")

with open(os.path.join(HERE, "locales", "en.json"), encoding="utf-8") as f:
    EN: dict[str, str] = json.load(f)


def request_lang(headers, header: str) -> str:
    explicit = headers.get(header, "")
    if explicit in LANGS:
        return explicit
    accept = headers.get("accept-language", "").lower().split(",")[0].strip()
    # Ohne Angabe bleibt es Deutsch (Home Assistant, Skripte)
    return "en" if accept and not accept.startswith("de") and accept != "*" else "de"


def tr(text: str, lang: str = "de", **values) -> str:
    out = EN.get(text, text) if lang == "en" else text
    if values:
        out = re.sub(r"\{(\w+)\}", lambda m: str(values.get(m.group(1), m.group(0))), out)
    return out


def N_(text: str) -> str:
    """Markiert eine Meldung, die erst später übersetzt wird (z. B. Rückgabewert, den translate() bekommt)."""
    return text


def _pattern(key: str) -> re.Pattern:
    return re.compile("^" + "(.*?)".join(re.escape(p) for p in key.split("{x}")) + "$", re.S)


# längere (spezifischere) Muster zuerst
PATTERNS = [(_pattern(k), v) for k, v in sorted(((k, v) for k, v in EN.items() if "{x}" in k),
                                                  key=lambda kv: -len(kv[0].replace("{x}", "")))]


def translate(message: str, lang: str) -> str:
    """Fertige Meldung übersetzen (auch mit eingesetzten Werten)."""
    if lang != "en" or not isinstance(message, str) or not message:
        return message
    if message in EN:
        return EN[message]
    for rx, to in PATTERNS:
        m = rx.match(message)
        if m:
            # eingesetzte Werte koennen selbst bekannte Texte sein (z. B. "Aktion: nicht freigeschaltet")
            groups = iter(translate(g, lang) for g in m.groups())
            return re.sub(r"\{x\}", lambda _: next(groups, ""), to)
    return message


def script(key: str, header: str) -> str:
    """Inhalt von /i18n.js: Wörterbuch + Laufzeit."""
    with open(os.path.join(HERE, "static", "i18n.js"), encoding="utf-8") as f:
        runtime = f.read()
    head = json.dumps({"key": key, "header": header, "en": EN}, ensure_ascii=False)
    return f"window.I18N = {head};\n{runtime}"
