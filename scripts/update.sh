#!/usr/bin/env bash
#
# Laeuft AUF DEM ZIELRECHNER. Holt den neuesten Stand per git, prueft ihn und
# startet den Proxy neu. Schlaegt der Health-Check fehl, wird automatisch auf den
# vorherigen Commit zurueckgerollt.
#
# Layout:   ~/ollama-proxy/app        Git-Klon (dieses Repo)
#           ~/ollama-proxy/data       state.json + Logs
#           ~/ollama-proxy/venv       Python-Umgebung (nur launchd/systemd)
#           ~/ollama-proxy/deploy.env optional: DEPLOY_MODE=launchd|systemd|docker,
#                                     LAUNCHD_LABEL (Standard local.ollamaproxy)
#           ~/ollama-proxy/stack.env  nur docker: Variablen fuer docker-compose.yml
#
# Betriebsarten (ohne deploy.env automatisch erkannt):
#   launchd  macOS, launchd-User-Agent
#   systemd  Linux ohne Docker, systemd-User-Unit ollama-proxy.service
#   docker   Linux mit Docker Compose; es wird nur der Proxy-Container neu gebaut,
#            Ollama laeuft weiter
#
# Nutzung:  ~/ollama-proxy/app/scripts/update.sh            (nur wenn es Neues gibt)
#           ~/ollama-proxy/app/scripts/update.sh --force    (Dienst auf jeden Fall neu laden)
#
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BASE_DIR="$(dirname "${REPO_DIR}")"
PORT="${PROXY_PORT:-11435}"
# /admin/state antwortet auch, wenn Ollama selbst gerade nicht erreichbar ist
HEALTH_URL="http://127.0.0.1:${PORT}/admin/state"

# launchd (Label einstellbar: LAUNCHD_LABEL in deploy.env)
PLIST_SRC="${REPO_DIR}/deploy/macos/ollamaproxy.plist"
# systemd (User-Unit, laeuft dank "loginctl enable-linger" auch ohne Login)
UNIT="ollama-proxy.service"
UNIT_SRC="${REPO_DIR}/deploy/linux/${UNIT}"
UNIT_DST="${HOME}/.config/systemd/user/${UNIT}"
# docker
COMPOSE=(docker compose --project-directory "${REPO_DIR}" -f "${REPO_DIR}/docker-compose.yml")
[[ -f "${BASE_DIR}/stack.env" ]] && COMPOSE+=(--env-file "${BASE_DIR}/stack.env")

cd "${REPO_DIR}"

# ---- Betriebsart bestimmen ----
DEPLOY_MODE=""
[[ -f "${BASE_DIR}/deploy.env" ]] && source "${BASE_DIR}/deploy.env"
if [[ -z "${DEPLOY_MODE}" ]]; then
  if [[ "$(uname)" == "Darwin" ]]; then
    DEPLOY_MODE=launchd
  elif [[ -f "${UNIT_DST}" ]]; then
    DEPLOY_MODE=systemd
  elif command -v docker >/dev/null 2>&1; then
    DEPLOY_MODE=docker
  else
    echo "FEHLER: Betriebsart unklar. DEPLOY_MODE in ${BASE_DIR}/deploy.env setzen." >&2
    exit 1
  fi
fi
LABEL="${LAUNCHD_LABEL:-local.ollamaproxy}"
PLIST_DST="${HOME}/Library/LaunchAgents/${LABEL}.plist"
case "${DEPLOY_MODE}" in launchd|systemd|docker) ;; *)
  echo "FEHLER: unbekannte DEPLOY_MODE '${DEPLOY_MODE}'" >&2; exit 1 ;;
esac

# Python fuer Syntax-Check und pip: venv, sonst System-Python (docker-Modus)
PYTHON="${BASE_DIR}/venv/bin/python"
[[ -x "${PYTHON}" ]] || PYTHON="$(command -v python3 || true)"

# ---- Dienst neu laden, je nach Betriebsart ----
restart_launchd() {
  local domain="gui/$(id -u)"
  mkdir -p "${BASE_DIR}/data"   # launchd legt den Log-Ordner nicht selbst an
  sed -e "s#__LABEL__#${LABEL}#g" -e "s#__HOME__#${HOME}#g" "${PLIST_SRC}" > "${PLIST_DST}"
  launchctl bootout "${domain}/${LABEL}" 2>/dev/null || true
  # bootout ist asynchron: warten bis launchd den Job wirklich entfernt hat und der
  # Port frei ist — sonst scheitert bootstrap mit "5: Input/output error"
  for _ in $(seq 1 15); do
    if ! launchctl print "${domain}/${LABEL}" >/dev/null 2>&1 \
       && ! lsof -iTCP:${PORT} -sTCP:LISTEN >/dev/null 2>&1; then
      break
    fi
    sleep 1
  done
  # und trotzdem ein paar Versuche, falls launchd noch nachhaengt
  for attempt in 1 2 3 4 5; do
    launchctl bootstrap "${domain}" "${PLIST_DST}" 2>/dev/null && return 0
    echo "    bootstrap Versuch ${attempt} fehlgeschlagen, warte ..."
    sleep 2
  done
  launchctl bootstrap "${domain}" "${PLIST_DST}"
}

