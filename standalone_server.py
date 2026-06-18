"""
Standalone Streamable HTTP MCP server hostujący wyłącznie 'onion-search'.

Po co osobny wariant?
  Gumloop wymaga publicznego HTTPS endpointu (Streamable HTTP albo SSE) –
  patrz https://docs.gumloop.com/nodes/mcp/custom_mcp_servers.
  guMCP `remote.py` hostuje WSZYSTKIE serwery z folderu src/servers; tutaj
  exportujemy tylko onion-search, żeby deploy był mały i prosty.

Endpoints:
  GET  /                – healthcheck
  POST /mcp             – Streamable HTTP MCP (zalecane przez Gumloop)
  GET  /sse             – fallback SSE transport

Auth:
  Opcjonalny Bearer token (env MCP_BEARER_TOKEN). Gumloop wysyła go w
  nagłówku Authorization. Jeśli env nie jest ustawiony – auth wyłączony.
"""
import os
import sys
import logging
import argparse
import contextlib

import uvicorn
from starlette.applications import Starlette
from starlette.responses import JSONResponse, Response
from starlette.routing import Route, Mount
from starlette.requests import Request
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware

from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.sse import SseServerTransport

# Załaduj naszą implementację serwera
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src", "servers", "onion-search"))
from main import create_server, get_initialization_options  # noqa: E402

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("onion-search-standalone")

BEARER_TOKEN = os.environ.get("MCP_BEARER_TOKEN", "").strip()


class BearerAuthMiddleware(BaseHTTPMiddleware):
    """Bardzo prosty Bearer-token guard (jeśli MCP_BEARER_TOKEN jest ustawiony)."""

    async def dispatch(self, request: Request, call_next):
        # Healthchecki bez auth
        if request.url.path in ("/", "/health"):
            return await call_next(request)
        if not BEARER_TOKEN:
            return await call_next(request)
        auth = request.headers.get("authorization", "")
        if not auth.lower().startswith("bearer "):
            return JSONResponse({"error": "missing bearer token"}, status_code=401)
        token = auth.split(" ", 1)[1].strip()
        if token != BEARER_TOKEN:
            return JSONResponse({"error": "invalid token"}, status_code=403)
        return await call_next(request)


def build_app() -> Starlette:
    # Singleton serwer (stateless – nie trzymamy state per użytkownik).
    server_instance = create_server(user_id="default")
    init_options = get_initialization_options(server_instance)

    # ---- Streamable HTTP (preferowany przez Gumloop) ----
    session_manager = StreamableHTTPSessionManager(
        app=server_instance,
        event_store=None,
        json_response=False,
        stateless=True,
    )

    async def handle_streamable(scope, receive, send):
        await session_manager.handle_request(scope, receive, send)

    # ---- SSE (fallback) ----
    sse_transport = SseServerTransport("/messages/")

    async def handle_sse(request: Request):
        async with sse_transport.connect_sse(
            request.scope, request.receive, request._send
        ) as streams:
            await server_instance.run(streams[0], streams[1], init_options)
        return Response()

    async def health(request: Request):
        return JSONResponse({
            "status": "ok",
            "server": "onion-search",
            "transport": ["streamable-http", "sse"],
            "tor_proxy": os.environ.get("TOR_SOCKS_PROXY", "socks5h://127.0.0.1:9050"),
        })

    @contextlib.asynccontextmanager
    async def lifespan(app):
        async with session_manager.run():
            logger.info("StreamableHTTPSessionManager started")
            yield
            logger.info("Shutting down session manager")

    app = Starlette(
        debug=False,
        lifespan=lifespan,
        middleware=[Middleware(BearerAuthMiddleware)],
        routes=[
            Route("/", endpoint=health),
            Route("/health", endpoint=health),
            Mount("/mcp", app=handle_streamable),
            Route("/sse", endpoint=handle_sse),
            Mount("/messages/", app=sse_transport.handle_post_message),
        ],
    )
    return app


def main():
    parser = argparse.ArgumentParser(description="Onion-search standalone MCP server")
    parser.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8080")))
    args = parser.parse_args()

    app = build_app()
    logger.info(
        "Serving onion-search MCP on http://%s:%d  (auth=%s, tor=%s)",
        args.host, args.port,
        "on" if BEARER_TOKEN else "off",
        os.environ.get("TOR_SOCKS_PROXY", "socks5h://127.0.0.1:9050"),
    )
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
