#!/usr/bin/env bash
#
# Vorab-Check fuer den Strix Halo (oder jeden Linux-Host mit AMD-GPU).
# Aendert NICHTS, zeigt nur an, ob GPU, Treiber, Speicher-Limits und Docker passen.
#
# Nutzung (auf dem Zielrechner):  bash ~/ollama-proxy/app/scripts/check-host.sh
#
set -uo pipefail

ok()   { printf "  \033[32mOK\033[0m    %s\n" "$*"; }
warn() { printf "  \033[33mWARN\033[0m  %s\n" "$*"; }
bad()  { printf "  \033[31mFEHLT\033[0m %s\n" "$*"; }
gib()  { awk -v b="$1" 'BEGIN { printf "%.1f GiB", b / 1073741824 }'; }

echo "== System =="
echo "  $(uname -srm)"
[[ -r /etc/os-release ]] && echo "  $(. /etc/os-release; echo "${PRETTY_NAME}")"
grep -m1 "model name" /proc/cpuinfo | sed 's/.*: /  CPU: /'
MEM_TOTAL=$(awk '/^MemTotal/ {print $2 * 1024}' /proc/meminfo)
echo "  RAM (fuer Linux sichtbar): $(gib "${MEM_TOTAL}")"

echo
echo "== GPU-Geraete =="
[[ -e /dev/kfd ]] && ok "/dev/kfd vorhanden (ROCm-Compute)" || warn "/dev/kfd fehlt — ROCm geht nicht (Vulkan evtl. schon)"
ls /dev/dri/renderD* >/dev/null 2>&1 && ok "Render-Node: $(ls /dev/dri/renderD* | tr '\n' ' ')" || bad "kein /dev/dri/renderD* — GPU-Treiber (amdgpu) nicht geladen?"
lsmod 2>/dev/null | grep -q '^amdgpu' && ok "Kernelmodul amdgpu geladen" || bad "Kernelmodul amdgpu nicht geladen"
for g in video render; do
  id -nG | tr ' ' '\n' | grep -qx "$g" && ok "User in Gruppe '$g'" \
    || warn "User nicht in Gruppe '$g' (nur fuer Betrieb ohne Docker noetig: sudo usermod -aG $g \$USER)"
done

echo
echo "== GPU-Speicher (amdgpu sysfs) =="
found=0
for dev in /sys/class/drm/card[0-9]*/device; do
  [[ -r "$dev/mem_info_vram_total" ]] || continue
  found=1
  vt=$(cat "$dev/mem_info_vram_total"); gt=$(cat "$dev/mem_info_gtt_total")
  echo "  $(basename "$(dirname "$dev")"): VRAM $(gib "$vt") (im BIOS reserviert), GTT $(gib "$gt") (aus System-RAM)"
  if (( gt < MEM_TOTAL / 2 + 1073741824 )); then
    warn "GTT ist nur ~halber RAM. Fuer grosse Modelle ttm.pages_limit setzen (siehe docs/linux-amd.md)"
  else
    ok "GTT deckt den Grossteil des RAM ab"
  fi
done
(( found )) || bad "keine amdgpu-Karte mit mem_info_* gefunden"
for p in /sys/module/ttm/parameters/pages_limit /sys/module/amdttm/parameters/pages_limit; do
  [[ -r "$p" ]] && echo "  $(echo "$p" | cut -d/ -f4).pages_limit = $(cat "$p") Seiten = $(gib $(( $(cat "$p") * 4096 )))"
done
grep -o 'ttm[^ ]*\|amdttm[^ ]*\|amdgpu\.gttsize[^ ]*' /proc/cmdline | sed 's/^/  Kernel-Parameter: /'

echo
echo "== ROCm / Vulkan auf dem Host (nur fuer Betrieb ohne Docker relevant) =="
command -v rocminfo >/dev/null && { rocminfo 2>/dev/null | grep -m3 -E "Name:\s+gfx" | sed 's/^ */  rocminfo: /'; } \
  || echo "  rocminfo nicht installiert (fuer Docker egal, ROCm steckt im Image)"
command -v vulkaninfo >/dev/null && vulkaninfo --summary 2>/dev/null | grep -m2 -E "deviceName|driverName" | sed 's/^ */  vulkan: /' \
  || echo "  vulkaninfo nicht installiert"

echo
echo "== Docker =="
if command -v docker >/dev/null; then
  ok "$(docker --version)"
  if v=$(docker compose version --short 2>/dev/null); then
    if [[ "$(printf '%s\n2.24.0\n' "${v#v}" | sort -V | head -1)" == "2.24.0" ]]; then
      ok "docker compose ${v}"
    else
      warn "docker compose ${v} — fuer docker-compose.yml wird >= 2.24 gebraucht"
    fi
  else
    bad "docker compose (Plugin) fehlt"
  fi
  docker info >/dev/null 2>&1 && ok "Docker-Daemon erreichbar" || warn "Docker-Daemon nicht erreichbar (laeuft er? User in Gruppe docker?)"
else
  echo "  Docker nicht installiert (ok, wenn ohne Docker betrieben werden soll)"
fi

echo
echo "== systemd (Betrieb ohne Docker) =="
loginctl show-user "$USER" -p Linger 2>/dev/null | grep -q yes && ok "Linger aktiv (User-Dienste laufen ohne Login)" \
  || echo "  Linger aus — fuer den Proxy als User-Dienst: sudo loginctl enable-linger $USER"
systemctl is-active --quiet ollama 2>/dev/null && ok "ollama.service laeuft (nativ)" || echo "  ollama.service nicht aktiv"

echo
echo "== Ports =="
for port in 11434 11435; do
  if ss -ltn 2>/dev/null | awk '{print $4}' | grep -qE "[:.]${port}\$"; then
    echo "  ${port}: belegt ($(ss -ltnp 2>/dev/null | grep -E "[:.]${port} " | grep -o 'users:(("[^"]*' | cut -d'"' -f2 | head -1))"
  else
    echo "  ${port}: frei"
  fi
done
