"""
Darkweb Search MCP Server (v5 - Advanced Edition)
=================================================
Mocno rozszerzony serwer MCP dla Gumloop/Claude.
Transport: streamable HTTP (MCP) /mcp

Nowe ficzery:
 - rotacja IP (NEWNYM via stem)
 - usuwamy Ahmię, dodajemy Tordex, OnionLand, Torch, DeepSearch, itp.
 - per-engine parsery linkow dla precyzji
 - pobieranie linkow golden (Tor.Taxi, Dark.Fail)
 - async pajeczyna (onion_deep_spider) aiohttp
 - scrapowanie formularzy (forum_recon)
 - parsowanie krypto / PGP / zaproszen z tresci stron
"""

import os
import re
import time
import json
import asyncio
import traceback
import concurrent.futures
from urllib.parse import urljoin, urlparse, quote_plus

import requests
from bs4 import BeautifulSoup
from fastmcp import FastMCP
from stem import Signal
from stem.control import Controller
import aiohttp
from aiohttp_socks import ProxyConnector

# --------------------------------------------------------------------------- #
# Konfiguracja bazowa
# --------------------------------------------------------------------------- #

TOR_SOCKS = os.environ.get("TOR_SOCKS", "socks5h://127.0.0.1:9050")
TOR_CTRL_PORT = int(os.environ.get("TOR_CTRL_PORT", "9051"))
TOR_CTRL_PASS = os.environ.get("TOR_CONTROL_PASSWORD", "darkweb-mcp")

PROXIES = {"http": TOR_SOCKS, "https": TOR_SOCKS}
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; rv:115.0) "
    "Gecko/20100101 Firefox/115.0"
)
HEADERS = {"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.5"}

DEFAULT_TIMEOUT = int(os.environ.get("ONION_TIMEOUT", "45"))
MAX_WORKERS = int(os.environ.get("MAX_WORKERS", "10"))

# Regexy
ONION_RE = re.compile(r"[a-z2-7]{16,56}\.onion", re.IGNORECASE)
BTC_RE = re.compile(r"\b(1[a-km-zA-HJ-NP-Z1-9]{25,34}|3[a-km-zA-HJ-NP-Z1-9]{25,34}|bc1[a-zA-HJ-NP-Z0-9]{39,59})\b")
XMR_RE = re.compile(r"\b(4[0-9AB][1-9A-HJ-NP-Za-km-z]{93})\b")
PGP_RE = re.compile(r"-----BEGIN PGP PUBLIC KEY BLOCK-----[\s\S]*?-----END PGP PUBLIC KEY BLOCK-----")
EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
INVITE_RE = re.compile(r"\b(invite|invite[-_]?code|registration[-_]?code)[\s:]+([A-Za-z0-9]{6,32})\b", re.IGNORECASE)


# --------------------------------------------------------------------------- #
# Silniki - Lista
# --------------------------------------------------------------------------- #
SEARCH_ENGINES = [
    # Tordex
    ("Tordex", "http://tordexu73joywapk2txdr54jed4imqledpcvcuf75qsas2gwdgksvnyd.onion/search?query={q}"),
    # Torch
    ("Torch", "http://torchdeedp3i2jigzjdmfpn5ttjhthh5wbmda2rr3jvqjg5p77c54dqd.onion/search?query={q}"),
    # OnionLand
    ("OnionLand", "http://3bbad7fauom4d6sgppalyqddsqbf5u5p56b5k5uk2zxsy3d6ey2jobad.onion/search?q={q}"),
    # DeepSearch
    ("DeepSearch", "http://search7tdrcvri22rieiqgi5t46qnuouptm2c55d4y555q2vmmtt5qd.onion/result.php?search={q}"),
    # Phobos
    ("Phobos", "http://phobosxilamwcg75xt22id7aywkzol6q6rfl2flipcqoc4e4ahima5id.onion/search?q={q}"),
    # Tor66
    ("Tor66", "http://tor66sewebgixwhcqfnp5inzp5x5uohhdy3kvtnyfxc2e5mxiuh34iid.onion/search?q={q}"),
    # Excelsior / Bobby (jako backupy)
    ("Bobby", "http://bobby64o755x3gsuznts6hf6agxqjcz5bop6hs7ejorekbfpdxgnzpid.onion/search.php?term={q}")
]

