FROM python:3.11-slim

# Install Tor, curl and ngrok
RUN apt-get update && apt-get install -y --no-install-recommends tor curl ca-certificates && \
    curl -s https://ngrok-agent.s3.amazonaws.com/ngrok.asc | tee /etc/apt/trusted.gpg.d/ngrok.asc >/dev/null && \
    echo "deb https://ngrok-agent.s3.amazonaws.com buster main" | tee /etc/apt/sources.list.d/ngrok.list && \
    apt-get update && apt-get install -y --no-install-recommends ngrok && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install deps first for better layer caching
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY . /app

# Tuned torrc: more circuits, faster bootstrap (generated inline so no extra repo files needed)
RUN printf '%s\n' \
    'SocksPort 9050' \
    'CircuitBuildTimeout 60' \
    'LearnCircuitBuildTimeout 0' \
    'MaxCircuitDirtiness 600' \
    'NewCircuitPeriod 30' \
    'NumEntryGuards 8' \
    'MaxClientCircuitsPending 64' \
    'Log notice stdout' \
    > /etc/tor/torrc

# Entrypoint: start Tor, wait for bootstrap, then launch server + ngrok
RUN printf '%s\n' \
    '#!/usr/bin/env bash' \
    'set -e' \
    'echo "[*] Starting Tor..."' \
    'tor -f /etc/tor/torrc &' \
    'TOR_PID=$!' \
    'echo "[*] Waiting for Tor bootstrap..."' \
    'for i in $(seq 1 60); do' \
    '  if curl --socks5-hostname 127.0.0.1:9050 -s -o /dev/null -m 5 https://check.torproject.org/ ; then' \
    '    echo "[*] Tor is up (after ${i}s)."; break' \
    '  fi; sleep 1' \
    'done' \
    'PORT=${PORT:-8000}' \
    'echo "[*] Launching MCP server on port $PORT..."' \
    'python onion_mcp_server.py &' \
    'SERVER_PID=$!' \
    'echo "[*] Launching ngrok tunnel -> 127.0.0.1:$PORT ..."' \
    'ngrok http --url=legend-caboose-overturn.ngrok-free.dev --pooling-enabled "$PORT" &' \
    'wait -n $TOR_PID $SERVER_PID' \
    > /app/start.sh && chmod +x /app/start.sh

EXPOSE 8000
CMD ["/app/start.sh"]
