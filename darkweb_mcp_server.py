"""
Darkweb MCP Server - Hacker Edition v2
======================================
Async + connection pooling + Tor circuit rotation + per-engine parsers
+ retry/backoff + sqlite cache + dedicated tooling for invite forums.

Transport: streamable HTTP (MCP) na /mcp, port 8000.
"""

import os
import re
import json
import time
import random
import asyncio
import sqlite3
import logging
from pathlib import Path
from urllib.parse import urljoin, urlparse, quote_plus
from typing import Optional

import httpx
from bs4 import BeautifulSoup
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from fastmcp import FastMCP

# Stem dla NEWNYM (opcjonalne -- jesli brak ControlPort, idziemy bez)
try:
    from stem import Signal
    from stem.control import Controller
    STEM_AVAILABLE = True
except ImportError:
    STEM_AVAILABLE = False

# --------------------------------------------------------------------------- #
# Konfig + logging
# --------------------------------------------------------------------------- #

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger("darkweb")

TOR_SOCKS = os.environ.get("TOR_SOCKS", "socks5h://127.0.0.1:9050")
TOR_CONTROL_PORT = int(os.environ.get("TOR_CONTROL_PORT", "9051"))
DEFAULT_TIMEOUT = float(os.environ.get("ONION_TIMEOUT", "60"))
MAX_CONCURRENCY = int(os.environ.get("MAX_CONCURRENCY", "10"))
CACHE_TTL = int(os.environ.get("CACHE_TTL", "1800"))  # 30 min
CACHE_PATH = os.environ.get("CACHE_PATH", "/tmp/darkweb_cache.db")

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; rv:115.0) Gecko/20100101 Firefox/115.0",
    "Mozilla/5.0 (X11; Linux x86_64; rv:115.0) Gecko/20100101 Firefox/115.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:115.0) Gecko/20100101 Firefox/115.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
]

ONION_RE = re.compile(r"[a-z2-7]{16,56}\.onion", re.IGNORECASE)

# Invite regex z kontekstem -- mniej false positives
INVITE_PATTERNS = [
    re.compile(r"(?i)(?:invite|invitation)\s*(?:code|token|key)?\s*[:=]\s*([A-Za-z0-9_-]{8,40})"),
    re.compile(r"(?i)(?:use|enter|your)\s+(?:invite|invitation|referral)[\s:]+([A-Za-z0-9_-]{8,40})"),
    re.compile(r"(?i)referral\s*(?:code|link)?\s*[:=]\s*([A-Za-z0-9_-]{8,40})"),
    re.compile(r"(?i)registration\s+(?:code|key)\s*[:=]\s*([A-Za-z0-9_-]{8,40})"),
]
BTC_RE = re.compile(r"\b(?:bc1[a-z0-9]{25,62}|[13][a-km-zA-HJ-NP-Z1-9]{25,34})\b")
XMR_RE = re.compile(r"\b4[0-9AB][1-9A-HJ-NP-Za-km-z]{93}\b")
ETH_RE = re.compile(r"\b0x[a-fA-F0-9]{40}\b")
EMAIL_RE = re.compile(r"\b[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9.-]+\b")
PGP_RE = re.compile(r"-----BEGIN PGP (?:PUBLIC KEY BLOCK|MESSAGE)-----")

# --------------------------------------------------------------------------- #
# Zlote zrodla i silniki (zero Ahmii, zero martwych v2)
# --------------------------------------------------------------------------- #

GOLDEN_CATALOGS = [
    ("Tor.Taxi",   "http://tortaxi2dev6xjwbaydqzla77rrnth7yn2oqzjfmiuwn5h3uwv2g3sad.onion/"),
    ("Dark.Fail",  "http://darkfailenbsdla5mal2epn2kzeweih2g4nwnw2w2i4t6bov4nmpnqd.onion/"),
    ("Daunt",      "http://dauntfmzcdbnvzeahnfkuckhcz3pgwk3afilhh4vnno6owmf5fkpryqd.onion/"),
    ("HiddenWiki-v3",
     "http://paavlaytlfsqyvkg3yqj7hflfg5jw2jdg2fgkza5ruf6lplwseeqtvyd.onion/"),
    ("OnionLinks", "http://s4k4ceiapwwgcm3mkb6e4diqecpo7kvdnfr5gg7sph7jjppqkvwwqtyd.onion/"),
]

