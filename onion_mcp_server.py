import os
import re
import time
import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from fastapi import FastAPI, Request
import uvicorn

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("onion-crawler")

app = FastAPI()

# ----------------------------------------------------------------------------
# Configuration (overridable via environment variables)
# ----------------------------------------------------------------------------
TOR_SOCKS = os.getenv("TOR_SOCKS", "socks5h://127.0.0.1:9050")
TOR_PROXIES = {"http": TOR_SOCKS, "https": TOR_SOCKS}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; rv:115.0) Gecko/20100101 Firefox/115.0",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}

REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "30"))
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "3"))
RETRY_BACKOFF = float(os.getenv("RETRY_BACKOFF", "2.0"))
MAX_WORKERS = int(os.getenv("MAX_WORKERS", "8"))
MAX_PAGES = int(os.getenv("MAX_PAGES", "200"))          # hard cap on pages per crawl
MAX_OUTPUT_CHARS = int(os.getenv("MAX_OUTPUT_CHARS", "40000"))

# v3 onion = 56 chars, legacy v2 = 16 chars. Anchored to base32 alphabet [a-z2-7].
ONION_RE = re.compile(r"(?:https?://)?([a-z2-7]{16}|[a-z2-7]{56})\.onion", re.IGNORECASE)

# ----------------------------------------------------------------------------
# Shared HTTP session (connection pooling over Tor)
# ----------------------------------------------------------------------------
def build_session() -> requests.Session:
    s = requests.Session()
    s.proxies.update(TOR_PROXIES)
    s.headers.update(HEADERS)
    return s


def fetch(session: requests.Session, url: str, timeout: int = REQUEST_TIMEOUT):
    """GET with retries + exponential backoff. Returns Response or None."""
    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            res = session.get(url, timeout=timeout, allow_redirects=True)
            if res.status_code == 200:
                return res
            last_err = f"HTTP {res.status_code}"
        except Exception as e:  # noqa: BLE001
            last_err = str(e)
        if attempt < MAX_RETRIES:
            time.sleep(RETRY_BACKOFF * attempt)
    log.warning("fetch failed %s (%s)", url, last_err)
    return None


# ----------------------------------------------------------------------------
# Parsing helpers
# ----------------------------------------------------------------------------
def normalize_onion(raw: str) -> str | None:
    """Normalize any onion-bearing string to canonical http URL form."""
    m = ONION_RE.search(raw or "")
    if not m:
        return None
    host = m.group(0)
    if not host.startswith("http"):
        host = "http://" + host.lstrip("/")
    return host


def extract_onion_links(html_content: str, base_url: str = "") -> set[str]:
    """Extract every .onion URL from a page: hrefs, src, and raw text."""
    links: set[str] = set()
    soup = BeautifulSoup(html_content, "html.parser")

    for tag in soup.find_all(["a", "link", "area"], href=True):
        href = tag["href"]
        absolute = urljoin(base_url, href) if base_url else href
        norm = normalize_onion(absolute) or normalize_onion(href)
        if norm:
            links.add(norm)

    for tag in soup.find_all(src=True):
        norm = normalize_onion(tag["src"])
        if norm:
            links.add(norm)

    # Catch raw .onion strings mentioned in plain text (very common on indexes/forums)
    for match in ONION_RE.finditer(html_content):
        norm = normalize_onion(match.group(0))
        if norm:
            links.add(norm)

    return links


