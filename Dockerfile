FROM python:3.11-slim

# --- System: Tor (do .onion), curl, ngrok ---
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl gnupg tor obfs4proxy ca-certificates && \
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

# Hashujemy haslo Tor controlportu z env (TOR_CONTROL_PASSWORD; default: darkweb-mcp)
# i wrzucamy do /etc/tor/torrc.
ENV TOR_CONTROL_PASSWORD=darkweb-mcp

EXPOSE 8000

CMD ["/bin/sh", "-c", "\
    if [ -z \"$NGROK_AUTHTOKEN\" ]; then \
        echo 'ERROR: NGROK_AUTHTOKEN nie ustawione'; exit 1; \
    fi && \
    HASH=$(tor --hash-password \"$TOR_CONTROL_PASSWORD\" | tail -n1) && \
    cp /app/torrc /etc/tor/torrc && \
    sed -i \"s|^HashedControlPassword.*|HashedControlPassword $HASH|\" /etc/tor/torrc && \
    tor -f /etc/tor/torrc & \
    sleep 10 && \
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