# Format: (name, url_template, parser_key, method)
SEARCH_ENGINES = [
    ("Torch",
     "http://torchdeedp3i2jigzjdmfpn5ttjhthh5wbmda2rr3jvqjg5p77c54dqd.onion/search?query={q}",
     "torch", "GET"),
    ("Tordex",
     "http://tordexu73joywapk2txdr54jed4imqledpcvcuf75qsas2gwdgksvnyd.onion/search?query={q}",
     "tordex", "GET"),
    ("Excavator",
     "http://2fd6cemt4gmccflhm6imvdfvli3nf7zn6rfrwpsy7uhxrgbypvwf5fad.onion/?q={q}",
     "excavator", "GET"),
    ("OnionLand",
     "http://3bbad7fauom4d6sgppalyqddsqbf5u5p56b5k5uk2zxsy3d6ey2jobad.onion/search?q={q}",
     "onionland", "GET"),
    ("Bobby",
     "http://bobby64o755x3gsuznts6hf6agxqjcz5bop6hs7ejorekbfpdxgnzpid.onion/search.php?term={q}",
     "bobby", "GET"),
    ("Haystak",
     "http://haystak5njsmn2hqkewlzpvdndbsqmnfsnmza53rk239hl2gtrasawad.onion/?q={q}",
     "haystak", "GET"),
    ("Tor66",
     "http://tor66sewebgixwhcqfnp5inzp5x5uohhdy3kvtnyfxc2e5mxiuh34iid.onion/search?q={q}",
     "tor66", "GET"),
    ("Submarine",
     "http://no6m4wzdexe3auiupv2zwif7rm6qwxcyhslkcnzisxgeiw6pvjsgafad.onion/search?query={q}",
     "submarine", "GET"),
]

# Specjalistyczne -- fora i community real-time
COMMUNITY_SOURCES = [
    ("Dread",
     "http://dreadytofatroptsdj6io7l3xptbet6onoyno2yv7jicoxknyazubrad.onion/search?q={q}"),
    ("Pitch",
     "http://pitchprvcabqlc2zsync3eqd4owt6ng7ymdik4yi6ee52ekuw2bw4hyd.onion/search?q={q}"),
]

# Znane invite-forums (CTI hot list)
INVITE_FORUMS = [
    ("XSS.is",         "http://xssforumv3isucukbxhdhwz67hoa5e2voakcfkuieq4ch257vsburuid.onion/"),
    ("Exploit",        "http://exploitin5yog4jrtoshqfb5z6rxh6azpkrm4xpenjm6fmvabigsa2yd.onion/"),
    ("DarkForums",     "http://darkforumsxkqcjcw3wxa2blpoxlmwybw7m2tj55nrfd3yzkv6t6vbqd.onion/"),
    ("CryptBB",        "http://cryptbbtg65gibadeeo2awe3j7s6evg7eklserehqr4w4e2bis5tebid.onion/"),
    ("RAID-clones",    "http://raidf4kbgi7c2dbeqx2t7rwocrrslvuawcv7kvecasclb6t5jubvkjyd.onion/"),
]

# --------------------------------------------------------------------------- #
# SQLite cache
# --------------------------------------------------------------------------- #