restart_systemd() {
  mkdir -p "${BASE_DIR}/data" "$(dirname "${UNIT_DST}")"
  cp "${UNIT_SRC}" "${UNIT_DST}"
  systemctl --user daemon-reload
  systemctl --user enable --quiet "${UNIT}"
  systemctl --user restart "${UNIT}"
}

restart_docker() {
  mkdir -p "${BASE_DIR}/data"
  # nur den Proxy neu bauen/starten; Ollama (und das geladene Modell) bleibt
  "${COMPOSE[@]}" up -d --build --no-deps proxy
}

restart_service() { "restart_${DEPLOY_MODE}"; }

show_logs() {
  case "${DEPLOY_MODE}" in
    launchd) tail -n 20 "${BASE_DIR}/data/proxy.err" ;;
    systemd) journalctl --user -u "${UNIT}" -n 20 --no-pager ;;
    docker)  "${COMPOSE[@]}" logs --tail 20 proxy ;;
  esac
}

health_ok() {
  for _ in $(seq 1 20); do
    curl -fs -m 3 "${HEALTH_URL}" >/dev/null && return 0
    sleep 1
  done
  return 1
}

# ---- Update holen ----
BRANCH="$(git rev-parse --abbrev-ref HEAD)"
echo "==> Fetch ${BRANCH} von $(git remote get-url origin)  [Modus: ${DEPLOY_MODE}]"
git fetch --quiet origin "${BRANCH}"
OLD=$(git rev-parse HEAD)
NEW=$(git rev-parse "origin/${BRANCH}")
# Rollback-Ziel; beim Neustart nach Selbst-Update vom Vorgaenger uebergeben
ROLLBACK_TO="${UPDATE_ROLLBACK_TO:-${OLD}}"

if [[ "${OLD}" == "${NEW}" && "${1:-}" != "--force" ]]; then
  echo "    Bereits aktuell: $(git log -1 --format='%h %s')"
  exit 0
fi

echo "==> Syntax-Check des neuen Stands"
if [[ -n "${PYTHON}" ]]; then
  git show "${NEW}:ollama_proxy.py" | "${PYTHON}" -c "import ast, sys; ast.parse(sys.stdin.read())"
  echo "    OK"
else
  echo "    kein Python auf dem Host — prueft der Docker-Build"
fi

if [[ "${OLD}" != "${NEW}" ]]; then
  echo "==> Update ${OLD:0:7} -> ${NEW:0:7}"
  git log --oneline "${OLD}..${NEW}" | sed 's/^/    /'
  git merge --ff-only --quiet "origin/${BRANCH}"
  if [[ "${DEPLOY_MODE}" != docker ]] && ! git diff --quiet "${OLD}" "${NEW}" -- requirements.txt; then
    echo "==> requirements.txt geaendert, venv aktualisieren"
    "${PYTHON}" -m pip install --quiet -r requirements.txt
  fi
  # Hat sich dieses Skript selbst geaendert, den Rest mit der neuen Version machen
  if ! git diff --quiet "${OLD}" "${NEW}" -- scripts/update.sh; then
    echo "==> update.sh wurde aktualisiert, starte neue Version"
    UPDATE_ROLLBACK_TO="${OLD}" exec "${REPO_DIR}/scripts/update.sh" --force
  fi
fi

echo "==> Dienst neu laden (${DEPLOY_MODE})"
restart_service

echo "==> Health-Check (${HEALTH_URL})"
if health_ok; then
  echo "    OK, laeuft: $(git log -1 --format='%h %s')"
  exit 0
fi

echo "FEHLER: Proxy antwortet nicht. Letzte Log-Zeilen:" >&2
show_logs >&2 || true

if [[ "${ROLLBACK_TO}" != "$(git rev-parse HEAD)" ]]; then
  echo "==> Rollback auf ${ROLLBACK_TO:0:7}" >&2
  git reset --hard --quiet "${ROLLBACK_TO}"
  restart_service
  if health_ok; then
    echo "    Rollback OK, alter Stand laeuft wieder." >&2
  else
    echo "    Rollback-Stand antwortet ebenfalls nicht — manuell pruefen!" >&2
  fi
fi
exit 1
