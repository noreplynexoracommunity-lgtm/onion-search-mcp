# onion-search-mcp

guMCP-zgodny serwer Model Context Protocol do wyszukiwania i eksploracji
dark webu (`.onion`). Idealny do podłączenia do Gumloopa jako
[Custom MCP Server](https://docs.gumloop.com/nodes/mcp/custom_mcp_servers).

## Struktura repo

```
onion-search-mcp/
├── src/servers/onion-search/      # serwer w konwencji guMCP
│   ├── main.py                    # create_server / list_tools / call_tool
│   ├── config.yaml                # metadane + tools (jak perplexity)
│   ├── README.md                  # dokumentacja serwera
│   └── assets/icon.jpeg
├── standalone_server.py           # Streamable HTTP + SSE w jednym pliku
├── Dockerfile                     # python + tor + uvicorn = jeden kontener
├── requirements.txt
└── README.md                      # ten plik
```

## Tools (6)

- `list_search_engines` – co potrafi serwer
- `search_ahmia` – wyszukiwarka clearnet/onion Ahmii
- `search_dark_web` – multi-engine (Ahmia + Torch)
- `fetch_onion` – pobiera tekst .onion przez Tor
- `check_onion_status` – ping .onion
- `extract_onion_links` – regex v2/v3 .onion z tekstu

## Szybki start lokalnie

```bash
# 1. Zależności
pip install -r requirements.txt

# 2. Tor (do narzędzi .onion)
sudo systemctl start tor    # albo: tor &

# 3. Serwer
python standalone_server.py --host 0.0.0.0 --port 8080

# 4. Smoke test
curl http://127.0.0.1:8080/health
```

Endpoint MCP dla klientów Streamable HTTP: `http://127.0.0.1:8080/mcp`.

## Deploy na Gumloopa

1. Zbuduj obraz: `docker build -t onion-search-mcp .`
2. Wystaw publiczne **HTTPS** (Render, Fly.io, Railway, AWS App Runner,
   Cloudflare Tunnel… cokolwiek). HTTP jest blokowany przez Gumloop.
3. (opcjonalnie) ustaw `MCP_BEARER_TOKEN=<sekret>` żeby wymagać auth.
4. W Gumloop: Settings → Credentials → MCP Server → Add credential
   - URL: `https://<twoj-host>/mcp`
   - Access Token: ten sam `MCP_BEARER_TOKEN`
5. Add tools → MCP Server → Custom → `onion-search` na agencie.

## Integracja z głównym repo guMCP

Folder `src/servers/onion-search/` jest 1:1 zgodny z konwencją guMCP –
wystarczy go skopiować do swojego forka:

```bash
cp -r src/servers/onion-search /path/to/guMCP/src/servers/
python src/servers/local.py --server onion-search --user-id local
```

Auto-discovery w `remote.py::discover_servers()` znajdzie folder
automatycznie.

## Bezpieczeństwo

- Cały ruch do .onion idzie przez Tor SOCKS5 (`TOR_SOCKS_PROXY`).
- Domyślnie Ahmia uderzana jest po clearnet (HTTPS, bez Tora) – szybkie
  i bezpieczne.
- Ahmia filtruje CSAM; Torch/Haystak – nie. Stosuj odpowiednie filtry
  po stronie wywołującego.
- Standalone server obsługuje Bearer-token (`MCP_BEARER_TOKEN`).
