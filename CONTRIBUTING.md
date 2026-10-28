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

## Guidelines

- Small, self-contained changes – rather several pull requests than one big one.
- Keep the style of the existing code: short comments that explain *why*. Code comments and the UI are in German;
  English is fine for new comments.
- Commit messages: one short line saying what changed, details in the body if needed.
- No private data in code, tests or issues: no access tokens, private IP addresses or host names. Use example values
  such as `192.168.1.10`.
- Line endings LF (enforced by `.gitattributes`).

By contributing you agree that your contribution is licensed under the project's [MIT License](LICENSE).

## Deutsch

Beiträge auf Deutsch sind willkommen – Issues und Pull Requests dürfen gern deutsch sein. Die ausführliche Doku unter
`docs/` gibt es bisher nur auf Deutsch.
