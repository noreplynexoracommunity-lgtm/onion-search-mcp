# Onion Search guMCP Server

Wyszukiwanie i eksploracja dark webu (.onion) jako pełnoprawny serwer
[Model Context Protocol](https://modelcontextprotocol.io/) zgodny z architekturą
[guMCP](https://github.com/gumloop/guMCP).

## Funkcje

| Tool | Opis | Wymaga Tora |
|------|------|-------------|
| `list_search_engines` | Lista obsługiwanych wyszukiwarek darkweb | Nie |
| `search_ahmia` | Ahmia (`https://ahmia.fi`) — domyślnie clearnet | Nie* |
| `search_dark_web` | Multi-engine (Ahmia + Torch) | Tylko gdy `use_tor=true` |
| `fetch_onion` | Pobiera HTML/tekst strony .onion | **Tak** |
| `check_onion_status` | Sprawdza, czy .onion żyje | **Tak** |
| `extract_onion_links` | Ekstrahuje adresy v2/v3 .onion z tekstu | Nie |

\* `use_tor=true` przekierowuje na onionową wersję Ahmii.

## Prerequisites

- Python 3.11+
- Lokalny demon **Tor** nasłuchujący na SOCKS5 (domyślnie `127.0.0.1:9050`)
  dla narzędzi wymagających Tora.

### Linux/macOS

```bash
sudo apt install tor       # Debian/Ubuntu
brew install tor           # macOS
sudo systemctl start tor   # lub: tor &
```

Sprawdzenie: `curl --socks5-hostname 127.0.0.1:9050 https://check.torproject.org`

### Konfiguracja

```bash
# Opcjonalnie, jeśli Tor słucha gdzie indziej:
export TOR_SOCKS_PROXY=socks5h://127.0.0.1:9050
```

## Uruchomienie

### Local (stdio) – zgodnie z konwencją guMCP

Wrzuć folder `src/servers/onion-search/` do swojego klonu `guMCP`, potem:

```bash
python src/servers/local.py --server onion-search --user-id local
```

### Remote (SSE) – serwer wielo-tenant przez `remote.py`

```bash
./start_remote_dev_server.sh
# endpoint:  http://localhost:8000/onion-search/<session_key>
```

### Standalone Streamable HTTP (bez reszty repo)

Repo zawiera wariant `standalone_server.py`, który stawia jeden serwer
Streamable HTTP MCP **tylko z tym jednym serwerem onion-search** – idealny do
deploymentu na Render/Fly/Railway i podłączenia do Gumloopa jako Custom MCP
Server (HTTPS jest tam wymagane).

```bash
pip install -r requirements.txt
python standalone_server.py --host 0.0.0.0 --port 8080
```

## Podłączenie do Gumloopa

1. Zdeployuj `standalone_server.py` (Render/Fly/Railway) z aktywnym demonem
   Tor w tym samym kontenerze – patrz `Dockerfile`.
2. Wystaw HTTPS (Cloudflare Tunnel / reverse proxy ingress platformy).
3. W Gumloop: **Settings → Credentials → MCP Server → Add credential**
   - Label: `onion-search`
   - Server URL: `https://twoj-host/mcp`
   - Access Token: dowolny sekret (opcjonalnie — patrz `MCP_BEARER_TOKEN`)
4. Dodaj serwer do agenta: **Add tools → MCP Server → Custom → onion-search**.

## Bezpieczeństwo & legalność

- Narzędzie służy do legalnego OSINT-u, badań akademickich, audytu
  bezpieczeństwa, dziennikarstwa.
- Nie używaj do dostępu do treści nielegalnych w Twojej jurysdykcji.
- Ahmia automatycznie filtruje CSAM, ale **inne wyszukiwarki – nie**.
  Filtrowanie content-warning jest odpowiedzialnością wywołującego.
- Cały ruch przez Tor jest **wolniejszy** i może wymagać retry; timeouty
  są celowo długie (45 s).

## Architektura

Zgodnie z konwencją guMCP (zobacz `src/servers/perplexity/main.py` jako
referencję):

```
src/servers/onion-search/
├── main.py           # create_server(), server, get_initialization_options()
├── config.yaml       # metadane + lista tools
├── README.md
└── assets/
    └── icon.jpeg
```

`main.py` eksportuje:
- `create_server(user_id, api_key=None) -> Server`
- `server = create_server`  (factory ref – używany przez `local.py`/`remote.py`)
- `get_initialization_options(server_instance) -> InitializationOptions`

Auto-discovery w `src/servers/remote.py::discover_servers()` znajdzie ten
serwer automatycznie po wgraniu folderu.
