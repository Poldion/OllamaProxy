# Running on Linux with an AMD GPU (e.g. Ryzen AI Max “Strix Halo”)

🇬🇧 English · 🇩🇪 [Deutsch](linux-amd.md)

Ollama and the proxy on a headless Linux machine with an AMD GPU. Tested on a Ryzen AI Max+ 395 (iGPU Radeon 8060S,
`gfx1151`, 128 GB unified memory) with Ubuntu and `ollama/ollama:0.40.x-rocm`. Two modes with the same folder layout and
the same update script:

| | Docker (recommended) | without Docker |
|---|---|---|
| Ollama | container `ollama/ollama:rocm` (or `:latest` = Vulkan) | official installer, systemd service `ollama` |
| Proxy | container from this repo (`Dockerfile`) | venv + systemd user unit `ollama-proxy` |
| Drivers on the host | only the kernel driver `amdgpu` | additionally ROCm 7 (or Vulkan/Mesa) |
| Update | `scripts/update.sh` or `scripts/deploy.sh` | the same |

```
~/ollama-proxy/
  app/          Git clone of this repo
  data/         state.json and modules.json of the proxy
  ollama/       Docker only: Ollama data incl. models (/root/.ollama)
  venv/         without Docker only: Python for the proxy
  stack.env     Docker only: variables for docker-compose.yml (template deploy/stack.env.example)
  ollama.env    optional: more variables for Ollama (template deploy/ollama.env.example)
  deploy.env    optional: force DEPLOY_MODE=docker|systemd
```

## 1. Prepare the host

1. **BIOS:** keep the fixed UMA frame buffer (“VRAM”) small, e.g. 512 MB–2 GB. Models then end up in the GTT, i.e. in
   normal RAM that the GPU shares.
2. **Kernel parameters for the GTT.** Without them the GPU can only use about half of the RAM. Value = desired GiB ×
   262144 (pages of 4 KiB); leave about 8 GiB for the system:

   | RAM | GTT target | `pages_limit` |
   |---|---|---|
   | 64 GB | 56 GiB | 14680064 |
   | 96 GB | 88 GiB | 23068672 |
   | 128 GB | 108–120 GiB | 28311552–31457280 |

   Depending on kernel/driver the parameter is called `ttm.` or `amdttm.` (with amdgpu-dkms). If in doubt, set both,
   e.g. in `/etc/default/grub`:

   ```
   GRUB_CMDLINE_LINUX_DEFAULT="quiet ttm.pages_limit=28311552 amdttm.pages_limit=28311552"
   ```

   Then `sudo update-grub` and reboot. Setting it via `/etc/modprobe.d/` does not work reliably.
3. **Check** (changes nothing):

   ```bash
   mkdir -p ~/ollama-proxy && cd ~/ollama-proxy
   git clone https://github.com/Poldion/OllamaProxy.git app
   bash app/scripts/check-host.sh
   ```

   Expected: `/dev/kfd`, `/dev/dri/renderD128`, amdgpu loaded, GTT ≈ RAM minus a buffer.

## 2a. Docker

```bash
cd ~/ollama-proxy
cp app/deploy/stack.env.example stack.env        # check PUID/PGID with `id -u`/`id -g`
cp app/deploy/ollama.env.example ollama.env      # optional
mkdir -p data ollama
echo "DEPLOY_MODE=docker" > deploy.env

cd app
docker compose --env-file ../stack.env up -d --build
docker compose --env-file ../stack.env logs -f ollama   # check GPU detection
```

The GPU must show up in the Ollama logs (ROCm: `gfx1151`, Vulkan: `Radeon`). If only the CPU is listed: try
`HSA_OVERRIDE_GFX_VERSION=11.5.1` in `ollama.env` or switch to Vulkan (`OLLAMA_IMAGE_TAG=latest` in `stack.env`).

**Important:** the proxy shares the PID namespace with Ollama (`pid: service:ollama`) so that it can see the runner
processes for the memory measurement. So always restart the whole stack (`docker compose ... up -d`). If only Ollama is
recreated, the proxy container exits and stays down.

**Updating Ollama** (`update.sh` only builds the proxy):

```bash
cd ~/ollama-proxy/app
# adjust OLLAMA_IMAGE_TAG in ../stack.env, then:
docker compose --env-file ../stack.env pull ollama
docker compose --env-file ../stack.env up -d
```

**Working in parallel:** `OLLAMA_NUM_PARALLEL=2` (stack.env) lets two requests per model run at the same time. Each
slot gets the full enforced context, so the KV cache doubles. Because generation is limited by memory bandwidth, two
simultaneous requests hardly take longer than one. `OLLAMA_MAX_LOADED_MODELS` must be at least the number of models
loaded at the same time.

## 2b. Without Docker

```bash
# Ollama (official installer; creates the system service "ollama")
curl -fsSL https://ollama.com/install.sh | sh
sudo mkdir -p /etc/systemd/system/ollama.service.d
sudo cp ~/ollama-proxy/app/deploy/linux/ollama-override.conf /etc/systemd/system/ollama.service.d/override.conf
sudo systemctl daemon-reload && sudo systemctl restart ollama
sudo usermod -aG video,render ollama            # if the installer does not do it

# Proxy
cd ~/ollama-proxy
python3 -m venv venv
venv/bin/pip install -r app/requirements.txt
mkdir -p data
echo "DEPLOY_MODE=systemd" > deploy.env
sudo loginctl enable-linger "$USER"              # user services without being logged in
mkdir -p ~/.config/systemd/user
cp app/deploy/linux/ollama-proxy.service ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now ollama-proxy
```

Logs: `journalctl -u ollama -f` or `journalctl --user -u ollama-proxy -f`.

## 3. Updates via Git

On the machine: `~/ollama-proxy/app/scripts/update.sh` (fetches the new state, checks it, rebuilds or restarts the
proxy, health check, rollback on error). From a workstation: enter the target in `deploy/targets.local.env` (template
`deploy/targets.env.example`) and run `scripts/deploy.sh --target <name>`. The script checks that the same branch is
checked out locally and on the target.

## 4. ROCm or Vulkan?

Both are supported, switched via `OLLAMA_IMAGE_TAG` (`rocm` or `latest`). According to community tests, Vulkan is
sometimes faster at generation on this iGPU, ROCm with long prompts. Recommendation: start with ROCm and measure both
with your own model and context.

## 5. Memory measurement on Linux

- RAM/swap from `/proc/meminfo`, VRAM/GTT from `/sys/class/drm/card*/device/mem_info_*`, runner processes from `/proc`.
- With unified memory, GPU weights in the GTT count towards the graphics driver, not the RSS of `llama-server`. The
  proxy calculates process RSS + share of GPU memory per model (by `size_vram` from `/api/ps`, minus modules such as
  ComfyUI).
- The forecast uses `free VRAM + min(MemAvailable, free GTT)`.

## Sources

- Ollama docs GPU: https://github.com/ollama/ollama/blob/main/docs/gpu.mdx
- Ollama docs Docker: https://github.com/ollama/ollama/blob/main/docs/docker.mdx
- AMD ROCm blog, Ollama on Ryzen AI Max: https://rocm.blogs.amd.com/artificial-intelligence/ryzen-uma-llm/README.html
- GTT/TTM on Strix Halo: https://forum.level1techs.com/t/rocm-can-only-use-half-of-the-unified-memory-on-my-strix-halo/253152
- Strix Halo wiki: https://strixhalo.wiki/AI/AI_Capabilities_Overview/
