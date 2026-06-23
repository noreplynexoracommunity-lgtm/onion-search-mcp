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
# 1) Tor demon (SOCKS5 9050)
# 2) Token ngrok ze zmiennej env (Railway: NGROK_AUTHTOKEN)
# 3) Serwer MCP w tle (streamable HTTP, /mcp na :8000)
# 4) ngrok z retry-loop: free tier ma ZAREZERWOWANY domain — jak stary
#    kontener jeszcze go trzyma po redeployu, czekamy i probujemy ponownie
CMD ["/bin/sh", "-c", "\
    if [ -z \"$NGROK_AUTHTOKEN\" ]; then \
        echo 'ERROR: NGROK_AUTHTOKEN nie ustawione'; exit 1; \
    fi && \
    tor & \
    sleep 8 && \
    ngrok config add-authtoken \"$NGROK_AUTHTOKEN\" && \
    python darkweb_mcp_server.py & \
    sleep 3 && \
    n=0; \
    until ngrok http 8000 --log=stdout; do \
        n=$((n+1)); \
        if [ $n -ge 20 ]; then \
            echo 'ngrok nie wstal po 20 probach -- poddaje sie'; \
            exit 1; \
        fi; \
        echo \"[ngrok-retry $n/20] domain nadal zajety, czekam 30s...\"; \
        sleep 30; \
    done \
"]