def clean_html_to_text(html_content: str) -> str:
    """Strip scripts/styles/nav and collapse whitespace to readable text."""
    soup = BeautifulSoup(html_content, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg", "head"]):
        tag.extract()
    text = soup.get_text(separator=" ")
    lines = (line.strip() for line in text.splitlines())
    chunks = (phrase.strip() for line in lines for phrase in line.split("  "))
    return "\n".join(chunk for chunk in chunks if chunk)


def page_title(html_content: str) -> str:
    soup = BeautifulSoup(html_content, "html.parser")
    if soup.title and soup.title.string:
        return soup.title.string.strip()
    h1 = soup.find("h1")
    return h1.get_text(strip=True) if h1 else "(no title)"


def keyword_snippets(text: str, keyword: str, max_hits: int = 5) -> list[str]:
    """Return context windows around EVERY occurrence of keyword (capped)."""
    if not keyword:
        return []
    snippets = []
    low = text.lower()
    kw = keyword.lower()
    start = 0
    while len(snippets) < max_hits:
        pos = low.find(kw, start)
        if pos == -1:
            break
        a = max(0, pos - 200)
        b = min(len(text), pos + len(keyword) + 300)
        snippets.append("... " + text[a:b].replace("\n", " ").strip() + " ...")
        start = pos + len(keyword)
    return snippets


# ----------------------------------------------------------------------------
# Tool 1: multi-index search
# ----------------------------------------------------------------------------
def _search_ahmia(session: requests.Session, query: str) -> list[dict]:
    """Query the Ahmia index (filters known abuse content)."""
    out = []
    url = f"https://ahmia.fi/search/?q={requests.utils.quote(query)}"
    res = fetch(session, url, timeout=REQUEST_TIMEOUT)
    if not res:
        return out
    soup = BeautifulSoup(res.text, "html.parser")
    for li in soup.find_all("li", class_="result"):
        a = li.find("a")
        cite = li.find("cite")
        p = li.find("p")
        link = normalize_onion(cite.get_text(strip=True)) if cite else None
        if not link and a and a.get("href"):
            # ahmia redirect link e.g. /search/redirect?...&redirect_url=<onion>
            link = normalize_onion(a["href"])
        if link:
            out.append({
                "source": "ahmia",
                "title": a.get_text(strip=True) if a else "(no title)",
                "link": link,
                "desc": p.get_text(strip=True) if p else "",
            })
    return out


# Registry of search backends. Add more callables here to widen coverage.
SEARCH_BACKENDS = [_search_ahmia]


def darkweb_multi_search(query: str) -> str:
    if not query or not query.strip():
        return "Error: empty query."

    session = build_session()
    aggregated: list[dict] = []
    errors: list[str] = []

    with ThreadPoolExecutor(max_workers=len(SEARCH_BACKENDS) or 1) as ex:
        futures = {ex.submit(fn, session, query): fn.__name__ for fn in SEARCH_BACKENDS}
        for fut in as_completed(futures):
            name = futures[fut]
            try:
                aggregated.extend(fut.result())
            except Exception as e:  # noqa: BLE001
                errors.append(f"[!] {name} failed: {e}")

    # Deduplicate by onion host
    seen, unique = set(), []
    for r in aggregated:
        host = urlparse(r["link"]).netloc
        if host not in seen:
            seen.add(host)
            unique.append(r)

    if not unique:
        msg = (f"No results for '{query}' in public indexes. "
               f"Try onion_deep_spider on a known .onion to crawl it directly.")
        if errors:
            msg += "\n" + "\n".join(errors)
        return msg

    lines = [f"=== {len(unique)} unique results for '{query}' ==="]
    for r in unique:
        lines.append(
            f"Title: {r['title']}\nLink: {r['link']}\nSource: {r['source']}\nDesc: {r['desc']}\n---"
        )
    if errors:
        lines.append("\n".join(errors))
    return "\n".join(lines)[:MAX_OUTPUT_CHARS]


# ----------------------------------------------------------------------------
# Tool 2: concurrent BFS spider
# ----------------------------------------------------------------------------
def _crawl_one(session: requests.Session, url: str, keyword: str | None) -> dict:
    """Fetch a single page; return parsed result dict."""
    res = fetch(session, url)
    if not res:
        return {"url": url, "ok": False, "error": "unreachable", "links": set()}
    text = clean_html_to_text(res.text)
    links = extract_onion_links(res.text, base_url=url)
    record = {
        "url": url,
        "ok": True,
        "title": page_title(res.text),
        "links": links,
        "text": text,
    }
    if keyword:
        record["snippets"] = keyword_snippets(text, keyword)
    return record


def onion_deep_spider(target_url: str, keyword: str = None, depth: int = 1) -> str:
    norm = normalize_onion(target_url)
    if not norm:
        return "Error: target_url must be a valid .onion address."

    depth = max(0, min(int(depth), 3))  # safety cap
    session = build_session()

    visited: set[str] = set()
    frontier = [norm]
    out = [f"[*] Deep crawl start: {norm} (depth={depth}, max_pages={MAX_PAGES})"]
    if keyword:
        out.append(f"[*] Keyword filter: '{keyword}'")

    matches = 0
    for level in range(depth + 1):
        if not frontier:
            break
        # only crawl pages not yet seen, respecting the global page cap
        batch = [u for u in frontier if u not in visited][: MAX_PAGES - len(visited)]
        for u in batch:
            visited.add(u)
        if not batch:
            break

        out.append(f"\n--- Depth {level}: crawling {len(batch)} page(s) ---")
        next_frontier: set[str] = set()

        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
            futures = {ex.submit(_crawl_one, session, u, keyword): u for u in batch}
            for fut in as_completed(futures):
                rec = fut.result()
                if not rec["ok"]:
                    out.append(f"[-] {rec['url']} :: {rec['error']}")
                    continue

                if keyword:
                    snips = rec.get("snippets", [])
                    if snips:
                        matches += 1
                        out.append(f"[!!!] MATCH @ {rec['url']} ({rec['title']})")
                        for s in snips:
                            out.append(f"    {s}")
                else:
                    out.append(f"[+] {rec['url']} ({rec['title']})")
                    out.append(f"    {rec['text'][:400]}")

                # queue child links for next depth
                if level < depth:
                    next_frontier |= rec["links"]

        frontier = list(next_frontier - visited)
        if len(visited) >= MAX_PAGES:
            out.append(f"\n[*] Reached page cap ({MAX_PAGES}); stopping.")
            break

    out.append(f"\n[*] Done. Pages crawled: {len(visited)}. "
               + (f"Keyword matches: {matches}." if keyword else
                  f"Unique onion links discovered: {sum(1 for _ in visited)}."))
    return "\n".join(out)[:MAX_OUTPUT_CHARS]


# ----------------------------------------------------------------------------
# MCP tool schemas
# ----------------------------------------------------------------------------
TOOLS = [
    {
        "name": "darkweb_multi_search",
        "description": "Searches the darknet via multiple indexing gateways at once for links related to a query.",
        "inputSchema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
    {
        "name": "onion_deep_spider",
        "description": "Autonomous web spider (crawler) that enters a .onion page, analyzes its code, extracts deep links and searches page content for given keywords.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "target_url": {"type": "string", "description": ".onion address to scan"},
                "keyword": {"type": "string", "description": "Keyword to search for in page content"},
                "depth": {"type": "integer", "description": "Link crawl depth (default 1, max 3)"},
            },
            "required": ["target_url"],
        },
    },
]

