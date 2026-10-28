# OllamaProxy als Container. Gebaut wird er ueber docker-compose.yml,
# laeuft aber auch allein:
#   docker build -t ollama-proxy .
#   docker run -p 11435:11435 -e OLLAMA_UPSTREAM=http://<ollama-host>:11434 \
#              -v ./data:/data ollama-proxy
FROM python:3.12-slim
LABEL org.opencontainers.image.title="OllamaProxy" \
      org.opencontainers.image.source="https://github.com/Poldion/OllamaProxy" \
      org.opencontainers.image.licenses="MIT"

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY ollama_proxy.py i18n.py ./
COPY locales ./locales
COPY static ./static

ENV OLLAMA_UPSTREAM=http://ollama:11434 \
    PROXY_HOST=0.0.0.0 \
    PROXY_PORT=11435 \
    PROXY_STATE=/data/state.json \
    PYTHONUNBUFFERED=1

# /data fuer state.json; beschreibbar fuer jeden User, weil compose den Container
# mit der UID des Host-Users startet (dann gehoert die Datei auf dem Host dir)
RUN mkdir -p /data && chmod 777 /data
VOLUME /data
EXPOSE 11435

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('PROXY_PORT', '11435') + '/admin/state', timeout=4)"]

CMD ["python", "ollama_proxy.py"]
