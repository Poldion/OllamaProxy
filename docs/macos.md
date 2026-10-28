# Betrieb unter macOS (launchd)

Der Proxy läuft als launchd-User-Agent neben einem vorhandenen Ollama (App oder `ollama serve`).

## Ordner

```
~/ollama-proxy/
  app/        Git-Klon dieses Repos (Code, Skripte, plist-Vorlage)
  venv/       Python-Umgebung (aus requirements.txt)
  data/       state.json (Modellliste + Presets), modules.json, proxy.log, proxy.err
  deploy.env  optional: DEPLOY_MODE=launchd, LAUNCHD_LABEL=...
```

Laufzeitdaten liegen bewusst außerhalb von `app/`, damit Git sie nie anfasst.

## Installation

```bash
git clone https://github.com/Poldion/OllamaProxy.git ~/ollama-proxy/app
python3 -m venv ~/ollama-proxy/venv
~/ollama-proxy/venv/bin/pip install -r ~/ollama-proxy/app/requirements.txt

# Kurztest im Vordergrund (Strg-C beendet), danach http://<mac>:11435/ öffnen
OLLAMA_UPSTREAM=http://localhost:11434 ~/ollama-proxy/venv/bin/python ~/ollama-proxy/app/ollama_proxy.py

# Als Dienst einrichten: setzt die Vorlage deploy/macos/ollamaproxy.plist ein und startet den Dienst
~/ollama-proxy/app/scripts/update.sh --force
```

Der Dienst heißt `local.ollamaproxy` (änderbar mit `LAUNCHD_LABEL` in `~/ollama-proxy/deploy.env`). Logs:
`~/ollama-proxy/data/proxy.log` und `proxy.err`.

## Update

Auf dem Mac: `~/ollama-proxy/app/scripts/update.sh` – holt den neuen Stand, prüft die Syntax, lädt den Dienst neu,
macht einen Health-Check und rollt bei Fehlern auf den vorherigen Commit zurück. Von einem anderen Rechner aus:
`scripts/deploy.sh --target <name>` (Ziele in `deploy/targets.local.env`). Im Klon auf dem Mac nicht selbst
editieren, sonst scheitert das Fast-Forward-Update.

Dienst von Hand neu laden (mit Pause, sonst meldet launchd „Input/output error“):

```bash
launchctl bootout gui/$(id -u)/local.ollamaproxy 2>/dev/null
sleep 2
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/local.ollamaproxy.plist
```

## Stolpersteine unter macOS

- **Zwei Ollama-Instanzen:** Die Menüleisten-App und ein eigener `ollama serve`-Dienst können gleichzeitig laufen
  und auf verschiedenen Adressen lauschen (`127.0.0.1` gegen `*`). Je nach Auflösung von `localhost` landet der Proxy
  dann bei der falschen Instanz, `ollama ps` ist leer, obwohl Speicher belegt ist. Prüfen mit
  `ps aux | grep -E "[l]lama-server|[o]llama"` und `lsof -iTCP -sTCP:LISTEN -P | grep -i ollama`, dann eine der
  beiden abschalten. `scripts/diagnose.sh --target <name>` sammelt diese Angaben.
- **Speicher-Messwerte:** Metal hält Puffer nach einer Anfrage einige Minuten „wired“. Speicherwerte nur nach ein paar
  Minuten Ruhe vergleichen. Ab Ollama 0.40 liegen die Gewichte im GPU-Speicher und zählen nicht beim Prozess; der
  Proxy zeigt deshalb je Modell das Maximum aus Messung und Prognose und dazu „In use system memory“ des
  IOAccelerator.
- **`use_mmap`:** Ohne mmap (Standard) komprimiert/swappt macOS den CPU-Teil eines Modells im Leerlauf (mehr freier
  RAM, langsame erste Antwort). Mit `use_mmap: true` bleibt das Modell fest im RAM (konstant schnell, weniger frei).
- **Embeddings** (`/api/embeddings`) werden unverändert durchgereicht und nicht auf das Chat-Modell gezwungen.
- **CORS** ist offen (`*`), damit Desktop-Clients mit Browser-Engine (Electron/Tauri) den Proxy ansprechen können.
