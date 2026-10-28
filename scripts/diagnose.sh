#!/usr/bin/env bash
#
# Diagnose-Snapshot eines macOS-Ziels (launchd): Proxy-Status, Ollama-Instanzen,
# geladene Modelle, Speicher. Hilft beim Doppel-Ollama-Problem und bei Speicherfragen.
#
# Nutzung:  ./scripts/diagnose.sh [--target name]   (Ziele: deploy/targets.local.env)
#
set -euo pipefail
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET=""
[[ "${1:-}" == "--target" || "${1:-}" == "-t" ]] && TARGET="${2:-}"
[[ -f "${REPO_DIR}/deploy/targets.local.env" ]] && source "${REPO_DIR}/deploy/targets.local.env"
TARGET="${TARGET:-${DEFAULT_TARGET:-}}"
host_var="TARGET_${TARGET}_HOST"
MAC_HOST="${!host_var:-}"
if [[ -z "${MAC_HOST}" ]]; then
  echo "FEHLER: Ziel '${TARGET}' unbekannt (deploy/targets.local.env)." >&2
  exit 1
fi
LABEL="${LAUNCHD_LABEL:-local.ollamaproxy}"

ssh "${MAC_HOST}" LABEL="${LABEL}" bash -s << 'REMOTE'
echo "===== Proxy-Dienst ====="
launchctl print gui/$(id -u)/${LABEL} 2>/dev/null | grep "state =" || echo "  Dienst nicht geladen"
echo "  Deployter Stand: $(git -C ~/ollama-proxy/app log -1 --format='%h %s (%cr)' 2>/dev/null || echo 'kein Git-Klon in ~/ollama-proxy/app')"
git -C ~/ollama-proxy/app status --porcelain 2>/dev/null | sed 's/^/  LOKAL GEAENDERT: /'

echo
echo "===== Ollama-Prozesse (Achtung: mehr als ein 'ollama serve' = Doppel-Instanz) ====="
ps aux | grep -E "[l]lama-server|[o]llama serve" | awk '{printf "  PID %-6s PPID? %-6s %s\n", $2, "-", $11" "$12}'

echo
echo "===== Wer lauscht auf welchem Port ====="
lsof -iTCP -sTCP:LISTEN -P 2>/dev/null | grep -iE "ollama|11434|11435" || echo "  (nichts gefunden)"

echo
echo "===== ollama ps (vom Standard-CLI gesehen) ====="
/Applications/Ollama.app/Contents/Resources/ollama ps

echo
echo "===== Menueleisten-App gesperrt? (erwartet: kein x-Bit) ====="
ls -l /Applications/Ollama.app/Contents/MacOS/Ollama

echo
echo "===== Speicher der llama-server-Prozesse ====="
for pid in $(pgrep -f llama-server); do
  rss=$(ps -o rss= -p "$pid")
  ppid=$(ps -o ppid= -p "$pid")
  echo "  llama-server PID $pid  PPID $ppid  $(( rss / 1024 )) MB"
done
REMOTE
