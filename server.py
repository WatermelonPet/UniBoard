#!/usr/bin/env python3
"""
Collaborative Whiteboard — Web Server
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
pip install aiohttp
python server.py

Then open http://DOMAIN:PORT in any browser.
Anyone on the network can connect the same way.
"""

import json
import logging
from pathlib import Path
from aiohttp import web, WSMsgType

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)

# ╔══════════════════════════════════════════╗
# ║  CONFIGURATION — edit these three lines  ║
# ╚══════════════════════════════════════════╝
HOST   = "0.0.0.0"      # interface to listen on
DOMAIN = "localhost"    # hostname / public IP (shown in start-up log)
PORT   = 8765
# ════════════════════════════════════════════

_clients: set   = set()
_snapshots: dict = {}   # board_id → latest base64-PNG string


async def ws_handler(request):
    ws = web.WebSocketResponse(max_msg_size=200_000_000, heartbeat=20)
    await ws.prepare(request)
    _clients.add(ws)
    addr = request.remote
    logging.info(f"+ {addr}  ({len(_clients)} online)")

    # Send all current board snapshots to the newcomer
    for bid, snap in list(_snapshots.items()):
        try:
            await ws.send_json({"type": "board_state", "board_id": bid, "data": snap})
        except Exception:
            pass

    async for msg in ws:
        if msg.type == WSMsgType.TEXT:
            try:
                data = json.loads(msg.data)
            except Exception:
                continue

            mtype = data.get("type", "")
            bid   = data.get("board_id", "default")

            if mtype == "canvas_state":
                _snapshots[bid] = data.get("data", "")
            elif mtype == "clear":
                _snapshots.pop(bid, None)

            if data.get("public", True):
                dead = []
                for peer in _clients:
                    if peer is ws:
                        continue
                    try:
                        await peer.send_str(msg.data)
                    except Exception:
                        dead.append(peer)
                for d in dead:
                    _clients.discard(d)

        elif msg.type in (WSMsgType.CLOSE, WSMsgType.ERROR):
            break

    _clients.discard(ws)
    logging.info(f"- {addr}  ({len(_clients)} online)")
    return ws


async def index(request):
    return web.FileResponse(Path(__file__).parent / "index.html")


app = web.Application()
app.router.add_get("/",   index)
app.router.add_get("/ws", ws_handler)

if __name__ == "__main__":
    logging.info("═" * 54)
    logging.info(f"  Whiteboard  →  http://{DOMAIN}:{PORT}")
    logging.info(f"  Listening   →  {HOST}:{PORT}")
    logging.info("═" * 54)
    web.run_app(app, host=HOST, port=PORT, access_log=None)
