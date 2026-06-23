"""
Darkweb Search MCP Server
=========================
Serwer MCP udostepniajacy AI (Gumloop / Claude Desktop / dowolny klient MCP)
zaawansowana wyszukiwarke sieci Tor (.onion).

Transport: streamable HTTP (MCP) -- NIE SSE.
Endpoint:  http://0.0.0.0:8000/mcp

Caly ruch .onion idzie przez lokalny demon Tor (SOCKS5 127.0.0.1:9050).

Narzedzia:
  - tor_status            -> sprawdza czy Tor dziala i jakie ma wyjsciowe IP
  - darkweb_multi_search  -> rownolegle odpytuje wiele bram indeksujacych .onion
  - onion_fetch           -> pobiera surowa tresc + tytul pojedynczej strony .onion
  - onion_deep_spider     -> autonomiczny crawler: wchodzi, wyciaga linki, szuka slow kluczowych

Uruchomienie lokalne:
    pip install -r requirements.txt
    tor &                 # demon Tor musi nasluchiwac na 9050
    python darkweb_mcp_server.py
"""

import os
import re
import time
import concurrent.futures
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from fastmcp import FastMCP

# --------------------------------------------------------------------------- #
# Konfiguracja
# --------------------------------------------------------------------------- #

TOR_SOCKS = os.environ.get("TOR_SOCKS", "socks5h://127.0.0.1:9050")
PROXIES = {"http": TOR_SOCKS, "https": TOR_SOCKS}

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; rv:115.0) "
    "Gecko/20100101 Firefox/115.0"  # profil zblizony do Tor Browser
)
HEADERS = {"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.5"}

DEFAULT_TIMEOUT = int(os.environ.get("ONION_TIMEOUT", "45"))
MAX_WORKERS = int(os.environ.get("MAX_WORKERS", "8"))

ONION_RE = re.compile(r"[a-z2-7]{16,56}\.onion", re.IGNORECASE)

# Bramy indeksujace .onion. Mieszanka clearnet->onion gateway oraz natywnych
# wyszukiwarek .onion. {q} jest podmieniane na zakodowane zapytanie.
# Format krotki: (nazwa, szablon URL, czy wymaga Tor)
SEARCH_ENGINES = [
    ("Ahmia-clearnet", "https://ahmia.fi/search/?q={q}", False),
    ("Ahmia-onion",
     "http://juhanurmihxlp77nkq76byazcldy2hlmovfu2epvl5ankdibsot4csyd.onion/search/?q={q}",
     True),
    ("Torch",
     "http://torchdeedp3i2jigzjdmfpn5ttjhthh5wbmda2rr3jvqjg5p77c54dqd.onion/search?query={q}",
     True),
    ("Tordex",
     "http://tordexu73joywapk2txdr54jed4imqledpcvcuf75qsas2gwdgksvnyd.onion/search?query={q}",
     True),
    ("Excavator",
     "http://2fd6cemt4gmccflhm6imvdfvli3nf7zn6rfrwpsy7uhxrgbypvwf5fad.onion/?q={q}",
     True),
    ("OnionLand",
     "http://3bbad7fauom4d6sgppalyqddsqbf5u5p56b5k5uk2zxsy3d6ey2jobad.onion/search?q={q}",
     True),
    ("Bobby",
     "http://bobby64o755x3gsuznts6hf6agxqjcz5bop6hs7ejorekbfpdxgnzpid.onion/search.php?term={q}",
     True),
]

mcp = FastMCP("Darkweb Search Engine")


# --------------------------------------------------------------------------- #
# Pomocnicze
# --------------------------------------------------------------------------- #

def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    s.proxies.update(PROXIES)
    return s


def _fetch(url: str, session: requests.Session, timeout: int = DEFAULT_TIMEOUT):
    """Pobiera URL. Strony .onion ida przez Tor; clearnet tez (SOCKS5 to przepuszcza)."""
    try:
        r = session.get(url, timeout=timeout, allow_redirects=True)
        r.raise_for_status()
        return r.text, None
    except Exception as e:
        return None, str(e)


def _extract_results(html: str, base_url: str):
    """Wyciaga linki + tytuly + snippety z dowolnej strony wynikow."""
    soup = BeautifulSoup(html, "html.parser")
    found = {}

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        # niektore bramy linkuja przez redirect ?redirect_url= lub /search/redirect
        m = ONION_RE.search(href)
        if not m and "onion" not in href:
            continue
        # zbuduj absolutny url jesli to onion w tekscie
        if m and not href.startswith("http"):
            onion = m.group(0)
            full = "http://" + onion
        else:
            full = urljoin(base_url, href)

        title = a.get_text(strip=True)[:200] or "(brak tytulu)"
        host_m = ONION_RE.search(full)
        if not host_m:
            continue
        host = host_m.group(0)
        if host not in found:
            found[host] = {"title": title, "url": full, "onion": host}

    return list(found.values())


# --------------------------------------------------------------------------- #
# Narzedzia MCP
# --------------------------------------------------------------------------- #

@mcp.tool()
def tor_status() -> str:
    """
    Sprawdza czy lokalny Tor dziala i zwraca wyjsciowe IP wezla Tor.
    Uzyj na poczatku, zeby potwierdzic ze proxy SOCKS5 (9050) odpowiada.
    """
    s = _session()
    text, err = _fetch("https://check.torproject.org/api/ip", s, timeout=30)
    if err:
        return f"Tor NIE dziala lub brak polaczenia: {err}"
    return f"Tor OK. Odpowiedz check.torproject.org:\n{text}"


