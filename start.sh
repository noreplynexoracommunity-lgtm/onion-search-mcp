#!/bin/sh
# Startup script: tor -> wait -> mcp -> ngrok
set -e

if [ -z "$NGROK_AUTHTOKEN" ]; then
    echo "FATAL: NGROK_AUTHTOKEN nie ustawione"
    exit 1
fi

# 1) Tor data dir z poprawnymi prawami
mkdir -p /tmp/tor-data
chmod 700 /tmp/tor-data

echo "[start] Uruchamiam Tor..."
tor -f /etc/tor/torrc > /tmp/tor.log 2>&1 &
TOR_PID=$!
echo "[start] Tor PID=$TOR_PID"

# 2) Czekaj az Tor otworzy SOCKS na 9050 (max 90s)
echo "[start] Czekam az Tor zbuduje obwod..."
for i in $(seq 1 90); do
    if grep -q "Bootstrapped 100" /tmp/tor.log 2>/dev/null; then
        echo "[start] Tor BOOTSTRAPPED po ${i}s"
        break
    fi
    if ! kill -0 $TOR_PID 2>/dev/null; then
        echo "[start] FATAL: Tor padl. Logi:"
        cat /tmp/tor.log
        exit 1
    fi
    sleep 1
done

# Sanity: ostatnie linie logu
tail -5 /tmp/tor.log

# 3) Cookie auth -- daj readable dla naszego usera (jestesmy root)
chmod 644 /tmp/tor-data/control_auth_cookie 2>/dev/null || true

# 4) ngrok authtoken
ngrok config add-authtoken "$NGROK_AUTHTOKEN"

# 5) Start MCP w tle
echo "[start] Startuje MCP..."
python /app/darkweb_mcp_server.py > /tmp/mcp.log 2>&1 &
MCP_PID=$!
sleep 3

if ! kill -0 $MCP_PID 2>/dev/null; then
    echo "[start] FATAL: MCP padl. Logi:"
    cat /tmp/mcp.log
    exit 1
fi
echo "[start] MCP PID=$MCP_PID"

# 6) ngrok tunnel z retry (free tier reserved domain busy po redeployu)
n=0
until ngrok http 8000 --log=stdout; do
    n=$((n+1))
    if [ $n -ge 20 ]; then
        echo "[start] ngrok nie wstal po 20 probach"
        exit 1
    fi
    echo "[start ngrok-retry $n/20] domain busy, czekam 30s..."
    sleep 30
done
