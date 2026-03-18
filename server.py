#!/usr/bin/env python3
"""
WhiteBoard Server
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
pip install aiohttp
python server.py
Open http://DOMAIN:PORT in any browser.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Data layout (DATA_DIR):
  data/
    config.json            ← global config (accounts, settings)
    <account_code>/
      config.json          ← per-account settings
      boards/
        <board_id>.json    ← board data (strokes + metadata)
"""

import json
import logging
import os
import time
import uuid
from pathlib import Path
from aiohttp import web, WSMsgType

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)

# ╔══════════════════════════════════════════════╗
# ║  CONFIGURATION                               ║
# ╚══════════════════════════════════════════════╝
# ╔══════════════════════════════════════════════════════════════╗
# ║  USER CONFIGURATION — edit these lines                      ║
# ╚══════════════════════════════════════════════════════════════╝
MASTER    = "0000"     # ← Master admin code — change this!
HOST      = "0.0.0.0"  # Interface to listen on
DOMAIN    = "localhost" # Hostname shown in startup log
PORT      = 8765        # Port to serve on
DATA_DIR  = Path(__file__).parent / "data"
# ════════════════════════════════════════════════════════════════
# ═══════════════════════════════════════════════

DATA_DIR.mkdir(exist_ok=True)
GLOBAL_CFG = DATA_DIR / "config.json"

# ── Global config helpers ──────────────────────────────────────────
def load_global_cfg():
    if GLOBAL_CFG.exists():
        try:
            return json.loads(GLOBAL_CFG.read_text())
        except Exception:
            pass
    return {"accounts": {}, "settings": {"server_name": "WhiteBoard", "allow_guest": False}}

def save_global_cfg(cfg):
    GLOBAL_CFG.write_text(json.dumps(cfg, indent=2))

# Seed master account if not present
def ensure_master():
    cfg = load_global_cfg()
    if MASTER not in cfg["accounts"]:
        cfg["accounts"][MASTER] = {
            "label": "Admin",
            "is_master": True,
            "color": "#6c6fff",
            "created": time.time(),
        }
        save_global_cfg(cfg)
    # Ensure master data dir
    md = DATA_DIR / MASTER
    md.mkdir(exist_ok=True)
    (md / "boards").mkdir(exist_ok=True)

ensure_master()

# ── Per-account helpers ────────────────────────────────────────────
def account_dir(code):
    return DATA_DIR / code

def account_boards_dir(code):
    d = account_dir(code) / "boards"
    d.mkdir(parents=True, exist_ok=True)
    return d

def load_account_cfg(code):
    p = account_dir(code) / "config.json"
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            pass
    return {"label": code, "created": time.time()}

def save_account_cfg(code, cfg):
    account_dir(code).mkdir(parents=True, exist_ok=True)
    p = account_dir(code) / "config.json"
    p.write_text(json.dumps(cfg, indent=2))

def list_boards(code):
    """Return list of board metadata dicts sorted by last modified."""
    bd = account_boards_dir(code)
    boards = []
    for f in bd.glob("*.json"):
        try:
            data = json.loads(f.read_text())
            boards.append({
                "id":      data.get("id", f.stem),
                "name":    data.get("name", f.stem),
                "created": data.get("created", 0),
                "updated": data.get("updated", 0),
            })
        except Exception:
            pass
    boards.sort(key=lambda b: b["updated"], reverse=True)
    return boards

def load_board(code, board_id):
    p = account_boards_dir(code) / f"{board_id}.json"
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            pass
    return None

def save_board(code, board_data):
    board_id = board_data.get("id")
    if not board_id:
        return
    board_data["updated"] = time.time()
    p = account_boards_dir(code) / f"{board_id}.json"
    p.write_text(json.dumps(board_data))

def delete_board(code, board_id):
    p = account_boards_dir(code) / f"{board_id}.json"
    if p.exists():
        p.unlink()

# ── WebSocket rooms ───────────────────────────────────────────────
# rooms[code][board_id] = set of ws
_rooms: dict = {}

def join_room(code, board_id, ws):
    _rooms.setdefault(code, {}).setdefault(board_id, set()).add(ws)

def leave_room(code, board_id, ws):
    try:
        _rooms[code][board_id].discard(ws)
    except KeyError:
        pass

def leave_all_rooms(ws, code):
    if code in _rooms:
        for board_id, peers in _rooms[code].items():
            peers.discard(ws)

