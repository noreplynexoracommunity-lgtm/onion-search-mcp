FROM python:3.11-slim

# --- System: Tor (do .onion), curl, ngrok ---
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl gnupg tor ca-certificates && \
    curl -s https://ngrok-agent.s3.amazonaws.com/ngrok.asc \
        | gpg --dearmor -o /etc/apt/trusted.gpg.d/ngrok.gpg && \
    echo "deb https://ngrok-agent.s3.amazonaws.com buster main" \
        > /etc/apt/sources.list.d/ngrok.list && \
    apt-get update && apt-get install -y --no-install-recommends ngrok && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt
COPY . /app

EXPOSE 8000

# Start:
# 1) Tor jako demon (SOCKS5 9050)
# 2) Wstrzykniecie tokenu ngrok (rozwijane przez shell, nie przez ngrok config)
# 3) Serwer MCP w tle (streamable HTTP, /mcp na :8000)
# 4) ngrok tunelujacy :8000 (utrzymuje kontener przy zyciu)
CMD ["/bin/sh", "-c", "\
    if [ -z \"$NGROK_AUTHTOKEN\" ]; then \
        echo 'ERROR: zmienna NGROK_AUTHTOKEN jest pusta. Uruchom z -e NGROK_AUTHTOKEN=...'; exit 1; \
    fi && \
    tor & \
    sleep 8 && \
    ngrok config add-authtoken \"$NGROK_AUTHTOKEN\" && \
    python darkweb_mcp_server.py & \
    sleep 3 && \
    ngrok http 8000 --pooling-enabled --log=stdout \
"]