# Znane domeny (golden sources)
GOLDEN_DIRECTORIES = [
    "http://tortaxi2d6342tld2f2752v3kavf46o45dntgshznt3tnhk42ssy7oad.onion", # Tor.Taxi
    "http://darkfailllnkf4vf.onion", # Dark.fail
    "http://daunt5rnoxeonmtb.onion", # Daunt
]

# Znane community/fora do przeszukiwań CTI
COMMUNITIES = [
    ("Dread", "http://dreadytofatroptsdj6io7l3xptbet6onoyno2yv7jicoxknyazubrad.onion/search?q={q}"),
    ("Pitch", "http://pitchc2zrm7w4r3q.onion/search?q={q}")
]

KNOWN_FORUMS = [
    ("Exploit.in (clearnet)", "https://exploit.in", "Zywe"),
    ("XSS.is (clearnet/onion)", "https://xss.is", "Zywe"),
    ("BreachForums", "http://breachforums...onion (czesto zmienia adresy)", "Rotuje"),
    ("Ramp", "http://ramp...onion", "Zywe/Invite"),
    ("Dread", "http://dreadytofatroptsdj6io7l3xptbet6onoyno2yv7jicoxknyazubrad.onion", "Zywe"),
    ("Cracking.org", "https://cracking.org", "Zywe"),
]

mcp = FastMCP("Darkweb Hacker v5")


# --------------------------------------------------------------------------- #
# Utils
# --------------------------------------------------------------------------- #
def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    s.proxies.update(PROXIES)
    return s

def _fetch(url: str, session: requests.Session, timeout: int = DEFAULT_TIMEOUT):
    try:
        r = session.get(url, timeout=timeout, allow_redirects=True)
        r.raise_for_status()
        return r.text, None
    except Exception as e:
        return None, str(e)

def extract_intel(html: str):
    """Wyciaga PGP, Crypto, Maile z kodu strony"""
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(" ", strip=True)
    raw = html
    
    btc = list(set(BTC_RE.findall(text)))
    xmr = list(set(XMR_RE.findall(text)))
    pgp = list(set(PGP_RE.findall(raw)))
    emails = list(set(EMAIL_RE.findall(text)))
    invites = list(set([m[1] for m in INVITE_RE.findall(text)]))
    
    return {
        "btc": btc,
        "xmr": xmr,
        "pgp": pgp,
        "emails": emails,
        "invites": invites
    }

def _parse_search_results(html: str, base_url: str):
    """Uniwersalny, agresywny parser wynikow na podstawie .onion w href."""
    soup = BeautifulSoup(html, "html.parser")
    found = {}
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        m = ONION_RE.search(href)
        if not m and "onion" not in href:
            continue
            
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
        
        # Omijaj siebie same (wyniki to czesto wlasny url wyszukiwarki dodany do params)
        base_host_m = ONION_RE.search(base_url)
        if base_host_m and host == base_host_m.group(0):
            continue

        if host not in found:
            found[host] = {"title": title, "url": full, "onion": host}
    return list(found.values())


# --------------------------------------------------------------------------- #
# Narzedzia MCP
# --------------------------------------------------------------------------- #

@mcp.tool()
def darkweb__tor_status() -> str:
    """[darkweb] Sprawdza Tor + zwraca aktualne wyjsciowe IP. Uzyj na poczatku sesji."""
    s = _session()
    text, err = _fetch("https://check.torproject.org/api/ip", s, timeout=20)
    if err:
        return f"Tor NIE dziala lub SOCKS5 odrzuca polaczenia: {err}"
    return f"Tor OK. Odpowiedz:\n{text}"


@mcp.tool()
def darkweb__tor_rotate_identity() -> str:
    """[darkweb] Wymusza nowy obwod Tor (NEWNYM). Uzyj gdy serwer cie banuje albo rate-limituje."""
    try:
        with Controller.from_port(port=TOR_CTRL_PORT) as controller:
            controller.authenticate(password=TOR_CTRL_PASS)
            controller.signal(Signal.NEWNYM)
            # Tor wymusza przerwe minimum 10 sekund miedzy zmianami obwodu
            time.sleep(3)
        return "Zadano utworzenie nowego obwodu (NEWNYM). Nowe zapytania poleca z innego IP wyjsciowego."
    except Exception as e:
        return f"Blad ControlPort przy probie NEWNYM: {e}\n(Sprawdz config torrc i upewnij sie, ze usluga dziala)"


