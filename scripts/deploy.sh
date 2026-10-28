#!/usr/bin/env bash
#
# Deploy vom Arbeitsrechner aus: pusht den aktuellen Branch und stoesst auf dem
# Zielrechner scripts/update.sh an (git pull, Syntax-Check, Dienst-Neustart,
# Health-Check, bei Fehler Rollback).
#
# Nutzung:  ./scripts/deploy.sh                    Standardziel (DEFAULT_TARGET)
#           ./scripts/deploy.sh --target mac       anderes Ziel
#           ./scripts/deploy.sh --force            Dienst auf jeden Fall neu laden
#
# Ziele stehen in deploy/targets.local.env (nicht im Repo), Vorlage:
# deploy/targets.env.example.
#
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${REPO_DIR}"

TARGET=""
PASS=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --target|-t) TARGET="$2"; shift 2 ;;
    *) PASS+=("$1"); shift ;;
  esac
done

if [[ ! -f deploy/targets.local.env ]]; then
  echo "FEHLER: deploy/targets.local.env fehlt (Vorlage: deploy/targets.env.example)." >&2
  exit 1
fi
source deploy/targets.local.env
TARGET="${TARGET:-${DEFAULT_TARGET:-}}"

host_var="TARGET_${TARGET}_HOST"; repo_var="TARGET_${TARGET}_REPO"; url_var="TARGET_${TARGET}_URL"
HOST="${!host_var:-}"; REPO="${!repo_var:-~/ollama-proxy/app}"; URL="${!url_var:-}"
if [[ -z "${HOST}" ]]; then
  echo "FEHLER: Ziel '${TARGET}' unbekannt — TARGET_${TARGET}_HOST in deploy/targets.local.env eintragen." >&2
  exit 1
fi

BRANCH="$(git rev-parse --abbrev-ref HEAD)"

echo "==> Arbeitsverzeichnis pruefen"
if [[ -n "$(git status --porcelain)" ]]; then
  echo "FEHLER: Es gibt nicht committete Aenderungen — erst committen:" >&2
  git status --short >&2
  exit 1
fi
REMOTE_BRANCH="$(ssh "${HOST}" "git -C ${REPO} rev-parse --abbrev-ref HEAD")"
if [[ "${REMOTE_BRANCH}" != "${BRANCH}" ]]; then
  echo "FEHLER: Lokal Branch '${BRANCH}', auf ${TARGET} ist '${REMOTE_BRANCH}' ausgecheckt." >&2
  echo "        Erst lokal wechseln oder auf dem Ziel 'git switch ${BRANCH}'." >&2
  exit 1
fi

echo "==> Syntax-Check"
# Unter Windows ist "python3" oft nur der Microsoft-Store-Platzhalter -> echten Interpreter suchen
PY=""
for cand in python3 python py; do
  if "${cand}" -c "import sys" >/dev/null 2>&1; then PY="${cand}"; break; fi
done
if [[ -n "${PY}" ]]; then
  "${PY}" -c "import ast; ast.parse(open('ollama_proxy.py', encoding='utf-8').read())"
  echo "    OK (${PY})"
else
  echo "    kein lokales Python gefunden — Check laeuft nur auf dem Ziel"
fi

echo "==> Push ${BRANCH} nach $(git remote get-url origin)"
git push origin "${BRANCH}"

echo "==> Update auf ${TARGET} (${HOST})"
ssh "${HOST}" "${REPO}/scripts/update.sh ${PASS[*]:-}"

echo "==> Fertig.${URL:+ UI: ${URL}}"