TOOL_IMPL = {
    "darkweb_multi_search": lambda a: darkweb_multi_search(a.get("query", "")),
    "onion_deep_spider": lambda a: onion_deep_spider(
        target_url=a.get("target_url", ""),
        keyword=a.get("keyword"),
        depth=int(a.get("depth", 1)),
    ),
}


def _rpc_result(req_id, result):
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _rpc_error(req_id, code, message):
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


@app.post("/mcp")
@app.post("/")
async def handle_mcp_request(request: Request):
    try:
        body = await request.json()
    except Exception:
        return _rpc_error(None, -32700, "Parse error")

    method = body.get("method")
    req_id = body.get("id")

    if method == "initialize":
        return _rpc_result(req_id, {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "Onion-Crawler", "version": "3.0.0"},
        })

    # MCP clients send this notification after initialize; ack with no result.
    if method in ("notifications/initialized", "initialized"):
        return _rpc_result(req_id, {})

    if method == "tools/list":
        return _rpc_result(req_id, {"tools": TOOLS})

    if method == "tools/call":
        params = body.get("params", {})
        name = params.get("name")
        arguments = params.get("arguments", {}) or {}
        impl = TOOL_IMPL.get(name)
        if not impl:
            return _rpc_error(req_id, -32601, f"Unknown tool: {name}")
        try:
            text_output = impl(arguments)
        except Exception as e:  # noqa: BLE001
            log.exception("tool %s crashed", name)
            return _rpc_result(req_id, {
                "content": [{"type": "text", "text": f"Tool error: {e}"}],
                "isError": True,
            })
        return _rpc_result(req_id, {"content": [{"type": "text", "text": text_output}]})

    return _rpc_error(req_id, -32601, f"Method not found: {method}")


@app.get("/health")
async def health():
    return {"status": "ok", "tools": [t["name"] for t in TOOLS]}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