async def broadcast_room(code, board_id, message_str, exclude_ws=None):
    dead = []
    for peer in list(_rooms.get(code, {}).get(board_id, set())):
        if peer is exclude_ws:
            continue
        try:
            await peer.send_str(message_str)
        except Exception:
            dead.append((code, board_id, peer))
    for c, b, p in dead:
        _rooms.get(c, {}).get(b, set()).discard(p)

# ── HTTP / WS handlers ────────────────────────────────────────────

async def ws_handler(request):
    ws = web.WebSocketResponse(max_msg_size=200_000_000, heartbeat=30)
    await ws.prepare(request)
    addr    = request.remote
    code    = None   # set after auth
    active_board = None

    logging.info(f"WS connect from {addr}")

    async for msg in ws:
        if msg.type == WSMsgType.TEXT:
            try:
                data = json.loads(msg.data)
            except Exception:
                continue

            mtype = data.get("type", "")

            # ── Auth ──
            if mtype == "auth":
                incoming_code = str(data.get("code", "")).strip()
                cfg = load_global_cfg()
                if incoming_code == MASTER or incoming_code in cfg["accounts"]:
                    code = incoming_code
                    is_master = (incoming_code == MASTER)
                    # Ensure dirs
                    account_dir(code).mkdir(parents=True, exist_ok=True)
                    account_boards_dir(code)
                    # Get or create account entry
                    if code not in cfg["accounts"]:
                        cfg["accounts"][code] = {
                            "label": f"Account {code}",
                            "is_master": is_master,
                            "color": "#6c6fff",
                            "created": time.time(),
                        }
                        save_global_cfg(cfg)
                    acct_cfg = load_account_cfg(code)
                    boards = list_boards(code)
                    await ws.send_json({
                        "type": "auth_ok",
                        "is_master": is_master,
                        "account": cfg["accounts"][code],
                        "account_cfg": acct_cfg,
                        "boards": boards,
                    })
                    logging.info(f"Auth OK: {addr} as {'MASTER' if is_master else code}")
                else:
                    await ws.send_json({"type": "auth_fail"})
                continue

            # Require auth for everything below
            if not code:
                await ws.send_json({"type": "error", "msg": "Not authenticated"})
                continue

            # ── Board operations ──
            if mtype == "get_board":
                bid = data.get("board_id")
                board = load_board(code, bid)
                if board:
                    if active_board:
                        leave_room(code, active_board, ws)
                    active_board = bid
                    join_room(code, bid, ws)
                    await ws.send_json({"type": "board_data", "board": board})
                else:
                    await ws.send_json({"type": "board_not_found", "board_id": bid})

            elif mtype == "create_board":
                name = data.get("name", "New Board").strip() or "New Board"
                bid  = str(uuid.uuid4())[:8]
                board = {
                    "id": bid,
                    "name": name,
                    "created": time.time(),
                    "updated": time.time(),
                    "strokes": [],
                    "snapshot": None,   # base64 PNG for quick load
                }
                save_board(code, board)
                boards = list_boards(code)
                await ws.send_json({
                    "type": "board_created",
                    "board": board,
                    "boards": boards,
                })

            elif mtype == "rename_board":
                bid  = data.get("board_id")
                name = data.get("name", "").strip()
                if bid and name:
                    board = load_board(code, bid)
                    if board:
                        board["name"] = name
                        save_board(code, board)
                        await ws.send_json({"type": "board_renamed", "board_id": bid, "name": name})
                        # Notify room peers
                        await broadcast_room(code, bid,
                            json.dumps({"type": "board_renamed", "board_id": bid, "name": name}),
                            exclude_ws=ws)

            elif mtype == "delete_board":
                bid = data.get("board_id")
                if bid:
                    delete_board(code, bid)
                    leave_room(code, bid, ws)
                    if active_board == bid:
                        active_board = None
                    boards = list_boards(code)
                    await ws.send_json({"type": "board_deleted", "board_id": bid, "boards": boards})

            elif mtype == "clear_board":
                bid = data.get("board_id") or active_board
                if bid:
                    board = load_board(code, bid)
                    if board:
                        board["strokes"] = []
                        board["snapshot"] = None
                        save_board(code, board)
                        await ws.send_json({"type": "board_cleared", "board_id": bid})
                        await broadcast_room(code, bid,
                            json.dumps({"type": "board_cleared", "board_id": bid}),
                            exclude_ws=ws)

            # ── Canvas sync (snapshot + strokes) ──
            elif mtype == "canvas_snapshot":
                bid      = data.get("board_id") or active_board
                snapshot = data.get("snapshot")  # base64 PNG
                if bid and snapshot:
                    board = load_board(code, bid)
                    if not board:
                        board = {"id": bid, "name": bid, "created": time.time(), "strokes": []}
                    board["snapshot"] = snapshot
                    save_board(code, board)
                    # broadcast to room
                    await broadcast_room(code, bid,
                        json.dumps({"type": "canvas_snapshot", "board_id": bid, "snapshot": snapshot}),
                        exclude_ws=ws)

            elif mtype == "stroke":
                # Real-time stroke broadcast — don't save each stroke individually
                # (snapshot saves periodically)
                bid = data.get("board_id") or active_board
                if bid:
                    await broadcast_room(code, bid,
                        json.dumps({"type": "stroke", "board_id": bid,
                                    "stroke": data.get("stroke")}),
                        exclude_ws=ws)

            elif mtype == "join_board":
                bid = data.get("board_id")
                if bid:
                    if active_board:
                        leave_room(code, active_board, ws)
                    active_board = bid
                    join_room(code, bid, ws)

            # ── Admin operations (master only) ──
            elif mtype == "admin_get_accounts":
                cfg = load_global_cfg()
                if code == MASTER:
                    await ws.send_json({"type": "admin_accounts", "accounts": cfg["accounts"], "settings": cfg.get("settings", {})})
                else:
                    await ws.send_json({"type": "error", "msg": "Not master"})

            elif mtype == "admin_add_account":
                if code != MASTER:
                    await ws.send_json({"type": "error", "msg": "Not master"})
                    continue
                new_code  = str(data.get("code", "")).strip()
                new_label = str(data.get("label", "")).strip() or f"Account {new_code}"
                new_color = str(data.get("color", "#6c6fff")).strip()
                if not new_code:
                    await ws.send_json({"type": "error", "msg": "Invalid code"})
                    continue
                cfg = load_global_cfg()
                if new_code in cfg["accounts"]:
                    await ws.send_json({"type": "error", "msg": "Code already exists"})
                    continue
                cfg["accounts"][new_code] = {
                    "label": new_label,
                    "is_master": False,
                    "color": new_color,
                    "created": time.time(),
                }
                save_global_cfg(cfg)
                account_dir(new_code).mkdir(parents=True, exist_ok=True)
                account_boards_dir(new_code)
                await ws.send_json({"type": "admin_accounts", "accounts": cfg["accounts"], "settings": cfg.get("settings", {})})

            elif mtype == "admin_remove_account":
                if code != MASTER:
                    await ws.send_json({"type": "error", "msg": "Not master"})
                    continue
                rem_code = str(data.get("code", "")).strip()
                if rem_code == MASTER:
                    await ws.send_json({"type": "error", "msg": "Cannot remove master"})
                    continue
                cfg = load_global_cfg()
                cfg["accounts"].pop(rem_code, None)
                save_global_cfg(cfg)
                await ws.send_json({"type": "admin_accounts", "accounts": cfg["accounts"], "settings": cfg.get("settings", {})})

            elif mtype == "admin_update_settings":
                if code != MASTER:
                    await ws.send_json({"type": "error", "msg": "Not master"})
                    continue
                cfg = load_global_cfg()
                cfg.setdefault("settings", {}).update(data.get("settings", {}))
                save_global_cfg(cfg)
                await ws.send_json({"type": "settings_updated", "settings": cfg["settings"]})

            elif mtype == "update_account_cfg":
                acct_cfg = data.get("cfg", {})
                save_account_cfg(code, acct_cfg)
                await ws.send_json({"type": "account_cfg_updated"})

        elif msg.type in (WSMsgType.CLOSE, WSMsgType.ERROR):
            break

    if code:
        leave_all_rooms(ws, code)
    logging.info(f"WS disconnect {addr}")
    return ws


async def index(request):
    return web.FileResponse(Path(__file__).parent / "index.html")


# ── App setup ─────────────────────────────────────────────────────
app = web.Application()
app.router.add_get("/",   index)
app.router.add_get("/ws", ws_handler)

if __name__ == "__main__":
    logging.info("═" * 56)
    logging.info(f"  WhiteBoard  →  http://{DOMAIN}:{PORT}")
    logging.info(f"  Listening   →  {HOST}:{PORT}")
    logging.info(f"  Data dir    →  {DATA_DIR.resolve()}")
    logging.info(f"  Master code →  {MASTER}")
    logging.info("═" * 56)
    web.run_app(app, host=HOST, port=PORT, access_log=None)