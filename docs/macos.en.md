# Running on macOS (launchd)

🇬🇧 English · 🇩🇪 [Deutsch](macos.md)

The proxy runs as a launchd user agent next to an existing Ollama (the app or `ollama serve`).

## Folders

```
~/ollama-proxy/
  app/        Git clone of this repo (code, scripts, plist template)
  venv/       Python environment (from requirements.txt)
  data/       state.json (model list + presets), modules.json, proxy.log, proxy.err
  deploy.env  optional: DEPLOY_MODE=launchd, LAUNCHD_LABEL=...
```

Runtime data deliberately lives outside `app/` so that Git never touches it.

## Installation

```bash
git clone https://github.com/Poldion/OllamaProxy.git ~/ollama-proxy/app
python3 -m venv ~/ollama-proxy/venv
~/ollama-proxy/venv/bin/pip install -r ~/ollama-proxy/app/requirements.txt

# Quick test in the foreground (Ctrl-C stops it), then open http://<mac>:11435/
OLLAMA_UPSTREAM=http://localhost:11434 ~/ollama-proxy/venv/bin/python ~/ollama-proxy/app/ollama_proxy.py

# Set up as a service: installs the template deploy/macos/ollamaproxy.plist and starts the service
~/ollama-proxy/app/scripts/update.sh --force
```

The service is called `local.ollamaproxy` (change it with `LAUNCHD_LABEL` in `~/ollama-proxy/deploy.env`). Logs:
`~/ollama-proxy/data/proxy.log` and `proxy.err`.

## Update

On the Mac: `~/ollama-proxy/app/scripts/update.sh` – fetches the new state, checks the syntax, reloads the service, runs
a health check and rolls back to the previous commit on errors. From another machine: `scripts/deploy.sh --target <name>`
(targets in `deploy/targets.local.env`). Do not edit the clone on the Mac yourself, otherwise the fast-forward update
fails.

Reload the service by hand (with a pause, otherwise launchd reports “Input/output error”):

```bash
launchctl bootout gui/$(id -u)/local.ollamaproxy 2>/dev/null
sleep 2
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/local.ollamaproxy.plist
```

## Pitfalls on macOS

- **Two Ollama instances:** the menu bar app and your own `ollama serve` service can run at the same time and listen on
  different addresses (`127.0.0.1` vs. `*`). Depending on how `localhost` resolves, the proxy then ends up at the wrong
  instance; `ollama ps` is empty although memory is in use. Check with `ps aux | grep -E "[l]lama-server|[o]llama"` and
  `lsof -iTCP -sTCP:LISTEN -P | grep -i ollama`, then switch one of them off. `scripts/diagnose.sh --target <name>`
  collects this information.
- **Memory readings:** Metal keeps buffers “wired” for a few minutes after a request. Only compare memory values after a
  few minutes of rest. From Ollama 0.40 the weights live in GPU memory and do not count towards the process; the proxy
  therefore shows the maximum of measurement and forecast per model, plus “In use system memory” of the IOAccelerator.
- **`use_mmap`:** without mmap (the default) macOS compresses/swaps the CPU part of a model when idle (more free RAM,
  slow first answer). With `use_mmap: true` the model stays fixed in RAM (consistently fast, less free).
- **Embeddings** (`/api/embeddings`) are passed through unchanged and not forced onto the chat model.
- **CORS** is open (`*`) so that desktop clients with a browser engine (Electron/Tauri) can talk to the proxy.
