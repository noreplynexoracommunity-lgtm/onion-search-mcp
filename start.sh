#!/usr/bin/env bash
set -e

echo "[*] Starting Tor..."
tor -f /etc/tor/torrc &
TOR_PID=$!

# Wait until the SOCKS port actually accepts connections (bootstrap done)
echo "[*] Waiting for Tor bootstrap..."
for i in $(seq 1 60); do
  if curl --socks5-hostname 127.0.0.1:9050 -s -o /dev/null -m 5 https://check.torproject.org/ ; then
    echo "[*] Tor is up (after ${i}s)."
    break
  fi
  sleep 1
done

echo "[*] Launching MCP server..."
python onion_mcp_server.py &
SERVER_PID=$!

echo "[*] Launching ngrok tunnel..."
ngrok http --url=legend-caboose-overturn.ngrok-free.dev 8000 &

# If any core process dies, exit so the container restarts
wait -n $TOR_PID $SERVER_PID
