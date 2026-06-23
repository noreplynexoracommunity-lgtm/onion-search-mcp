# Darkweb Search MCP Server

Serwer MCP udostepniajacy zaawansowana wyszukiwarke sieci Tor (`.onion`).
Transport: **streamable HTTP (MCP)** — nie SSE. Endpoint: `/mcp`.

## Narzedzia
| Narzedzie | Opis |
|---|---|
| `tor_status` | Sprawdza czy Tor dziala i pokazuje wyjsciowe IP |
| `darkweb_multi_search` | Rownolegle odpytuje 7 bram indeksujacych `.onion` |
| `onion_fetch` | Pobiera tytul + tekst pojedynczej strony `.onion` |
| `onion_deep_spider` | Crawler: podaza za linkami, szuka slowa kluczowego |

## Uruchomienie lokalne
```bash
pip install -r requirements.txt
tor &                      # demon Tor na porcie 9050
python darkweb_mcp_server.py
# -> http://localhost:8000/mcp
```

## Docker + ngrok (zdalny dostep)
```bash
docker build -t darkweb-mcp .
docker run -e NGROK_AUTHTOKEN=twoj_token darkweb-mcp
```
Token jest wstrzykiwany do ngroka przez `ngrok config add-authtoken` w starcie kontenera —
nie ma pliku `ngrok.yml` z `${NGROK_AUTHTOKEN}` (ngrok nie rozwija zmiennych w configu).

Ngrok wypisze publiczny URL — dolacz `/mcp` na koncu i wpisz w kliencie MCP (Gumloop / Claude Desktop).

## Zmienne srodowiskowe
| Zmienna | Domyslnie | Opis |
|---|---|---|
| `NGROK_AUTHTOKEN` | — | token ngrok (wymagany w Dockerze) |
| `TOR_SOCKS` | `socks5h://127.0.0.1:9050` | adres proxy Tor |
| `MCP_PORT` | `8000` | port serwera |
| `ONION_TIMEOUT` | `45` | timeout requestow w sek. |
| `MAX_WORKERS` | `8` | watki przy multi-search |

## Podlaczenie w kliencie MCP (streamable HTTP)
```json
{
  "mcpServers": {
    "darkweb": {
      "url": "https://twoj-ngrok-url.ngrok-free.app/mcp"
    }
  }
}
```
