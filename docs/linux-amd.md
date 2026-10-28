# Betrieb unter Linux mit AMD-GPU (z. B. Ryzen AI Max „Strix Halo“)

🇬🇧 [English](linux-amd.en.md) · 🇩🇪 Deutsch

Ollama und Proxy auf einem headless Linux-Rechner mit AMD-GPU. Getestet auf Ryzen AI Max+ 395 (iGPU Radeon 8060S,
`gfx1151`, 128 GB Unified Memory) mit Ubuntu und `ollama/ollama:0.40.x-rocm`. Zwei Betriebsarten mit gleicher
Ordnerstruktur und gleichem Update-Skript:

| | Docker (empfohlen) | ohne Docker |
|---|---|---|
| Ollama | Container `ollama/ollama:rocm` (oder `:latest` = Vulkan) | offizieller Installer, systemd-Dienst `ollama` |
| Proxy | Container aus diesem Repo (`Dockerfile`) | venv + systemd-User-Unit `ollama-proxy` |
| Treiber auf dem Host | nur Kernel-Treiber `amdgpu` | zusätzlich ROCm 7 (oder Vulkan/Mesa) |
| Update | `scripts/update.sh` bzw. `scripts/deploy.sh` | dasselbe |

```
~/ollama-proxy/
  app/          Git-Klon dieses Repos
  data/         state.json und modules.json des Proxys
  ollama/       nur Docker: Ollama-Daten inkl. Modelle (/root/.ollama)
  venv/         nur ohne Docker: Python für den Proxy
  stack.env     nur Docker: Variablen für docker-compose.yml (Vorlage deploy/stack.env.example)
  ollama.env    optional: weitere Variablen für Ollama (Vorlage deploy/ollama.env.example)
  deploy.env    optional: DEPLOY_MODE=docker|systemd erzwingen
```

## 1. Host vorbereiten

1. **BIOS:** den festen UMA-Framebuffer („VRAM“) klein lassen, z. B. 512 MB–2 GB. Modelle landen dann im GTT, also im
   normalen RAM, den die GPU mitbenutzt.
2. **Kernel-Parameter für GTT.** Ohne sie kann die GPU nur etwa die Hälfte des RAM nutzen. Wert = gewünschte GiB ×
   262144 (Seiten à 4 KiB), etwa 8 GiB für das System übrig lassen:

   | RAM | GTT-Ziel | `pages_limit` |
   |---|---|---|
   | 64 GB | 56 GiB | 14680064 |
   | 96 GB | 88 GiB | 23068672 |
   | 128 GB | 108–120 GiB | 28311552–31457280 |

   Der Parameter heißt je nach Kernel/Treiber `ttm.` oder `amdttm.` (bei amdgpu-dkms). Im Zweifel beide setzen, z. B.
   in `/etc/default/grub`:

   ```
   GRUB_CMDLINE_LINUX_DEFAULT="quiet ttm.pages_limit=28311552 amdttm.pages_limit=28311552"
   ```

   Danach `sudo update-grub` und neu starten. Über `/etc/modprobe.d/` wirkt es nicht zuverlässig.
3. **Prüfen** (ändert nichts):

   ```bash
   mkdir -p ~/ollama-proxy && cd ~/ollama-proxy
   git clone https://github.com/Poldion/OllamaProxy.git app
   bash app/scripts/check-host.sh
   ```

   Erwartet: `/dev/kfd`, `/dev/dri/renderD128`, amdgpu geladen, GTT ≈ RAM minus Puffer.

## 2a. Docker

```bash
cd ~/ollama-proxy
cp app/deploy/stack.env.example stack.env        # PUID/PGID mit `id -u`/`id -g` prüfen
cp app/deploy/ollama.env.example ollama.env      # optional
mkdir -p data ollama
echo "DEPLOY_MODE=docker" > deploy.env

cd app
docker compose --env-file ../stack.env up -d --build
docker compose --env-file ../stack.env logs -f ollama   # GPU-Erkennung prüfen
```

In den Ollama-Logs muss die GPU auftauchen (ROCm: `gfx1151`, Vulkan: `Radeon`). Steht dort nur CPU: in `ollama.env`
`HSA_OVERRIDE_GFX_VERSION=11.5.1` probieren oder auf Vulkan wechseln (`OLLAMA_IMAGE_TAG=latest` in `stack.env`).

