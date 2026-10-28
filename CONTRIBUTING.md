# Contributing

Thanks for wanting to help! Bug reports, ideas and pull requests are welcome.

## Development setup

```bash
git clone https://github.com/Poldion/OllamaProxy.git && cd OllamaProxy
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt pytest
OLLAMA_UPSTREAM=http://localhost:11434 .venv/bin/python ollama_proxy.py   # UI on http://localhost:11435/
```

The whole proxy is one file, `ollama_proxy.py` (FastAPI, including the web UI).

## Tests

```bash
./run_tests.sh          # in Docker, same Python and packages as in production
python -m pytest tests  # or directly in your venv
```

Ollama and a module are simulated by `tests/fake_ollama.py`; no real Ollama or GPU is needed. Please add a test in
`tests/` for new behaviour.

## Translations

The interface is available in German and English. Visible texts are written in German in the code (`ollama_proxy.py` (UI in `HTML_UI` and admin messages)) and
translated gettext style – the German text is the key:

```js
tr('Speichern')                         // → "Save"
tr('{n} Bilder', {n: items.length})     // placeholders in curly braces
```

- **Interface:** English texts in `locales/en.json`. Fixed texts in the HTML are translated on load (text, `placeholder`, `title`,
  `aria-label`, `alt`); texts built in code go through `tr()`. If an entry is missing, the German text is shown.
- **Server messages:** written in German (`fail(400, "…", request)`) and translated by the server when the request asks for English
  (header `X-OllamaProxy-Lang`, sent by the interface, otherwise `Accept-Language`). Inserted values appear as `{x}` in the key.
- `python3 scripts/check_i18n.py` lists missing translations and wrong placeholders (part of the tests and the CI); `--list` prints the
  missing keys as JSON.
- Another language: add `locales/<code>.json` and extend `i18n.py` and `static/i18n.js` (`LANGS`, detection).

## Guidelines

- Small, self-contained changes – rather several pull requests than one big one.
- Keep the style of the existing code: short comments that explain *why*. Code comments are in German; English is fine for new
  comments. New UI texts need a translation (see above).
- Commit messages: one short line saying what changed, details in the body if needed.
- No private data in code, tests or issues: no access tokens, private IP addresses or host names. Use example values
  such as `192.168.1.10`.
- Line endings LF (enforced by `.gitattributes`).

By contributing you agree that your contribution is licensed under the project's [MIT License](LICENSE).

## Deutsch

Beiträge auf Deutsch sind willkommen – Issues und Pull Requests dürfen gern deutsch sein. Die ausführliche Doku unter
`docs/` gibt es bisher nur auf Deutsch.
