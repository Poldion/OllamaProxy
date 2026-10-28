#!/usr/bin/env python3
"""Findet Texte ohne englische Übersetzung in locales/en.json und Übersetzungen mit falschen Platzhaltern.

Erfasst: tr('…') und N_('…') in JS/HTML/Python, feste Texte im HTML (Textknoten, placeholder/title/aria-label/alt, <title>) und
Server-Meldungen (HTTPException, {"detail": …}; eingesetzte Werte werden zu {x}).
  python3 scripts/check_i18n.py          -> Exit 1 bei fehlenden Übersetzungen oder falschen Platzhaltern
  python3 scripts/check_i18n.py --list   -> fehlende Texte als JSON (Vorlage zum Ergänzen)
"""
import json
import os
import re
import sys
from html.parser import HTMLParser

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKIP_DIRS = {"tests", "scripts", "locales", ".git", "node_modules", "__pycache__", "docs", "homeassistant"}
SKIP_FILES = {"i18n.js", "i18n.py"}
ATTRS = {"placeholder", "title", "aria-label", "alt"}
# Muss Buchstaben enthalten; reine Namen/Kürzel (z. B. "Test", "SVG") stehen trotzdem im Wörterbuch
LETTERS = re.compile(r"[A-Za-zÄÖÜäöüß]{2}")

norm = lambda s: re.sub(r"\s+", " ", s).strip()


class Static(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.found, self.skip, self.no = set(), 0, 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ("script", "style", "textarea", "code", "pre"):
            self.skip += 1
        if "data-no-i18n" in a:
            self.no += 1
        if not self.no:
            for k in ATTRS & a.keys():
                if a[k] and LETTERS.search(a[k]) and "${" not in a[k]:
                    self.found.add(norm(a[k]))

    def handle_endtag(self, tag):
        if tag in ("script", "style", "textarea", "code", "pre"):
            self.skip = max(0, self.skip - 1)

    def handle_data(self, data):
        t = norm(data)
        if not self.skip and not self.no and t and LETTERS.search(t):
            self.found.add(t)


TR = re.compile(r"""\b(?:tr|N_)\(\s*(?:'((?:[^'\\]|\\.)*)'|"((?:[^"\\]|\\.)*)"|`([^`$]*)`)""")
PY_MSG = re.compile(r"""(?:\btranslate\(\s*|\bValueError\(\s*|HTTPException\(\s*\d+\s*,\s*|HTTPException\([^)]*?detail\s*=\s*|["']detail["']\s*:\s*|\bfail\(\s*\d+\s*,\s*)(f?)("(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')""")


def unescape(s: str) -> str:
    return re.sub(r"\\(.)", lambda m: {"n": "\n", "t": "\t"}.get(m.group(1), m.group(1)), s)


def own_app(path: str) -> bool:
    return any("check_i18n.py" in files for _, _, files in os.walk(path))


def collect():
    used = {}
    for d, dirs, files in os.walk(ROOT):
        # Unterordner mit eigener Prüfung (scripts/check_i18n.py) sind eigene Apps
        dirs[:] = [x for x in dirs if x not in SKIP_DIRS and not own_app(os.path.join(d, x))]
        for name in files:
            if name in SKIP_FILES or not name.endswith((".html", ".js", ".py")):
                continue
            path = os.path.join(d, name)
            rel = os.path.relpath(path, ROOT)
            src = open(path, encoding="utf-8").read()
            if name.endswith(".py"):
                # eingebettete Oberflächen (HTML in Python-Strings)
                for html in re.findall(r"<html.*?</html>", src, re.S | re.I):
                    p = Static(); p.feed(html)
                    for t in p.found:
                        used.setdefault(t, rel)
                for m in PY_MSG.finditer(src):
                    s = unescape(m.group(2)[1:-1])
                    if m.group(1):
                        s = re.sub(r"\{[^{}]*\}", "{x}", s)
                    if LETTERS.search(s):
                        used.setdefault(s, rel)
            elif name.endswith(".html"):
                p = Static(); p.feed(src)
                for t in p.found:
                    used.setdefault(t, rel)
            for m in TR.finditer(src):
                s = next(g for g in m.groups() if g is not None)
                used.setdefault(unescape(s), rel)
    # Texte aus Daten (z. B. Workflow-Namen), die die Oberfläche mit tr(wert) anzeigt
    extra = os.path.join(ROOT, "scripts", "i18n_data.py")
    if os.path.exists(extra):
        import importlib.util
        spec = importlib.util.spec_from_file_location("i18n_data", extra)
        mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
        for t in mod.texts(ROOT):
            if t and LETTERS.search(t):
                used.setdefault(t, "scripts/i18n_data.py")
    return used


def main():
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8")
    with open(os.path.join(ROOT, "locales", "en.json"), encoding="utf-8") as f:
        en = json.load(f)
    used = collect()
    missing = sorted(k for k in used if k not in en)
    ph = lambda s: sorted(re.findall(r"\{\w*\}", s))
    wrong = sorted(k for k, v in en.items() if ph(k) != ph(v))
    unused = sorted(k for k in en if k not in used)
    if "--list" in sys.argv:
        print(json.dumps({k: "" for k in missing}, ensure_ascii=False, indent=2))
        return 0
    if unused:
        print(f"Nicht mehr genutzt ({len(unused)}):\n  " + "\n  ".join(unused), file=sys.stderr)
    if wrong:
        print(f"Falsche Platzhalter ({len(wrong)}):\n  " + "\n  ".join(wrong), file=sys.stderr)
    if missing:
        print(f"Ohne Übersetzung ({len(missing)}):\n  " + "\n  ".join(f"{k}   [{used[k]}]" for k in missing),
              file=sys.stderr)
    if missing or wrong:
        return 1
    print(f"i18n: {len(used)} Texte, alle übersetzt")
    return 0


if __name__ == "__main__":
    sys.exit(main())
