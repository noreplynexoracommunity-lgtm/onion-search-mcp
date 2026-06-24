FROM python:3.11-slim

# --- System: Tor (.onion), curl, ngrok, libxml dla lxml ---
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl gnupg tor ca-certificates libxml2-dev libxslt1-dev gcc && \
    curl -s https://ngrok-agent.s3.amazonaws.com/ngrok.asc \
        | gpg --dearmor -o /etc/apt/trusted.gpg.d/ngrok.gpg && \
    echo "deb https://ngrok-agent.s3.amazonaws.com buster main" \
        > /etc/apt/sources.list.d/ngrok.list && \
    apt-get update && apt-get install -y --no-install-recommends ngrok && \
    rm -rf /var/lib/apt/lists/*

# Tor user musi miec dostep do cookie auth
RUN usermod -a -G debian-tor root || true

WORKDIR /app
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt
COPY . /app

# Wlasny torrc z ControlPort dla NEWNYM
RUN cp /app/torrc /etc/tor/torrc

EXPOSE 8000

CMD ["/bin/sh", "-c", "\
    if [ -z \"$NGROK_AUTHTOKEN\" ]; then \
        echo 'ERROR: NGROK_AUTHTOKEN nie ustawione'; exit 1; \
    fi && \
    tor -f /etc/tor/torrc & \
    sleep 10 && \
    chmod 644 /var/lib/tor/control_auth_cookie 2>/dev/null || true && \
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
        echo \"[ngrok-retry $n/20] domain zajety, czekam 30s...\"; \
        sleep 30; \
    done \
"]