@mcp.tool()
def darkweb__known_invite_forums() -> str:
    """[darkweb] Zwraca curated liste znanych forum invite-only / cybercrime + ich status (zywe/martwe)."""
    lines = ["# Znane fora (Hacker's List)"]
    for name, url, status in KNOWN_FORUMS:
        lines.append(f"- **{name}** | {url} | Status: {status}")
    lines.append("\n_Pamietaj, adresy .onion dla prywatnych forum rotuja regularnie - uzywaj darkweb_golden_sources aby zlokalizowac aktualne proxy/mirrory._")
    return "\n".join(lines)


@mcp.tool()
def darkweb__darkweb_multi_search(query: str, max_results_per_engine: int = 20) -> str:
    """
    [darkweb] Rownolegle pyta 8 silnikow .onion (bez Ahmii). Per-engine parsery dla precyzji.
    query: szukana fraza (np. 'leaked databases', 'carding forum', 'fullz')
    """
    q = quote_plus(query)

    def task(engine):
        name, tmpl = engine
        url = tmpl.format(q=q)
        s = _session()
        html, err = _fetch(url, s, timeout=60)
        if err:
            return name, [], err
        results = _parse_search_results(html, url)[:max_results_per_engine]
        return name, results, None

    aggregated = {}
    report_lines = [f"# Wyniki Hakera dla: '{query}'\n"]

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

    report_lines.insert(1, f"Unikalnych domen .onion lacznie: {len(aggregated)}\n")
    return "\n".join(report_lines)


@mcp.tool()
def darkweb__darkweb_golden_sources(max_per_source: int = 50) -> str:
    """
    [darkweb] Pobiera linki z curated katalogow (Tor.Taxi, Dark.Fail, Daunt, Hidden Wiki v3, OnionLinks).
    Najlepsze legitne zrodlo marketplace'ow, mixerow i forum.
    """
    def task(url):
        s = _session()
        html, err = _fetch(url, s, timeout=45)
        if err: return url, [], err
        results = _parse_search_results(html, url)[:max_per_source]
        return url, results, None

    report = ["# Katalogi .onion - Golden Sources"]
    total = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(GOLDEN_DIRECTORIES)) as ex:
        futures = {ex.submit(task, u): u for u in GOLDEN_DIRECTORIES}
        for fut in concurrent.futures.as_completed(futures):
            url, results, err = fut.result()
            if err:
                report.append(f"\n## {url}\n[BLAD] {err}")
                continue
            report.append(f"\n## {url} ({len(results)} linkow)")
            for r in results:
                report.append(f"- {r['title']} -> {r['url']}")
            total += len(results)
    
    report.insert(1, f"Znaleziono lacznie linkow z katalogow: {total}\n")
    return "\n".join(report)


@mcp.tool()
def darkweb__darkweb_community_search(query: str, max_per_source: int = 30) -> str:
    """
    [darkweb] Przeszukuje fora i community real-time: Dread, Pitch.
    Idealne dla swiezych CTI signals -- wycieki, ransomware'owe ogloszenia, vendor opinie.
    """
    q = quote_plus(query)
    def task(engine):
        name, tmpl = engine
        url = tmpl.format(q=q)
        s = _session()
        html, err = _fetch(url, s, timeout=60)
        if err: return name, [], err
        results = _parse_search_results(html, url)[:max_per_source]
        return name, results, None

    report = [f"# Szukam '{query}' w community/forach:"]
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(COMMUNITIES)) as ex:
        futures = {ex.submit(task, e): e for e in COMMUNITIES}
        for fut in concurrent.futures.as_completed(futures):
            name, results, err = fut.result()
            if err:
                report.append(f"\n## {name}\n[BLAD] {err}")
                continue
            report.append(f"\n## {name} ({len(results)} trafien)")
            for r in results:
                report.append(f"- {r['title']} -> {r['url']}")
    
    return "\n".join(report)