def _init_cache():
    conn = sqlite3.connect(CACHE_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS cache (
            url TEXT PRIMARY KEY,
            html TEXT,
            ts INTEGER
        )
    """)
    conn.commit()
    return conn

_cache_conn = _init_cache()
_cache_lock = asyncio.Lock()

async def _cache_get(url: str) -> Optional[str]:
    async with _cache_lock:
        row = _cache_conn.execute(
            "SELECT html, ts FROM cache WHERE url = ?", (url,)
        ).fetchone()
    if row and (time.time() - row[1]) < CACHE_TTL:
        return row[0]
    return None

async def _cache_put(url: str, html: str):
    async with _cache_lock:
        _cache_conn.execute(
            "REPLACE INTO cache(url, html, ts) VALUES (?, ?, ?)",
            (url, html, int(time.time()))
        )
        _cache_conn.commit()

# --------------------------------------------------------------------------- #
# Tor circuit rotation
# --------------------------------------------------------------------------- #

_newnym_lock = asyncio.Lock()
_last_newnym = 0.0

async def tor_new_circuit(force: bool = False) -> str:
    """Wymusza nowy obwod Tor przez NEWNYM. Min interwal 10s (limit tora)."""
    global _last_newnym
    if not STEM_AVAILABLE:
        return "stem niedostepny -- pomijam rotacje"
    async with _newnym_lock:
        delta = time.time() - _last_newnym
        if not force and delta < 10:
            await asyncio.sleep(10 - delta)
        try:
            def _do_newnym():
                with Controller.from_port(port=TOR_CONTROL_PORT) as c:
                    c.authenticate()
                    c.signal(Signal.NEWNYM)
            await asyncio.to_thread(_do_newnym)
            _last_newnym = time.time()
            return "NEWNYM OK -- nowy obwod"
        except Exception as e:
            return f"NEWNYM ERROR: {e}"

# --------------------------------------------------------------------------- #
# HTTP client (async, pooled, retry)
# --------------------------------------------------------------------------- #

_clients: dict = {}

def _get_client() -> httpx.AsyncClient:
    """Reuzywalny async client per event loop -- connection pooling."""
    loop = asyncio.get_event_loop()
    if loop not in _clients or _clients[loop].is_closed:
        _clients[loop] = httpx.AsyncClient(
            proxy=TOR_SOCKS,
            timeout=httpx.Timeout(DEFAULT_TIMEOUT, connect=30.0),
            follow_redirects=True,
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            headers={
                "User-Agent": random.choice(USER_AGENTS),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.5",
                "Accept-Encoding": "gzip, deflate",
                "DNT": "1",
                "Upgrade-Insecure-Requests": "1",
                "Connection": "keep-alive",
            },
        )
    return _clients[loop]


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=2, max=20),
    retry=retry_if_exception_type((httpx.HTTPError, httpx.ReadTimeout, httpx.ConnectError)),
    reraise=True
)
async def _http_get(url: str, headers: Optional[dict] = None) -> str:
    client = _get_client()
    h = dict(client.headers)
    if headers:
        h.update(headers)
    # Rotuj UA losowo na 30% requestow
    if random.random() < 0.3:
        h["User-Agent"] = random.choice(USER_AGENTS)
    r = await client.get(url, headers=h)
    r.raise_for_status()
    return r.text


async def _fetch(url: str, use_cache: bool = True) -> tuple[Optional[str], Optional[str]]:
    """Wraper: cache -> http -> on error, jednorazowy NEWNYM i retry raz."""
    if use_cache:
        cached = await _cache_get(url)
        if cached:
            log.debug("cache hit: %s", url)
            return cached, None
    try:
        html = await _http_get(url)
        await _cache_put(url, html)
        return html, None
    except Exception as e1:
        log.warning("fetch fail %s: %s -- proba NEWNYM", url, e1)
        await tor_new_circuit()
        try:
            html = await _http_get(url)
            await _cache_put(url, html)
            return html, None
        except Exception as e2:
            return None, f"{type(e2).__name__}: {e2}"

# --------------------------------------------------------------------------- #
# Per-engine parsers (precision > recall)
# --------------------------------------------------------------------------- #

def _generic_extract(html: str, base_url: str, link_selector: str = "a[href]"):
    soup = BeautifulSoup(html, "lxml")
    found = {}
    for a in soup.select(link_selector):
        href = (a.get("href") or "").strip()
        if not href or href.startswith(("#", "javascript:", "mailto:")):
            continue
        m = ONION_RE.search(href)
        if m and not href.startswith("http"):
            full = "http://" + m.group(0)
        else:
            full = urljoin(base_url, href)
        if not ONION_RE.search(full):
            continue
        # Pomin linki do samej wyszukiwarki
        base_host = ONION_RE.search(base_url)
        target_host = ONION_RE.search(full)
        if base_host and target_host and base_host.group(0) == target_host.group(0):
            # link wewnetrzny wyszukiwarki -- raczej smiec
            if any(seg in full for seg in ("/search", "/?q=", "/page", "/about", "/contact")):
                continue
        title = (a.get_text(strip=True) or "")[:200] or "(brak tytulu)"
        host = ONION_RE.search(full).group(0)
        if host not in found:
            found[host] = {"title": title, "url": full, "onion": host}
    return list(found.values())


PARSERS = {
    "torch":      lambda h, u: _generic_extract(h, u, "dl.search-result a, dl.result a, h5 a"),
    "tordex":     lambda h, u: _generic_extract(h, u, "h5 a, .result h5 a, a"),
    "excavator":  lambda h, u: _generic_extract(h, u, ".result a, .titleline a, a"),
    "onionland":  lambda h, u: _generic_extract(h, u, ".result-block a, .title a, a"),
    "bobby":      lambda h, u: _generic_extract(h, u, ".searchResult a, .result a, a"),
    "haystak":    lambda h, u: _generic_extract(h, u, ".result-link a, .url a, a"),
    "tor66":      lambda h, u: _generic_extract(h, u, "b a, a"),
    "submarine":  lambda h, u: _generic_extract(h, u, ".result a, h3 a, a"),
    "generic":    lambda h, u: _generic_extract(h, u, "a"),
}


# --------------------------------------------------------------------------- #
# Data scrapers (invites, kryptowaluty, PGP, maile)
# --------------------------------------------------------------------------- #

def _scrape_artifacts(html: str) -> dict:
    invites = set()
    for pat in INVITE_PATTERNS:
        invites.update(pat.findall(html))
    return {
        "invites": sorted(invites),
        "btc":     sorted(set(BTC_RE.findall(html))),
        "xmr":     sorted(set(XMR_RE.findall(html))),
        "eth":     sorted(set(ETH_RE.findall(html))),
        "emails":  sorted(set(EMAIL_RE.findall(html))),
        "has_pgp": bool(PGP_RE.search(html)),
    }


def _validate_url(url: str) -> bool:
    """Blokuj file://, ftp://, javascript: itp. Tylko http(s)."""
    try:
        p = urlparse(url)
        return p.scheme in ("http", "https") and bool(p.netloc)
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# MCP setup
# --------------------------------------------------------------------------- #

mcp = FastMCP("Darkweb Hacker Engine v2")


# --------------------------------------------------------------------------- #
# Narzedzia MCP
# --------------------------------------------------------------------------- #

@mcp.tool()
async def tor_status() -> str:
    """Sprawdza Tor + zwraca aktualne wyjsciowe IP. Uzyj na poczatku sesji."""
    # 1) test bezposredniego SOCKS connect
    try:
        async with httpx.AsyncClient(proxy=TOR_SOCKS, timeout=15.0) as c:
            r = await c.get("https://check.torproject.org/api/ip")
            return f"Tor OK. {r.text}"
    except Exception as e:
        # 2) Diagnostyka: czy SOCKS port w ogole odpowiada?
        import socket
        sock_status = "nieosiagalny"
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(3)
            host = "127.0.0.1"
            port = 9050
            # Parsowanie TOR_SOCKS na host:port
            if "://" in TOR_SOCKS:
                hp = TOR_SOCKS.split("://",1)[1]
                if ":" in hp:
                    host = hp.split(":")[0]
                    port = int(hp.split(":")[1])
            s.connect((host, port))
            sock_status = f"OK (port {host}:{port} otwarty)"
            s.close()
        except Exception as se:
            sock_status = f"BLAD: {se}"
        return (f"Tor NIE dziala: {type(e).__name__}: {e}\n"
                f"SOCKS port test: {sock_status}\n"
                f"TOR_SOCKS env: {TOR_SOCKS}")


@mcp.tool()
async def tor_rotate_identity() -> str:
    """Wymusza nowy obwod Tor (NEWNYM). Uzyj gdy serwer cie banuje albo rate-limituje."""
    return await tor_new_circuit(force=True)


@mcp.tool()
async def darkweb_golden_sources(max_per_source: int = 50) -> str:
    """
    Pobiera linki z curated katalogow (Tor.Taxi, Dark.Fail, Daunt, Hidden Wiki v3, OnionLinks).
    Najlepsze legitne zrodlo marketplace'ow, mixerow i forum.
    """
    sem = asyncio.Semaphore(MAX_CONCURRENCY)

    async def task(src):
        name, url = src
        async with sem:
            html, err = await _fetch(url)
            if err:
                return name, [], err
            return name, PARSERS["generic"](html, url)[:max_per_source], None

    results = await asyncio.gather(*(task(s) for s in GOLDEN_CATALOGS))

    report = ["# KATALOGI / ZLOTE ZRODLA DARKWEBU\n"]
    total = 0
    for name, links, err in results:
        if err:
            report.append(f"## {name}: BLAD -> {err}")
            continue
        report.append(f"\n## {name}: {len(links)} legitnych linkow")
        total += len(links)
        for r in links:
            report.append(f"- {r['title']} -> {r['url']}")
    report.insert(1, f"Lacznie zebrano: {total} linkow\n")
    return "\n".join(report)


@mcp.tool()
async def darkweb_multi_search(query: str, max_results_per_engine: int = 20) -> str:
    """
    Rownolegle pyta 8 silnikow .onion (bez Ahmii). Per-engine parsery dla precyzji.
    query: szukana fraza (np. 'leaked databases', 'carding forum', 'fullz')
    """
    q = quote_plus(query)
    sem = asyncio.Semaphore(MAX_CONCURRENCY)

    async def task(engine):
        name, tmpl, parser_key, _ = engine
        url = tmpl.format(q=q)
        async with sem:
            html, err = await _fetch(url)
            if err:
                return name, [], err
            parser = PARSERS.get(parser_key, PARSERS["generic"])
            results = parser(html, url)
            # Fallback: jesli specialized parser dostal < 3 wyniki, sproboj generic
            if len(results) < 3 and parser_key != "generic":
                merged = {r["onion"]: r for r in results}
                for r in PARSERS["generic"](html, url):
                    merged.setdefault(r["onion"], r)
                results = list(merged.values())
            return name, results[:max_results_per_engine], None

    results = await asyncio.gather(*(task(e) for e in SEARCH_ENGINES))

    aggregated = {}
    report_lines = [f"# DARKWEB SEARCH: '{query}'\n"]
    for name, links, err in results:
        if err:
            report_lines.append(f"\n## {name}: BLAD -> {err}")
            continue
        report_lines.append(f"\n## {name}: {len(links)} trafien")
        for r in links:
            report_lines.append(f"- {r['title']} -> {r['url']}")
            aggregated[r["onion"]] = r

    report_lines.insert(1, f"Unikalnych domen .onion: {len(aggregated)}\n")
    return "\n".join(report_lines)


@mcp.tool()
async def darkweb_community_search(query: str, max_per_source: int = 15) -> str:
    """
    Przeszukuje fora i community real-time: Dread, Pitch.
    Idealne dla swiezych CTI signals -- wycieki, ransomware'owe ogloszenia, vendor opinie.
    """
    q = quote_plus(query)

    async def task(src):
        name, tmpl = src
        url = tmpl.format(q=q)
        html, err = await _fetch(url)
        if err:
            return name, [], err
        return name, PARSERS["generic"](html, url)[:max_per_source], None

    results = await asyncio.gather(*(task(s) for s in COMMUNITY_SOURCES))

    report = [f"# COMMUNITY SEARCH: '{query}'\n"]
    for name, links, err in results:
        if err:
            report.append(f"\n## {name}: BLAD -> {err}")
            continue
        report.append(f"\n## {name}: {len(links)} postow/watkow")
        for r in links:
            report.append(f"- {r['title']} -> {r['url']}")
    return "\n".join(report)


@mcp.tool()
async def onion_fetch(target_url: str, max_text_chars: int = 4000) -> str:
    """
    Agresywnie pobiera strone .onion (lub clearnet przez Tor) i parsuje:
    invite kody, krypto adresy (BTC/XMR/ETH), maile, PGP, linki wew/zew.
    """
    if not _validate_url(target_url):
        return f"BLAD: niepoprawny lub niebezpieczny URL: {target_url}"

    html, err = await _fetch(target_url)
    if err:
        return f"Blad pobierania {target_url}: {err}"

    soup = BeautifulSoup(html, "lxml")
    title = (soup.title.string.strip() if soup.title and soup.title.string else "(brak tytulu)")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    text = "\n".join(ln.strip() for ln in soup.get_text().splitlines() if ln.strip())

    art = _scrape_artifacts(html)
    links = PARSERS["generic"](html, target_url)

    out = [
        f"# {title}",
        f"URL: {target_url}",
        f"Rozmiar HTML: {len(html)} | Tekst: {len(text)} | Linki .onion: {len(links)}",
        "\n## ARTEFAKTY:"
    ]
    if art["invites"]: out.append(f"- INVITE: {', '.join(art['invites'])}")
    if art["btc"]:     out.append(f"- BTC: {', '.join(art['btc'][:10])}")
    if art["xmr"]:     out.append(f"- XMR: {', '.join(art['xmr'][:5])}")
    if art["eth"]:     out.append(f"- ETH: {', '.join(art['eth'][:10])}")
    if art["emails"]:  out.append(f"- E-mail: {', '.join(art['emails'][:20])}")
    if art["has_pgp"]: out.append("- PGP key/message: WYKRYTO")

    out.append("\n## TRESC:")
    out.append(text[:max_text_chars])

    if links:
        out.append(f"\n## LINKI .onion (top 30 z {len(links)}):")
        for r in links[:30]:
            out.append(f"- {r['title']} -> {r['url']}")
    return "\n".join(out)


@mcp.tool()
async def onion_deep_spider(
    target_url: str,
    keyword: str = "",
    depth: int = 2,
    max_pages: int = 40,
    concurrency: int = 6,
) -> str:
    """
    Asynchroniczny pajak BFS: chodzi po podstronach do glebokosci `depth`,
    zbiera invite'y, slowa kluczowe, sasiadujace domeny .onion.
    Wykonuje rownolegle do `concurrency` requestow.
    """
    if not _validate_url(target_url):
        return f"BLAD: niepoprawny URL: {target_url}"
    if depth > 3: depth = 3
    if max_pages > 100: max_pages = 100

    visited: set[str] = set()
    discovered_onions: set[str] = set()
    keyword_hits: list[str] = []
    invites_found: set[str] = set()
    pgp_pages: list[str] = []

    base_host_m = ONION_RE.search(target_url)
    if not base_host_m:
        return f"BLAD: URL nie zawiera .onion: {target_url}"
    base_onion = base_host_m.group(0)

    queue: asyncio.Queue = asyncio.Queue()
    await queue.put((target_url, 1))
    sem = asyncio.Semaphore(concurrency)
    kw_lower = keyword.lower() if keyword else ""

    async def worker():
        while True:
            try:
                url, d = await asyncio.wait_for(queue.get(), timeout=2.0)
            except asyncio.TimeoutError:
                return
            try:
                parsed = urlparse(url)
                norm = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
                if norm in visited or len(visited) >= max_pages:
                    continue
                visited.add(norm)
                host_m = ONION_RE.search(url)
                if not host_m:
                    continue

                async with sem:
                    html, err = await _fetch(url)
                if err or not html:
                    continue

                # artefakty
                art = _scrape_artifacts(html)
                for inv in art["invites"]:
                    invites_found.add(f"{inv}  <- {url}")
                if art["has_pgp"]:
                    pgp_pages.append(url)

                # keyword
                if kw_lower:
                    text = BeautifulSoup(html, "lxml").get_text(" ", strip=True).lower()
                    if kw_lower in text:
                        # snippet
                        idx = text.find(kw_lower)
                        snippet = text[max(0, idx-60):idx+len(kw_lower)+60]
                        keyword_hits.append(f"[{url}]\n  ...{snippet}...")

                # rozwiniecie linkow
                if d < depth:
                    soup = BeautifulSoup(html, "lxml")
                    for a in soup.find_all("a", href=True):
                        href = a["href"].strip()
                        if not href or href.startswith(("javascript:", "#", "mailto:")):
                            continue
                        full = urljoin(url, href)
                        m = ONION_RE.search(full)
                        if not m:
                            continue
                        target_onion = m.group(0)
                        if target_onion != base_onion:
                            discovered_onions.add(target_onion)
                        else:
                            await queue.put((full, d + 1))
            finally:
                queue.task_done()

    workers = [asyncio.create_task(worker()) for _ in range(concurrency)]
    await asyncio.gather(*workers, return_exceptions=True)

    report = [
        "# DEEP SPIDER RAPORT",
        f"Start: {target_url}",
        f"Glebokosc: {depth} | Max stron: {max_pages} | Wspolbieznosc: {concurrency}",
        f"Odwiedzono: {len(visited)} | Sasiednie .onion: {len(discovered_onions)}",
        f"Slowo kluczowe: '{keyword or '-'}'",
    ]
    if invites_found:
        report.append(f"\n## INVITE KODY ({len(invites_found)}):")
        report.extend(f"- {i}" for i in sorted(invites_found))
    if keyword_hits:
        report.append(f"\n## TRAFIENIA KEYWORDU ({len(keyword_hits)}):")
        report.extend(keyword_hits[:25])
    if pgp_pages:
        report.append(f"\n## STRONY Z PGP ({len(pgp_pages)}):")
        report.extend(f"- {p}" for p in pgp_pages[:15])
    if discovered_onions:
        report.append(f"\n## ODKRYTE INNE DOMENY .onion ({len(discovered_onions)}):")
        report.extend(f"- {d}" for d in sorted(discovered_onions)[:60])
    return "\n".join(report)


@mcp.tool()
async def forum_recon(target_url: str) -> str:
    """
    Recon dla forum z zaproszeniem / invite-only:
    - znajduje strony rejestracji / login
    - parsuje formularze (action, method, pola)
    - lapie invite kody w treści
    - wykrywa captcha, Cloudflare, JS-only screen
    """
    if not _validate_url(target_url):
        return f"BLAD: niepoprawny URL: {target_url}"

    html, err = await _fetch(target_url)
    if err:
        return f"Blad: {err}"

    soup = BeautifulSoup(html, "lxml")
    text = soup.get_text(" ", strip=True).lower()

    indicators = {
        "invite_only":    any(s in text for s in ["invite only", "invitation required", "by invitation", "invite code required"]),
        "registration_open": any(s in text for s in ["sign up", "register", "create account"]),
        "captcha":        any(s in text for s in ["captcha", "i'm not a robot", "verify you are human"]),
        "cloudflare":     "cloudflare" in text or "cf-ray" in html.lower(),
        "js_required":    any(s in text for s in ["enable javascript", "please enable js", "requires javascript"]),
        "pgp_required":   "pgp" in text and ("required" in text or "verify" in text),
    }

    # Formularze (rejestracja, login, invite redeem)
    forms = []
    for f in soup.find_all("form"):
        action = f.get("action", "")
        method = (f.get("method") or "GET").upper()
        fields = []
        for inp in f.find_all(["input", "textarea", "select"]):
            name = inp.get("name")
            itype = inp.get("type", inp.name)
            if name:
                fields.append(f"{name}({itype})")
        forms.append({
            "action": urljoin(target_url, action) if action else target_url,
            "method": method,
            "fields": fields,
            "context": (f.get_text(" ", strip=True) or "")[:120],
        })

    # Linki kluczowe
    interesting_links = []
    for a in soup.find_all("a", href=True):
        href = a["href"].lower()
        label = a.get_text(strip=True)
        if any(k in href for k in ["register", "signup", "sign-up", "join", "invite", "redeem", "login"]):
            interesting_links.append((label, urljoin(target_url, a["href"])))

    art = _scrape_artifacts(html)

    report = [
        f"# FORUM RECON: {target_url}",
        "\n## WSKAZNIKI:",
    ]
    for k, v in indicators.items():
        report.append(f"- {k}: {'TAK' if v else 'nie'}")

    if art["invites"]:
        report.append(f"\n## INVITE KODY ZNALEZIONE: {', '.join(art['invites'])}")

    if interesting_links:
        report.append(f"\n## LINKI REJESTRACJI/INVITE ({len(interesting_links)}):")
        for label, link in interesting_links[:20]:
            report.append(f"- {label or '(brak labela)'} -> {link}")

    if forms:
        report.append(f"\n## FORMULARZE ({len(forms)}):")
        for i, f in enumerate(forms[:10], 1):
            report.append(
                f"\n[{i}] {f['method']} {f['action']}\n"
                f"    Pola: {', '.join(f['fields'])}\n"
                f"    Kontekst: {f['context']}"
            )

    # Rekomendacja
    report.append("\n## REKOMENDACJA:")
    if indicators["invite_only"]:
        report.append("- Forum INVITE-ONLY. Sprobuj `onion_deep_spider` na zlotych zrodlach i Dread w poszukiwaniu inv-kodow.")
    if indicators["js_required"] or indicators["cloudflare"]:
        report.append("- JS / CF challenge -- wlasciwy fetch przez Playwright (wdrozenie Sprint 2).")
    if indicators["registration_open"] and forms:
        report.append("- Rejestracja OTWARTA -- forma do POST gotowa, mozna automatyzowac.")

    return "\n".join(report)


@mcp.tool()
async def known_invite_forums() -> str:
    """Zwraca curated liste znanych forum invite-only / cybercrime + ich status (zywe/martwe)."""
    sem = asyncio.Semaphore(MAX_CONCURRENCY)

    async def probe(src):
        name, url = src
        async with sem:
            html, err = await _fetch(url, use_cache=False)
            if err:
                return name, url, "DOWN", err
            return name, url, "UP", f"{len(html)} bajtow"

    results = await asyncio.gather(*(probe(s) for s in INVITE_FORUMS))
    report = ["# ZNANE INVITE-ONLY / CYBERCRIME FORA\n"]
    for name, url, status, info in results:
        report.append(f"- [{status}] {name} -> {url}  ({info})")
    return "\n".join(report)


# --------------------------------------------------------------------------- #
# Start (streamable HTTP, /mcp)
# --------------------------------------------------------------------------- #

if __name__ == "__main__":
    host = os.environ.get("MCP_HOST", "0.0.0.0")
    port = int(os.environ.get("MCP_PORT", "8000"))
    log.info("Darkweb MCP v2 startuje na http://%s:%s/mcp (Tor SOCKS=%s)",
             host, port, TOR_SOCKS)
    mcp.run(transport="http", host=host, port=port, path="/mcp")