**Wichtig:** Der Proxy teilt den PID-Namensraum mit Ollama (`pid: service:ollama`), damit er die Runner-Prozesse für die
Speichermessung sieht. Den Stack deshalb immer komplett neu starten (`docker compose ... up -d`). Wird nur Ollama neu
erstellt, endet der Proxy-Container und bleibt aus.

**Ollama aktualisieren** (`update.sh` baut nur den Proxy):

```bash
cd ~/ollama-proxy/app
# OLLAMA_IMAGE_TAG in ../stack.env anpassen, dann:
docker compose --env-file ../stack.env pull ollama
docker compose --env-file ../stack.env up -d
```

**Parallel arbeiten:** `OLLAMA_NUM_PARALLEL=2` (stack.env) lässt zwei Anfragen je Modell gleichzeitig laufen. Jeder Slot
bekommt den vollen erzwungenen Kontext, der KV-Cache verdoppelt sich also. Weil die Erzeugung von der
Speicherbandbreite begrenzt ist, kosten zwei gleichzeitige Anfragen kaum mehr Zeit als eine.
`OLLAMA_MAX_LOADED_MODELS` muss mindestens so groß sein wie die Zahl der gleichzeitig geladenen Modelle.

## 2b. Ohne Docker

```bash
# Ollama (offizieller Installer; legt den Systemdienst "ollama" an)
curl -fsSL https://ollama.com/install.sh | sh
sudo mkdir -p /etc/systemd/system/ollama.service.d
sudo cp ~/ollama-proxy/app/deploy/linux/ollama-override.conf /etc/systemd/system/ollama.service.d/override.conf
sudo systemctl daemon-reload && sudo systemctl restart ollama
sudo usermod -aG video,render ollama            # falls der Installer das nicht tut

# Proxy
cd ~/ollama-proxy
python3 -m venv venv
venv/bin/pip install -r app/requirements.txt
mkdir -p data
echo "DEPLOY_MODE=systemd" > deploy.env
sudo loginctl enable-linger "$USER"              # User-Dienste ohne Anmeldung
mkdir -p ~/.config/systemd/user
cp app/deploy/linux/ollama-proxy.service ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now ollama-proxy
```

Logs: `journalctl -u ollama -f` bzw. `journalctl --user -u ollama-proxy -f`.

## 3. Updates per Git

Auf dem Rechner: `~/ollama-proxy/app/scripts/update.sh` (holt den neuen Stand, prüft ihn, baut bzw. startet den Proxy
neu, Health-Check, Rollback bei Fehler). Von einem Arbeitsrechner aus: Ziel in `deploy/targets.local.env` eintragen
(Vorlage `deploy/targets.env.example`) und `scripts/deploy.sh --target <name>`. Das Skript prüft, dass lokal und auf
dem Ziel derselbe Branch ausgecheckt ist.

## 4. ROCm oder Vulkan?

Beides ist vorgesehen, umgeschaltet über `OLLAMA_IMAGE_TAG` (`rocm` bzw. `latest`). Laut Community-Tests ist Vulkan
auf dieser iGPU bei der Erzeugung teils schneller, ROCm bei langen Prompts. Empfehlung: mit ROCm beginnen und mit dem
eigenen Modell und Kontext beide messen.

## 5. Speichermessung unter Linux

- RAM/Swap aus `/proc/meminfo`, VRAM/GTT aus `/sys/class/drm/card*/device/mem_info_*`, Runner-Prozesse aus `/proc`.
- Bei Unified Memory zählen GPU-Gewichte im GTT beim Grafiktreiber, nicht im RSS des `llama-server`. Der Proxy rechnet
  je Modell Prozess-RSS + Anteil am GPU-Speicher (nach `size_vram` aus `/api/ps`, abzüglich der Module wie ComfyUI).
- Die Prognose rechnet mit `freies VRAM + min(MemAvailable, freies GTT)`.

## Quellen

- Ollama-Doku GPU: https://github.com/ollama/ollama/blob/main/docs/gpu.mdx
- Ollama-Doku Docker: https://github.com/ollama/ollama/blob/main/docs/docker.mdx
- AMD ROCm-Blog, Ollama auf Ryzen AI Max: https://rocm.blogs.amd.com/artificial-intelligence/ryzen-uma-llm/README.html
- GTT/TTM auf Strix Halo: https://forum.level1techs.com/t/rocm-can-only-use-half-of-the-unified-memory-on-my-strix-halo/253152
- Strix-Halo-Wiki: https://strixhalo.wiki/AI/AI_Capabilities_Overview/