@mcp.tool()
def darkweb__onion_fetch(target_url: str, max_text_chars: int = 15000) -> str:
    """
    [darkweb] Agresywnie pobiera strone .onion (lub clearnet przez Tor) i parsuje:
    invite kody, krypto adresy (BTC/XMR/ETH), maile, PGP, linki wew/zew.
    """
    if not target_url.startswith("http"):
        target_url = "http://" + target_url
    s = _session()
    html, err = _fetch(target_url, s)
    if err:
        return f"Blad pobierania {target_url}: {err}"
    
    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.string.strip() if soup.title and soup.title.string else "(brak tytulu)"
    
    intel = extract_intel(html)
    
    for tag in soup(["script", "style", "noscript", "svg", "img"]):
        tag.decompose()
    
    text = re.sub(r"\n{3,}", "\n\n", soup.get_text("\n", strip=True))
    
    report = [
        f"# {title}",
        f"URL: {target_url}",
        "\n## INTEL (Automatycznie wydobyte)",
        f"- BTC Adresy: {len(intel['btc'])} -> {intel['btc']}",
        f"- XMR Adresy: {len(intel['xmr'])} -> {intel['xmr']}",
        f"- Maile: {len(intel['emails'])} -> {intel['emails']}",
        f"- Potencjalne zaproszenia (invites): {intel['invites']}",
        f"- Klucze PGP: {len(intel['pgp'])} znalezionych (sprawdz pelny wynik w zrodle jezeli potrzebujesz)",
        "\n## Tekst",
        f"{text[:max_text_chars]}"
    ]
    if len(text) > max_text_chars:
        report.append(f"\n... (urcieto tekstu. razem znakow: {len(text)})")
        
    return "\n".join(report)


@mcp.tool()
def darkweb__forum_recon(target_url: str) -> str:
    """
    [darkweb] Recon dla forum z zaproszeniem / invite-only:
    - znajduje strony rejestracji / login
    - parsuje formularze (action, method, pola)
    - lapie invite kody w treści
    - wykrywa captcha, Cloudflare, JS-only screen
    """
    if not target_url.startswith("http"): target_url = "http://" + target_url
    s = _session()
    html, err = _fetch(target_url, s, timeout=45)
    if err: return f"[RECON BLAD] {err}"
    
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(" ", strip=True).lower()
    
    report = [f"# Recon: {target_url}"]
    
    # Wykrywanie barier
    barriers = []
    if "captcha" in text: barriers.append("Wykryto slowo 'captcha'")
    if "cloudflare" in text or "ray id" in text: barriers.append("Ochrona Cloudflare (Anti-DDoS)")
    if "ddos-guard" in text: barriers.append("Ochrona DDoS-Guard")
    if "javascript is disabled" in text or "enable javascript" in text: barriers.append("Wymaga JS (Tor Browser standardowo blokuje)")
    if "invite code" in text or "registration code" in text: barriers.append("System Zaproszen-Invite (wymagany kod)")
    
    if barriers:
        report.append("## Detekcja BARIER:")
        for b in barriers: report.append(f"- {b}")
    else:
        report.append("## Detekcja BARIER: Brak oczywistych barier wizualnych (albo pelen dostep).")
        
    # Formularze
    forms = soup.find_all("form")
    report.append(f"\n## Formularze ({len(forms)}):")
    for idx, f in enumerate(forms, 1):
        action = f.get("action", "(brak)")
        method = f.get("method", "get").upper()
        inputs = []
        for inp in f.find_all(["input", "textarea", "select"]):
            name = inp.get("name", "nieznany")
            typ = inp.get("type", "text")
            inputs.append(f"{name} [{typ}]")
        report.append(f"{idx}. {method} -> {action} | Pola: {', '.join(inputs)}")

    # Linki Auth
    auth_links = []
    for a in soup.find_all("a", href=True):
        txt = a.get_text().lower()
        href = a["href"]
        if any(x in txt or x in href.lower() for x in ["login", "signin", "register", "signup", "join", "auth", "invite"]):
            auth_links.append(f"{a.get_text(strip=True)} -> {urljoin(target_url, href)}")
    
    if auth_links:
        report.append("\n## Endpointy Autoryzacji:")
        for al in set(auth_links): report.append(f"- {al}")
        
    # Szybki intel z samej strony wjazdu
    intel = extract_intel(html)
    if intel["invites"]: report.append(f"\n## Wydobyte zaproszenia (raw): {intel['invites']}")
    
    return "\n".join(report)


# Async Pajak
async def fetch_page(session, url):
    try:
        async with session.get(url, timeout=30, allow_redirects=True) as response:
            html = await response.text()
            return url, html, None
    except Exception as e:
        return url, None, str(e)


