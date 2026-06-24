"""
Live Preview Server
====================
FastAPI + WebSocket. Klient otwiera /preview -> przegladarka pokazuje
viewport Blendera na zywo jako stream JPEG-ow (Motion JPEG przez WS).

Blender addon pushuje kazda klatke do /ingest (POST base64 JPEG).
Wszyscy podlaczeni klienci od razu dostaja klatke.
"""
import os
import asyncio
import base64
import json
from pathlib import Path
from typing import Set

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

PREVIEW_PORT = int(os.getenv("PREVIEW_PORT", "8001"))
STATIC_DIR = Path(__file__).parent

app = FastAPI(title="blender-mcp preview")

# globalny stan: ostatnia klatka + set aktywnych klientow WS
_LATEST_FRAME: bytes = b""
_CLIENTS: Set[WebSocket] = set()
_LOCK = asyncio.Lock()


@app.get("/preview", response_class=HTMLResponse)
async def preview_page():
    html = (STATIC_DIR / "preview.html").read_text(encoding="utf-8")
    return HTMLResponse(html)


@app.get("/")
async def root():
    return JSONResponse({
        "service": "blender-mcp",
        "mcp_endpoint": "/mcp",
        "preview": "/preview",
        "preview_ws": "/preview/ws",
        "ingest": "POST /preview/ingest (base64 jpeg in body)",
    })


@app.post("/preview/ingest")
async def ingest(req: Request):
    """Blender addon wysyla tu kolejne klatki viewportu."""
    global _LATEST_FRAME
    body = await req.body()
    # body to surowy base64 JPEG (string) albo binarny JPEG
    try:
        if body[:5] in (b"data:", b"/9j/A") or body.lstrip()[:1] in (b"i", b"/"):
            # base64 string
            jpg = base64.b64decode(body.split(b",")[-1])
        else:
            jpg = body
    except Exception:
        jpg = body

    _LATEST_FRAME = jpg

    # roznieS do wszystkich klientow
    dead = []
    for ws in list(_CLIENTS):
        try:
            await ws.send_bytes(jpg)
        except Exception:
            dead.append(ws)
    for ws in dead:
        _CLIENTS.discard(ws)

    return {"ok": True, "clients": len(_CLIENTS), "bytes": len(jpg)}


@app.websocket("/preview/ws")
async def preview_ws(ws: WebSocket):
    await ws.accept()
    _CLIENTS.add(ws)
    try:
        # wyslij od razu ostatnia klatke jak jest
        if _LATEST_FRAME:
            await ws.send_bytes(_LATEST_FRAME)
        # trzymaj polaczenie, czekaj na klatki (pushowane przez ingest)
        while True:
            await asyncio.sleep(30)
            try:
                await ws.send_text("ping")
            except Exception:
                break
    except WebSocketDisconnect:
        pass
    finally:
        _CLIENTS.discard(ws)


def start_preview_server():
    uvicorn.run(app, host="0.0.0.0", port=PREVIEW_PORT, log_level="warning")


if __name__ == "__main__":
    start_preview_server()
