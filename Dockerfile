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

# Konfiguracja ngrok (token wstrzykiwany przez env NGROK_AUTHTOKEN)
COPY ngrok.yml /etc/ngrok.yml

EXPOSE 8000

# 1) Tor jako demon (SOCKS5 na 9050)
# 2) Serwer MCP (streamable HTTP, /mcp na 8000)
# 3) ngrok tunelujacy port 8000 na froncie (utrzymuje kontener przy zyciu)
CMD tor & \
    sleep 8 && \
    python darkweb_mcp_server.py & \
    ngrok start --all --config /etc/ngrok.yml --log=stdout