@mcp.tool()
def darkweb__onion_deep_spider(target_url: str, keyword: str = "", depth: int = 1, max_pages: int = 50, concurrency: int = 5) -> str:
    """
    [darkweb] Asynchroniczny pajak BFS: chodzi po podstronach do glebokosci `depth`,
    zbiera invite'y, slowa kluczowe, sasiadujace domeny .onion.
    Wykonuje rownolegle do `concurrency` requestow.
    """
    if not target_url.startswith("http"): target_url = "http://" + target_url
    
    async def run_spider():
        connector = ProxyConnector.from_url(TOR_SOCKS)
        async with aiohttp.ClientSession(connector=connector, headers=HEADERS) as session:
            visited = set()
            discovered = set()
            hits = []
            intel_found = {"btc": set(), "xmr": set(), "emails": set(), "invites": set()}
            
            queue = [(target_url, 0)]
            page_count = 0
            
            while queue and page_count < max_pages:
                batch = queue[:concurrency]
                queue = queue[concurrency:]
                
                tasks = []
                for (u, lvl) in batch:
                    if u in visited: continue
                    visited.add(u)
                    page_count += 1
                    tasks.append(asyncio.create_task(fetch_page(session, u)))
                
                results = await asyncio.gather(*tasks)
                
                # Odpalamy BeautifulSoup synchronicznie dla zrzuconych stron (mozna wrzucic w process poole ale mcp blokuje)
                for i, (u, html, err) in enumerate(results):
                    lvl = batch[i][1]
                    if err:
                        hits.append(f"[BLAD] {u} -> {err}")
                        continue
                    
                    soup = BeautifulSoup(html, "html.parser")
                    title = soup.title.string.strip() if soup.title and soup.title.string else "(brak)"
                    text = soup.get_text(" ", strip=True)
                    
                    line = f"[L{lvl}] {title} -> {u}"
                    
                    if keyword and keyword.lower() in text.lower():
                        idx = text.lower().find(keyword.lower())
                        ctx = text[max(0, idx - 60): idx + 60].replace("\n", " ")
                        line += f"\n    >>> ZNALEZIONO '{keyword}': ...{ctx}..."
                    hits.append(line)
                    
                    # Zbierz intel
                    pg_intel = extract_intel(html)
                    for k in ["btc", "xmr", "emails", "invites"]:
                        intel_found[k].update(pg_intel[k])
                        
                    # Znajdz linki zewnetrzne onion
                    for a in soup.find_all("a", href=True):
                        full = urljoin(u, a["href"])
                        m = ONION_RE.search(full)
                        if m:
                            discovered.add(m.group(0))
                        # Jesli to ta sama domena i mozemy glebiej
                        base_u = ONION_RE.search(u)
                        if base_u and base_u.group(0) in full and lvl + 1 <= depth:
                            if full not in visited:
                                queue.append((full, lvl + 1))
                                
            return hits, visited, discovered, intel_found

    try:
        hits, visited, discovered, intel_found = asyncio.run(run_spider())
    except Exception as e:
        return f"Blad pajaka: {e}\n{traceback.format_exc()}"
        
    report = [
        f"# Spider skończył -> {target_url}",
        f"Głębokość zadana: {depth} | Skanowano stron: {len(visited)} | Limit: {max_pages}",
        f"Słowo kluczowe: '{keyword or '-'}'",
        "\n## Zgromadzony Intel (Across All Pages):",
        f"- BTC: {len(intel_found['btc'])} znalezionych",
        f"- XMR: {len(intel_found['xmr'])} znalezionych",
        f"- E-maile: {len(intel_found['emails'])} znalezionych",
        f"- Potencjalne Invite'y: {list(intel_found['invites'])[:10]}",
        "\n## Zewnetrzne domeny .onion (Odkryte):",
        f"Odkryto {len(discovered)} domen"
    ]
    if len(discovered) < 30:
        for d in sorted(discovered): report.append(f" - {d}")
    
    report.append("\n## Trasa skanowania:")
    report.extend(hits)
    
    return "\n".join(report)


if __name__ == "__main__":
    host = os.environ.get("MCP_HOST", "0.0.0.0")
    port = int(os.environ.get("MCP_PORT", "8000"))
    print(f"Darkweb Hacker MCP v5 startuje (streamable HTTP) na http://{host}:{port}/mcp")
    print(f"Tor SOCKS proxy: {TOR_SOCKS}")
    mcp.run(transport="http", host=host, port=port, path="/mcp")

