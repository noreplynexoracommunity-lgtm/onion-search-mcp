"""
guMCP server: onion-search

Dark-web (.onion) search & fetch tools.

Implementuje wzorzec guMCP (jak src/servers/perplexity):
  - create_server(user_id, api_key=None) -> mcp.server.Server
  - server = create_server (factory ref używane przez local.py/remote.py)
  - get_initialization_options(server_instance) -> InitializationOptions

Tools:
  - list_search_engines     : zwraca obsługiwane wyszukiwarki dark webu
  - search_ahmia            : wyszukuje przez Ahmia (clearnet, nie wymaga Tor)
  - search_dark_web         : multi-engine (Ahmia + Torch fallback), opcjonalnie przez Tor
  - fetch_onion             : pobiera HTML strony .onion (wymaga TOR_SOCKS_PROXY)
  - check_onion_status      : sprawdza dostępność .onion (wymaga TOR_SOCKS_PROXY)
  - extract_onion_links     : ekstrahuje adresy v2/v3 .onion z dowolnego tekstu
"""

import os
import re
import sys
import logging
from pathlib import Path
from typing import List, Dict, Optional
from urllib.parse import quote_plus, urljoin

import httpx
from bs4 import BeautifulSoup

# Dodaj root projektu do PYTHONPATH (jak robią inne serwery guMCP)
project_root = os.path.abspath(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
)
sys.path.insert(0, project_root)
sys.path.insert(0, os.path.join(project_root, "src"))

from mcp.types import TextContent, Tool
from mcp.server import NotificationOptions, Server
from mcp.server.models import InitializationOptions

SERVICE_NAME = Path(__file__).parent.name

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(SERVICE_NAME)

# ---------------------------------------------------------------------------
# Konfiguracja
# ---------------------------------------------------------------------------

# Tor SOCKS5 proxy. Domyślnie lokalny tor (port 9050). Można nadpisać env.
TOR_SOCKS_PROXY = os.environ.get("TOR_SOCKS_PROXY", "socks5h://127.0.0.1:9050")

# Domyślny user-agent (Tor Browser-like) — żeby nie świecić, że to httpx.
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; rv:115.0) Gecko/20100101 Firefox/115.0"
)

DEFAULT_TIMEOUT = 45.0

SEARCH_ENGINES = {
    "ahmia": {
        "name": "Ahmia",
        "clearnet": "https://ahmia.fi/search/?q={q}",
        "onion": "http://juhanurmihxlp77nkq76byazcldy2hlmovfu2epvl5ankdibsot4csyd.onion/search/?q={q}",
        "needs_tor": False,
        "description": "Najpopularniejszy klaranetowy indeks .onion (filtruje CSAM).",
    },
    "torch": {
        "name": "Torch",
        "clearnet": None,
        "onion": "http://torchdeedp3i2jigzjdmfpn5ttjhthh5wbmda2rr3jvqjg5p77c54dqd.onion/search?query={q}",
        "needs_tor": True,
        "description": "Klasyczna wyszukiwarka .onion (wymaga Tora).",
    },
    "haystak": {
        "name": "Haystak",
        "clearnet": None,
        "onion": "http://haystak5njsmn2hqkewecpaxetahtwhsbsa64jom2k22z5afxhnpxfid.onion/?q={q}",
        "needs_tor": True,
        "description": "Bardzo duży indeks .onion (wymaga Tora).",
    },
    "onionland": {
        "name": "OnionLand",
        "clearnet": None,
        "onion": "http://3bbad7fauom4d6sgppalyqddsqbf5u5p56b5k5uk2zxsy3d6ey2jobad.onion/search?q={q}",
        "needs_tor": True,
        "description": "OnionLand search (wymaga Tora).",
    },
}

