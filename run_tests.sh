#!/usr/bin/env bash
# Tests des Proxys im Proxy-Image (gleiche Python-/Paketversionen wie im Betrieb).
# Braucht nur Docker; Ollama wird nachgebaut (tests/fake_ollama.py).
#   ./run_tests.sh [pytest-Optionen, z.B. -k openai -x]
set -euo pipefail
cd "$(dirname "$0")"
docker build -q -t ollama-proxy:test . >/dev/null
docker run --rm -v "$PWD":/src:ro -e PYTHONDONTWRITEBYTECODE=1 -w /tmp ollama-proxy:test \
  sh -c 'pip install -q --disable-pip-version-check --no-cache-dir pytest==8.3.4 >/dev/null 2>&1 &&
         cp -r /src /tmp/proxy && cd /tmp/proxy && python -m pytest -q -p no:cacheprovider tests "$@"' sh "$@"