@mcp.tool()
def darkweb_multi_search(query: str, max_results_per_engine: int = 20) -> str:
    """
    Przeszukuje darknet za pomoca wielu bram indeksujacych jednoczesnie
    w poszukiwaniu linkow .onion powiazanych z zapytaniem.

    query: szukana fraza (np. 'leaked databases', 'marketplace')
    max_results_per_engine: ile wynikow brac z jednej bramy
    """
    from urllib.parse import quote_plus
    q = quote_plus(query)

    def task(engine):
        name, tmpl, _ = engine
        url = tmpl.format(q=q)
        s = _session()
        html, err = _fetch(url, s)
        if err:
            return name, [], err
        results = _extract_results(html, url)[:max_results_per_engine]
        return name, results, None

    aggregated = {}
    report_lines = [f"# Wyniki darkweb dla: '{query}'\n"]

    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = {ex.submit(task, e): e for e in SEARCH_ENGINES}
        for fut in concurrent.futures.as_completed(futures):
            name, results, err = fut.result()
            if err:
                report_lines.append(f"\n## {name}: BLAD -> {err}")
                continue
            report_lines.append(f"\n## {name}: {len(results)} trafien")
            for r in results:
                report_lines.append(f"- {r['title']} -> {r['url']}")
                aggregated[r["onion"]] = r

    report_lines.insert(
        1, f"Unikalnych domen .onion lacznie: {len(aggregated)}\n"
    )
    return "\n".join(report_lines)


@mcp.tool()
def onion_fetch(target_url: str) -> str:
    """
    Pobiera pojedyncza strone .onion przez Tor i zwraca tytul + oczyszczony tekst.
    target_url: pelny adres, np. http://xxxxx.onion/
    """
    if not target_url.startswith("http"):
        target_url = "http://" + target_url
    s = _session()
    html, err = _fetch(target_url, s)
    if err:
        return f"Blad pobierania {target_url}: {err}"
    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.string.strip() if soup.title and soup.title.string else "(brak)"
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    text = re.sub(r"\n{3,}", "\n\n", soup.get_text("\n", strip=True))
    return f"# {title}\nURL: {target_url}\n\n{text[:6000]}"


@mcp.tool()
def onion_deep_spider(target_url: str, keyword: str = "", depth: int = 1) -> str:
    """
    Autonomiczny pajak: wchodzi na strone .onion, wyciaga linki, podaza za nimi
    do zadanej glebokosci i szuka slowa kluczowego w tresci.

    target_url: adres startowy .onion
    keyword: slowo kluczowe do podswietlenia (puste = tylko mapa linkow)
    depth: glebokosc (1-2 zalecane, max 3)
    """
    if not target_url.startswith("http"):
        target_url = "http://" + target_url
    depth = max(1, min(depth, 3))

    visited = set()
    queue = [(target_url, 0)]
    hits = []
    discovered = set()
    s = _session()

    while queue:
        url, lvl = queue.pop(0)
        host_m = ONION_RE.search(url)
        host = host_m.group(0) if host_m else url
        if host in visited or lvl > depth:
            continue
        visited.add(host)

        html, err = _fetch(url, s)
        if err:
            hits.append(f"[BLAD] {url} -> {err}")
            continue

        soup = BeautifulSoup(html, "html.parser")
        title = soup.title.string.strip() if soup.title and soup.title.string else "(brak)"
        page_text = soup.get_text(" ", strip=True)

        line = f"[L{lvl}] {title} -> {url}"
        if keyword and keyword.lower() in page_text.lower():
            idx = page_text.lower().find(keyword.lower())
            ctx = page_text[max(0, idx - 80): idx + 80]
            line += f"\n    >>> ZNALEZIONO '{keyword}': ...{ctx}..."
        hits.append(line)

        # wyciagnij kolejne linki onion
        for a in soup.find_all("a", href=True):
            full = urljoin(url, a["href"])
            m = ONION_RE.search(full)
            if not m:
                continue
            discovered.add(m.group(0))
            if m.group(0) not in visited and lvl + 1 <= depth:
                queue.append((full, lvl + 1))

    report = [f"# Spider start: {target_url}",
              f"Glebokosc: {depth} | Slowo kluczowe: '{keyword or '-'}'",
              f"Odwiedzono stron: {len(visited)} | Odkryto domen .onion: {len(discovered)}",
              "\n## Sciezka skanowania:"]
    report.extend(hits)
    if discovered:
        report.append("\n## Wszystkie odkryte domeny .onion:")
        report.extend(f"- {d}" for d in sorted(discovered))
    return "\n".join(report)


# --------------------------------------------------------------------------- #
# Start serwera -- streamable HTTP (MCP), NIE SSE
# --------------------------------------------------------------------------- #

if __name__ == "__main__":
    host = os.environ.get("MCP_HOST", "0.0.0.0")
    port = int(os.environ.get("MCP_PORT", "8000"))
    print(f"Darkweb MCP startuje (streamable HTTP) na http://{host}:{port}/mcp")
    print(f"Tor SOCKS proxy: {TOR_SOCKS}")
    mcp.run(transport="http", host=host, port=port, path="/mcp")