# Wzorce .onion (v2: 16 znaków base32, v3: 56 znaków base32)
ONION_V3_RE = re.compile(r"\b([a-z2-7]{56})\.onion\b", re.IGNORECASE)
ONION_V2_RE = re.compile(r"\b([a-z2-7]{16})\.onion\b", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Klienci HTTP
# ---------------------------------------------------------------------------

def _make_client(use_tor: bool, follow_redirects: bool = True) -> httpx.AsyncClient:
    """Buduje httpx.AsyncClient, opcjonalnie podpięty pod Tor SOCKS5."""
    headers = {
        "User-Agent": DEFAULT_UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
        "DNT": "1",
        "Upgrade-Insecure-Requests": "1",
    }
    if use_tor:
        # Wymaga: pip install "httpx[socks]"
        return httpx.AsyncClient(
            proxy=TOR_SOCKS_PROXY,
            timeout=DEFAULT_TIMEOUT,
            headers=headers,
            follow_redirects=follow_redirects,
        )
    return httpx.AsyncClient(
        timeout=DEFAULT_TIMEOUT, headers=headers, follow_redirects=follow_redirects
    )


# ---------------------------------------------------------------------------
# Parsery wyników
# ---------------------------------------------------------------------------

def _parse_ahmia_html(html: str, limit: int) -> List[Dict[str, str]]:
    """Ahmia zwraca <li class='result'> z linkiem do redirect i tytułem."""
    soup = BeautifulSoup(html, "html.parser")
    results: List[Dict[str, str]] = []
    for li in soup.select("li.result"):
        a = li.select_one("h4 a") or li.find("a")
        if not a:
            continue
        title = a.get_text(strip=True)
        href = a.get("href", "")
        # Ahmia zwraca /search/redirect?...&redirect_url=<onion>
        onion_url = href
        m = re.search(r"redirect_url=([^&]+)", href)
        if m:
            from urllib.parse import unquote
            onion_url = unquote(m.group(1))
        cite = li.find("cite")
        snippet_el = li.find("p")
        snippet = snippet_el.get_text(" ", strip=True) if snippet_el else ""
        results.append({
            "title": title or "(no title)",
            "url": onion_url,
            "display_url": cite.get_text(strip=True) if cite else onion_url,
            "snippet": snippet,
        })
        if len(results) >= limit:
            break
    return results


def _parse_torch_html(html: str, limit: int) -> List[Dict[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    results: List[Dict[str, str]] = []
    # Torch nie ma stałej klasy — bierzemy linki .onion z opisem
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if ".onion" not in href:
            continue
        title = a.get_text(strip=True)
        if not title or len(title) < 3:
            continue
        results.append({"title": title, "url": href, "display_url": href, "snippet": ""})
        if len(results) >= limit:
            break
    return results


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

class AhmiaBlocked(Exception):
    """Ahmia odrzuciła zapytanie (redirect na "/", rate-limit albo IP block)."""


async def _search_ahmia(query: str, limit: int = 10, use_tor: bool = False) -> List[Dict[str, str]]:
    url_tpl = SEARCH_ENGINES["ahmia"]["onion" if use_tor else "clearnet"]
    url = url_tpl.format(q=quote_plus(query))
    # Nie podążajmy za przekierowaniem na "/" — to oznacza blokadę.
    async with _make_client(use_tor=use_tor, follow_redirects=False) as client:
        r = await client.get(url)
        if r.status_code in (301, 302, 303, 307, 308):
            loc = r.headers.get("location", "")
            if loc in ("/", url_tpl.split("?")[0]):
                raise AhmiaBlocked(
                    f"Ahmia odrzuciła zapytanie ({r.status_code} → {loc}). "
                    "Prawdopodobnie blokada IP/rate-limit. "
                    "Spróbuj use_tor=true albo wywołaj z innego IP."
                )
            # legalny redirect (np. /search?q -> /search/?q) — podążaj raz
            if loc:
                r = await client.get(urljoin(url, loc))
        r.raise_for_status()
        return _parse_ahmia_html(r.text, limit)


async def _search_torch(query: str, limit: int = 10) -> List[Dict[str, str]]:
    url = SEARCH_ENGINES["torch"]["onion"].format(q=quote_plus(query))
    async with _make_client(use_tor=True) as client:
        r = await client.get(url)
        r.raise_for_status()
        return _parse_torch_html(r.text, limit)


def _format_results(query: str, engine: str, results: List[Dict[str, str]]) -> str:
    if not results:
        return f"Brak wyników dla zapytania '{query}' w {engine}."
    lines = [f"# Wyniki ({engine}) dla: {query}", ""]
    for i, r in enumerate(results, 1):
        lines.append(f"{i}. **{r['title']}**")
        lines.append(f"   URL: {r['url']}")
        if r.get("snippet"):
            lines.append(f"   {r['snippet']}")
        lines.append("")
    return "\n".join(lines)


def _extract_onions(text: str) -> Dict[str, List[str]]:
    v3 = sorted({m.group(0).lower() for m in ONION_V3_RE.finditer(text)})
    # v2 ma luźniejszy regex — odsiej te które są częścią v3
    v2_candidates = {m.group(0).lower() for m in ONION_V2_RE.finditer(text)}
    v3_hosts = set(v3)
    v2 = sorted(h for h in v2_candidates if h not in v3_hosts)
    return {"v3": v3, "v2": v2}


# ---------------------------------------------------------------------------
# Server factory
# ---------------------------------------------------------------------------

def create_server(user_id: Optional[str] = None, api_key: Optional[str] = None) -> Server:
    """Utwórz instancję serwera MCP onion-search."""
    server = Server("onion-search-server")
    server.user_id = user_id
    server.api_key = api_key

    @server.list_tools()
    async def handle_list_tools() -> list[Tool]:
        logger.info(f"Listing onion-search tools for user: {server.user_id}")
        return [
            Tool(
                name="list_search_engines",
                description="Zwraca listę obsługiwanych wyszukiwarek dark webu wraz z informacją czy wymagają Tora.",
                inputSchema={"type": "object", "properties": {}},
            ),
            Tool(
                name="search_ahmia",
                description=(
                    "Wyszukuje strony .onion przez Ahmia. Domyślnie używa clearnetowego "
                    "endpointu (https://ahmia.fi) – nie wymaga Tora. Bezpieczna opcja "
                    "domyślna do eksploracji."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Zapytanie wyszukiwania."},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10},
                        "use_tor": {
                            "type": "boolean",
                            "default": False,
                            "description": "Jeśli true – uderz w wersję .onion Ahmii przez Tor.",
                        },
                    },
                    "required": ["query"],
                },
            ),
            Tool(
                name="search_dark_web",
                description=(
                    "Multi-engine wyszukiwanie dark webu. Domyślnie zaczyna od Ahmii (clearnet); "
                    "jeśli use_tor=true – zapyta dodatkowo Torch przez SOCKS5. Zwraca "
                    "zdeduplikowane wyniki ze wskazaniem silnika."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10},
                        "use_tor": {"type": "boolean", "default": False},
                        "engines": {
                            "type": "array",
                            "items": {"type": "string", "enum": list(SEARCH_ENGINES.keys())},
                            "description": "Jawna lista silników. Pusta = automatyczny dobór.",
                        },
                    },
                    "required": ["query"],
                },
            ),
            Tool(
                name="fetch_onion",
                description=(
                    "Pobiera HTML strony .onion przez Tor SOCKS5 (TOR_SOCKS_PROXY). "
                    "Zwraca tytuł, czysty tekst (do max_chars) i znalezione linki .onion."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "url": {"type": "string", "description": "Pełny URL (musi zawierać .onion)."},
                        "max_chars": {"type": "integer", "minimum": 200, "maximum": 50000, "default": 4000},
                        "include_links": {"type": "boolean", "default": True},
                    },
                    "required": ["url"],
                },
            ),
            Tool(
                name="check_onion_status",
                description="Sprawdza, czy adres .onion odpowiada (HTTP HEAD/GET przez Tor).",
                inputSchema={
                    "type": "object",
                    "properties": {"url": {"type": "string"}},
                    "required": ["url"],
                },
            ),
            Tool(
                name="extract_onion_links",
                description="Ekstrahuje adresy .onion (v2 i v3) z dowolnego tekstu.",
                inputSchema={
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                },
            ),
        ]

    @server.call_tool()
    async def handle_call_tool(name: str, arguments: dict | None) -> List[TextContent]:
        logger.info(f"User {server.user_id} calling tool: {name} args={arguments}")
        arguments = arguments or {}

        try:
            if name == "list_search_engines":
                lines = ["# Obsługiwane wyszukiwarki dark webu", ""]
                for key, eng in SEARCH_ENGINES.items():
                    lines.append(f"## {eng['name']} (`{key}`)")
                    lines.append(f"- {eng['description']}")
                    lines.append(f"- clearnet: {eng['clearnet'] or 'brak'}")
                    lines.append(f"- onion: {eng['onion']}")
                    lines.append(f"- wymaga Tora: {eng['needs_tor']}")
                    lines.append("")
                return [TextContent(type="text", text="\n".join(lines))]

            if name == "search_ahmia":
                q = arguments.get("query")
                if not q:
                    return [TextContent(type="text", text="Error: missing 'query'")]
                limit = int(arguments.get("limit", 10))
                use_tor = bool(arguments.get("use_tor", False))
                try:
                    results = await _search_ahmia(q, limit=limit, use_tor=use_tor)
                except AhmiaBlocked as e:
                    return [TextContent(
                        type="text",
                        text=f"Ahmia blocked: {e}",
                    )]
                return [TextContent(
                    type="text",
                    text=_format_results(q, "Ahmia" + (" (.onion)" if use_tor else " (clearnet)"), results),
                )]

            if name == "search_dark_web":
                q = arguments.get("query")
                if not q:
                    return [TextContent(type="text", text="Error: missing 'query'")]
                limit = int(arguments.get("limit", 10))
                use_tor = bool(arguments.get("use_tor", False))
                engines: List[str] = arguments.get("engines") or (
                    ["ahmia", "torch"] if use_tor else ["ahmia"]
                )

                all_results: List[Dict[str, str]] = []
                seen = set()
                errors: List[str] = []
                for eng in engines:
                    if eng not in SEARCH_ENGINES:
                        errors.append(f"Nieznany silnik: {eng}")
                        continue
                    if SEARCH_ENGINES[eng]["needs_tor"] and not use_tor:
                        errors.append(f"Pomijam {eng}: wymaga use_tor=true.")
                        continue
                    try:
                        if eng == "ahmia":
                            try:
                                res = await _search_ahmia(q, limit=limit, use_tor=use_tor)
                            except AhmiaBlocked as ab:
                                # Auto-fallback: spróbuj przez Tor (jeśli mamy do dyspozycji)
                                if not use_tor:
                                    errors.append(f"ahmia clearnet zablokowany: {ab}")
                                    res = []
                                else:
                                    raise
                        elif eng == "torch":
                            res = await _search_torch(q, limit=limit)
                        else:
                            # generyczny fallback: GET + parse linków .onion
                            url = SEARCH_ENGINES[eng]["onion"].format(q=quote_plus(q))
                            async with _make_client(use_tor=True) as c:
                                r = await c.get(url)
                                r.raise_for_status()
                                res = _parse_torch_html(r.text, limit)
                        for r in res:
                            if r["url"] in seen:
                                continue
                            seen.add(r["url"])
                            r["_engine"] = eng
                            all_results.append(r)
                    except Exception as e:
                        errors.append(f"{eng}: {e}")

                text = _format_results(q, "+".join(engines), all_results)
                if errors:
                    text += "\n\n_Ostrzeżenia:_\n" + "\n".join(f"- {e}" for e in errors)
                return [TextContent(type="text", text=text)]

            if name == "fetch_onion":
                url = arguments.get("url", "")
                if ".onion" not in url:
                    return [TextContent(type="text", text="Error: URL musi zawierać .onion")]
                max_chars = int(arguments.get("max_chars", 4000))
                include_links = bool(arguments.get("include_links", True))
                async with _make_client(use_tor=True) as c:
                    r = await c.get(url)
                    r.raise_for_status()
                    soup = BeautifulSoup(r.text, "html.parser")
                    title = (soup.title.get_text(strip=True) if soup.title else "").strip()
                    for tag in soup(["script", "style", "noscript"]):
                        tag.decompose()
                    text = re.sub(r"\n{3,}", "\n\n", soup.get_text("\n", strip=True))
                    text = text[:max_chars]
                    out = [f"# {title or url}", f"URL: {url}", "", text]
                    if include_links:
                        links = sorted({a["href"] for a in soup.find_all("a", href=True) if ".onion" in a["href"]})
                        if links:
                            out.append("\n## Linki .onion na stronie:")
                            out.extend(f"- {l}" for l in links[:50])
                    return [TextContent(type="text", text="\n".join(out))]

            if name == "check_onion_status":
                url = arguments.get("url", "")
                if ".onion" not in url:
                    return [TextContent(type="text", text="Error: URL musi zawierać .onion")]
                async with _make_client(use_tor=True) as c:
                    try:
                        r = await c.get(url)
                        return [TextContent(
                            type="text",
                            text=f"Status: {r.status_code} {r.reason_phrase}\nURL: {url}\nrozmiar: {len(r.content)} B",
                        )]
                    except Exception as e:
                        return [TextContent(type="text", text=f"OFFLINE / błąd: {e}")]

            if name == "extract_onion_links":
                text = arguments.get("text", "")
                found = _extract_onions(text)
                lines = [f"Znaleziono v3: {len(found['v3'])}, v2: {len(found['v2'])}", ""]
                if found["v3"]:
                    lines.append("## v3 (.onion 56-znaków)")
                    lines.extend(f"- {h}" for h in found["v3"])
                if found["v2"]:
                    lines.append("\n## v2 (.onion 16-znaków, deprecated)")
                    lines.extend(f"- {h}" for h in found["v2"])
                return [TextContent(type="text", text="\n".join(lines))]

            return [TextContent(type="text", text=f"Unknown tool: {name}")]
        except httpx.HTTPError as e:
            logger.exception("HTTP error")
            return [TextContent(type="text", text=f"HTTP error: {e}")]
        except Exception as e:
            logger.exception("Tool error")
            return [TextContent(type="text", text=f"Error: {e}")]

    return server


# Factory ref (jak inne serwery guMCP)
server = create_server


def get_initialization_options(server_instance: Server) -> InitializationOptions:
    return InitializationOptions(
        server_name="onion-search-server",
        server_version="1.0.0",
        capabilities=server_instance.get_capabilities(
            notification_options=NotificationOptions(),
            experimental_capabilities={},
        ),
    )


if __name__ == "__main__":
    print("onion-search guMCP server module.")
    print("Run via guMCP framework:")
    print("  python src/servers/local.py --server onion-search --user-id local")
    print("  ./start_remote_dev_server.sh")
