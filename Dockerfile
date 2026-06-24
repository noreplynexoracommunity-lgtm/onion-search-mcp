# =========================================================================
#  Blender MCP -- Railway image  (FLAT layout)
# =========================================================================
FROM python:3.11-slim

ENV DEBIAN_FRONTEND=noninteractive \
    BLENDER_VERSION=4.2.3 \
    BLENDER_MAJOR=4.2

RUN apt-get update && apt-get install -y --no-install-recommends \
        curl wget xz-utils ca-certificates gnupg \
        libxi6 libxxf86vm1 libxfixes3 libxrender1 libgl1 libglu1-mesa \
        libsm6 libxkbcommon0 libxcb1 libx11-6 libxext6 \
        libfreetype6 libfontconfig1 \
        libpng16-16 libtiff6 libjpeg62-turbo \
        libsndfile1 libopenal1 libpulse0 \
        libxcb-randr0 libxcb-xfixes0 libxcb-shape0 \
        unzip xvfb \
    && curl -s https://ngrok-agent.s3.amazonaws.com/ngrok.asc \
        | gpg --dearmor -o /etc/apt/trusted.gpg.d/ngrok.gpg \
    && echo "deb https://ngrok-agent.s3.amazonaws.com buster main" \
        > /etc/apt/sources.list.d/ngrok.list \
    && apt-get update && apt-get install -y --no-install-recommends ngrok \
    && rm -rf /var/lib/apt/lists/*

RUN cd /opt && \
    wget -q "https://download.blender.org/release/Blender${BLENDER_MAJOR}/blender-${BLENDER_VERSION}-linux-x64.tar.xz" \
        -O blender.tar.xz && \
    tar -xf blender.tar.xz && rm blender.tar.xz && \
    mv "blender-${BLENDER_VERSION}-linux-x64" blender && \
    ln -s /opt/blender/blender /usr/local/bin/blender

WORKDIR /app
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

# Flat: wszystkie pliki na root /app/
COPY . /app/

RUN mkdir -p /app/output && chmod +x /app/start_blender.sh

ENV PYTHONUNBUFFERED=1 \
    MCP_PORT=8000 \
    PREVIEW_PORT=8001 \
    BLENDER_HOST=127.0.0.1 \
    BLENDER_PORT=9876 \
    PREVIEW_INGEST=http://127.0.0.1:8001/preview/ingest \
    PREVIEW_FPS=8 \
    OUTPUT_DIR=/app/output \
    HOME=/root

EXPOSE 8000 8001

CMD ["/bin/sh", "-c", "\
    if [ -z \"$NGROK_AUTHTOKEN\" ]; then \
        echo 'ERROR: NGROK_AUTHTOKEN nie ustawione'; exit 1; \
    fi && \
    ngrok config add-authtoken \"$NGROK_AUTHTOKEN\" && \
    /app/start_blender.sh & \
    sleep 12 && \
    cd /app && python blender_mcp_server.py & \
    sleep 4 && \
    n=0; \
    until ngrok http 8000 --log=stdout; do \
        n=$((n+1)); \
        if [ $n -ge 20 ]; then echo 'ngrok poddal sie'; exit 1; fi; \
        echo \"[ngrok-retry $n/20] czekam 30s...\"; \
        sleep 30; \
    done \
"]
