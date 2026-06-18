# Onion-search MCP server + lokalny Tor demon w jednym obrazie.
# Pozwala zdeployować wszystko jako pojedynczy kontener na Render/Fly/Railway.
FROM python:3.11-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    TOR_SOCKS_PROXY=socks5h://127.0.0.1:9050 \
    PORT=8080

RUN apt-get update \
 && apt-get install -y --no-install-recommends tor curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . /app

# Minimalna konfiguracja Tora — tylko SOCKS5 na 9050.
RUN echo "SocksPort 127.0.0.1:9050\nLog notice stdout\nClientOnly 1" > /etc/tor/torrc.docker

# Entrypoint: odpal tor w tle + uvicorn na froncie.
RUN printf '#!/bin/sh\nset -e\ntor -f /etc/tor/torrc.docker &\nTOR_PID=$!\nsleep 5\nexec python standalone_server.py --host 0.0.0.0 --port "${PORT:-8080}"\n' > /entrypoint.sh \
 && chmod +x /entrypoint.sh

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD curl -fsS http://127.0.0.1:${PORT:-8080}/health || exit 1

ENTRYPOINT ["/entrypoint.sh"]
